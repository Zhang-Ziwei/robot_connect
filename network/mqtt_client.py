"""
通用 MQTT 客户端（业务无关）。

只做连接、订阅、发布、以及按 requestId 匹配的请求-应答。
ConST 的主题/字段约定在 programs/CONST_FLOW/mqtt_adapter.py。

依赖 paho-mqtt；未安装时 connect() 会给出明确错误，不会在 import 阶段炸掉。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()
_LOG = "MqttClient"

MessageCallback = Callable[[str, Dict[str, Any]], None]


def _make_paho_client(client_id: str):
    try:
        import paho.mqtt.client as mqtt  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "未安装 paho-mqtt，无法建立 MQTT 连接。请执行: pip install paho-mqtt"
        ) from exc

    kwargs = {"client_id": client_id, "clean_session": True}
    try:
        # paho-mqtt 2.x 要求显式 callback API 版本
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,  # type: ignore[attr-defined]
            protocol=mqtt.MQTTv311,
            **kwargs,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(protocol=mqtt.MQTTv311, **kwargs)
    return client


class MqttClient:
    """
    后台 loop 的 MQTT 客户端。

    request() 会发布一条带 requestId 的 JSON，阻塞等到任意已订阅主题上
    出现相同 requestId 的消息，或超时。适合规范里的点对点交互。
    """

    def __init__(self, client_id: str = "robot_connect"):
        self.client_id = client_id
        self._client = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[threading.Event, List[Optional[Dict[str, Any]]]]] = {}
        self._topic_callbacks: List[Tuple[str, MessageCallback]] = []
        self._host = ""
        self._port = 0

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self._client is not None

    def connect(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        keepalive: int = 30,
        timeout: float = 8.0,
    ) -> bool:
        self.disconnect()
        self._host = host
        self._port = int(port)
        client = _make_paho_client(self.client_id)
        if username:
            client.username_pw_set(username, password or None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        self._connected.clear()
        try:
            client.connect(host, int(port), keepalive=keepalive)
        except Exception as e:  # noqa: BLE001
            logger.error(_LOG, f"连接 {host}:{port} 失败: {e}")
            self._client = None
            return False
        client.loop_start()
        if not self._connected.wait(timeout=timeout):
            logger.error(_LOG, f"连接 {host}:{port} 超时（{timeout}s）")
            self.disconnect()
            return False
        logger.info(_LOG, f"已连接 {host}:{port} client_id={self.client_id}")
        return True

    def disconnect(self):
        client = self._client
        self._client = None
        self._connected.clear()
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def subscribe(self, topic: str, qos: int = 1) -> bool:
        if not self._client:
            return False
        result, _mid = self._client.subscribe(topic, qos=qos)
        ok = result == 0
        if ok:
            logger.info(_LOG, f"订阅 {topic}")
        else:
            logger.error(_LOG, f"订阅 {topic} 失败 rc={result}")
        return ok

    def on_topic(self, topic_filter: str, callback: MessageCallback):
        """
        注册主题回调。topic_filter 支持 MQTT 单层 + 与多层 #。
        所有消息仍会先走 requestId 匹配。
        """
        self._topic_callbacks.append((topic_filter, callback))

    def publish(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False) -> bool:
        if not self._client:
            logger.error(_LOG, f"发布 {topic} 失败：未连接")
            return False
        body = json.dumps(payload, ensure_ascii=False)
        info = self._client.publish(topic, body, qos=qos, retain=retain)
        if info.rc != 0:
            logger.error(_LOG, f"发布 {topic} 失败 rc={info.rc}")
            return False
        return True

    def request(
        self,
        topic: str,
        payload: Dict[str, Any],
        timeout: float = 1.0,
        retries: int = 2,
        qos: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        发布 payload（必须含 requestId），等待任意主题上相同 requestId 的回复。

        retries 是超时后的重发次数：retries=2 表示最多发 3 次。
        """
        request_id = str(payload.get("requestId") or "")
        if not request_id:
            logger.error(_LOG, f"request() 缺少 requestId，主题 {topic}")
            return None

        event = threading.Event()
        box: List[Optional[Dict[str, Any]]] = [None]
        with self._lock:
            self._pending[request_id] = (event, box)

        attempts = max(0, int(retries)) + 1
        try:
            for i in range(attempts):
                if not self.publish(topic, payload, qos=qos):
                    time.sleep(min(timeout, 0.2))
                    continue
                if event.wait(timeout=timeout):
                    return box[0]
                logger.warning(
                    _LOG,
                    f"requestId={request_id} 主题 {topic} 第 {i + 1}/{attempts} 次等待超时（{timeout}s）",
                )
            return None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    # ── paho 回调 ─────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):  # noqa: ARG002
        if rc == 0:
            self._connected.set()
        else:
            logger.error(_LOG, f"MQTT on_connect rc={rc}")
            self._connected.clear()

    def _on_disconnect(self, client, userdata, rc):  # noqa: ARG002
        self._connected.clear()
        if rc != 0:
            logger.warning(_LOG, f"MQTT 意外断开 rc={rc}，paho 将自动重连")

    def _on_message(self, client, userdata, msg):  # noqa: ARG002
        topic = getattr(msg, "topic", "") or ""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(_LOG, f"主题 {topic} 不是合法 JSON: {e}")
            return
        if not isinstance(payload, dict):
            return

        request_id = str(payload.get("requestId") or "")
        if request_id:
            with self._lock:
                pending = self._pending.get(request_id)
            if pending is not None:
                event, box = pending
                box[0] = payload
                event.set()

        for topic_filter, callback in list(self._topic_callbacks):
            if _topic_matches(topic_filter, topic):
                try:
                    callback(topic, payload)
                except Exception as e:  # noqa: BLE001
                    logger.error(_LOG, f"主题回调 {topic_filter} 异常: {e}")


def _topic_matches(filter_str: str, topic: str) -> bool:
    if filter_str == topic:
        return True
    f_parts = filter_str.split("/")
    t_parts = topic.split("/")
    for i, token in enumerate(f_parts):
        if token == "#":
            return True
        if i >= len(t_parts):
            return False
        if token == "+":
            continue
        if token != t_parts[i]:
            return False
    return len(f_parts) == len(t_parts)
