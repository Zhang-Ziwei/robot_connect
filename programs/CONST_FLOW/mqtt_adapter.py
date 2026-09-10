"""
ConST MQTT 协议适配（规范 v1 / ras）。

通用连接在 network.mqtt_client.MqttClient；本模块只翻译主题、字段、心跳、发现。
calsysId 发现一次后一直沿用回传值。
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from infrastructure.config_loader import load_config
from infrastructure.error_logger import get_error_logger
from network.mqtt_client import MqttClient
from programs.CONST_FLOW.constants import (
    ConstTimeout, HumanReason, RobotLiveStatus, StationStatus,
)

logger = get_error_logger()
_LOG = "CONST_MQTT"


def new_request_id() -> str:
    return f"{random.getrandbits(64):016x}"


def station_seq_of(station: Optional[Dict[str, Any]], default: int = 0) -> int:
    """上位机工位序号：规范字段是 stationSeq，兼容 sequenceNumber / sequence。"""
    if not station:
        return default
    raw = station.get("stationSeq")
    if raw is None or raw == "":
        raw = station.get("sequenceNumber")
    if raw is None or raw == "":
        raw = station.get("sequence")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def load_mqtt_config() -> Dict[str, Any]:
    cfg = load_config() or {}
    mqtt = dict(cfg.get("mqtt") or {})
    robot = dict(cfg.get("const_robot") or {})
    mqtt.setdefault("host", "127.0.0.1")
    mqtt.setdefault("port", 8870)
    mqtt.setdefault("username", "robot")
    mqtt.setdefault("password", "ConST123456")
    mqtt.setdefault("keepalive", 30)
    robot.setdefault("robotId", "robot2026001")
    robot.setdefault("name", "具身智能机器人")
    robot.setdefault("manufacturer", "XX机器人公司")
    robot.setdefault("model", "NO1")
    robot.setdefault("hardwareVersion", "H1.0.0")
    robot.setdefault("softwareVersion", "S1.0.0")
    return {"mqtt": mqtt, "robot": robot}


class ConSTMqttAdapter:
    """
    一台机器人对应一份适配器：心跳线程 + 订阅检定系统 live/通知 + 点对点请求。
    """

    def __init__(self, get_battery: Optional[Callable[[], int]] = None):
        loaded = load_mqtt_config()
        self._mqtt_cfg = loaded["mqtt"]
        self.robot_info = loaded["robot"]
        self.robot_id = str(self.robot_info["robotId"])
        self._client = MqttClient(client_id=f"const-{self.robot_id}")
        self._get_battery = get_battery

        self.calsys_id: str = ""
        self._lock = threading.RLock()
        self._live_status = RobotLiveStatus.IDLE
        self._station_seq: Optional[int] = None
        self.need_human = False
        self.human_reason = ""
        self._human_cleared = threading.Event()
        self._human_cleared.set()

        self._calsys_live: Dict[str, Any] = {}
        self._calsys_live_event = threading.Event()
        self._live_fp = None
        self._end_notify: Optional[Dict[str, Any]] = None
        self._end_event = threading.Event()
        self._dut_by_station: Dict[str, Dict[str, Any]] = {}
        self._dut_events: Dict[str, threading.Event] = {}

        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None

    @property
    def connected(self) -> bool:
        return self._client.connected

    def connect(self) -> bool:
        cfg = self._mqtt_cfg
        ok = self._client.connect(
            host=str(cfg.get("host") or "127.0.0.1"),
            port=int(cfg.get("port") or 8870),
            username=str(cfg.get("username") or ""),
            password=str(cfg.get("password") or ""),
            keepalive=int(cfg.get("keepalive") or 30),
        )
        if not ok:
            return False
        self._client.on_topic("v1/ras/calsys/info_reply", self._on_info_reply)
        self._client.on_topic("v1/ras/robot/info", self._on_robot_info_query)
        self._client.on_topic("v1/ras/calsys/+/live", self._on_calsys_live)
        self._client.on_topic("v1/ras/calsys/+/dutinfo_notify", self._on_dutinfo_notify)
        self._client.on_topic("v1/ras/calsys/end_notify", self._on_end_notify)
        self._client.subscribe("v1/ras/calsys/info_reply")
        self._client.subscribe("v1/ras/robot/info")
        self._client.subscribe("v1/ras/calsys/+/live")
        self._client.subscribe("v1/ras/calsys/+/dutinfo_notify")
        self._client.subscribe("v1/ras/calsys/end_notify")
        rid = self.robot_id
        self._client.subscribe(f"v1/ras/robot/{rid}/install_reply")
        self._client.subscribe(f"v1/ras/robot/{rid}/unInstall_reply")
        self._client.subscribe(f"v1/ras/robot/{rid}/start_reply")
        self._client.subscribe(f"v1/ras/robot/{rid}/dutInfo_reply")
        return True

    def disconnect(self):
        self.stop_heartbeat()
        self._client.disconnect()

    def start_heartbeat(self):
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="CONST-mqtt-live",
        )
        self._hb_thread.start()

    def stop_heartbeat(self):
        self._hb_stop.set()
        thread = self._hb_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._hb_thread = None

    def set_status(self, status: str, station_seq: Optional[int] = None):
        with self._lock:
            old = (self._live_status, self._station_seq)
            self._live_status = status
            if station_seq is not None:
                self._station_seq = int(station_seq)
            new = (self._live_status, self._station_seq)
        if old != new:
            logger.info(_LOG, f"机器人状态变为 {new[0]} stationSeq={new[1]}")

    def call_human(self, reason: str):
        with self._lock:
            self.need_human = True
            self.human_reason = reason or HumanReason.CALSYS_ERROR
            self._live_status = RobotLiveStatus.CALL_HUMAN
        self._human_cleared.clear()
        logger.warning(_LOG, f"呼叫人工 reason={self.human_reason}")

    def human_handled(self):
        with self._lock:
            self.need_human = False
            self.human_reason = ""
            if self._live_status == RobotLiveStatus.CALL_HUMAN:
                self._live_status = RobotLiveStatus.IDLE
        self._human_cleared.set()
        logger.info(_LOG, "人工已处理，清除 needHuman")

    def wait_human(self, stop_event: Optional[threading.Event] = None) -> bool:
        while not self._human_cleared.is_set():
            if stop_event is not None and stop_event.is_set():
                return False
            self._human_cleared.wait(timeout=0.5)
        return True

    def discover(self, timeout: float = ConstTimeout.MQTT_DISCOVER) -> bool:
        payload = {
            "requestId": new_request_id(),
            "data": dict(self.robot_info),
        }
        logger.info(_LOG, "发送上位机发现 %s" % json.dumps(payload.get("data"), ensure_ascii=False))
        reply = self._client.request(
            "v1/ras/calsys/info", payload,
            timeout=timeout, retries=ConstTimeout.MQTT_RETRIES,
        )
        data = (reply or {}).get("data") or {}
        if reply is None:
            logger.warning(_LOG, "上位机发现无回复")
        else:
            logger.info(_LOG, "收到上位机发现 %s" % json.dumps(data, ensure_ascii=False))
        calsys_id = str(data.get("calsysId") or "")
        if not calsys_id:
            if self.calsys_id:
                logger.info(_LOG, f"发现回复无 calsysId，使用 live 已记录的 {self.calsys_id}")
                return True
            logger.error(_LOG, "发现检定系统失败：回复里没有 calsysId")
            return False
        with self._lock:
            self.calsys_id = calsys_id
        logger.info(_LOG, f"已注册 calsysId={calsys_id}")
        return True

    def is_idle(self) -> bool:
        live = self.snapshot_calsys()
        if not live:
            return False
        return (not bool(live.get("isRunning"))) and (not bool(live.get("isPendingConfirm")))

    def is_pending_confirm(self) -> bool:
        return bool((self.snapshot_calsys() or {}).get("isPendingConfirm"))

    def is_bound(self) -> bool:
        live = self.snapshot_calsys()
        robots = (live or {}).get("bindingRobots") or []
        return self.robot_id in [str(x) for x in robots]

    def snapshot_calsys(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._calsys_live)

    def stations(self) -> List[Dict[str, Any]]:
        return list((self.snapshot_calsys().get("stations") or []))

    def enabled_stations(self) -> List[Dict[str, Any]]:
        return [s for s in self.stations() if s.get("isEnabled")]

    def find_wait_install_station(self) -> Optional[Dict[str, Any]]:
        for st in self.enabled_stations():
            if st.get("robotStationStatus") == StationStatus.WAIT_INSTALL:
                return st
        return None

    def all_enabled_free(self) -> bool:
        enabled = self.enabled_stations()
        if not enabled:
            return False
        return all(s.get("robotStationStatus") == StationStatus.WAIT_INSTALL for s in enabled)

    def wait_until(
        self,
        predicate: Callable[[], bool],
        timeout: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
        poll: float = 0.2,
    ) -> bool:
        deadline = None if timeout is None else (time.time() + float(timeout))
        while True:
            if predicate():
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(poll)

    def install(self, station_seq, action: int) -> Optional[Dict[str, Any]]:
        return self._calsys_request("install", {
            "robotId": self.robot_id,
            "stationSeq": str(station_seq),
            "action": int(action),
        })

    def uninstall(self, station_seq, action: int) -> Optional[Dict[str, Any]]:
        return self._calsys_request("unInstall", {
            "robotId": self.robot_id,
            "stationSeq": str(station_seq),
            "action": int(action),
        })

    def start_test(self) -> Optional[Dict[str, Any]]:
        return self._calsys_request("start", {"robotId": self.robot_id})

    def query_dutinfo(self, station_seq) -> Optional[Dict[str, Any]]:
        return self._calsys_request(
            "dutInfo",
            {"robotId": self.robot_id, "stationSeq": str(station_seq)},
            timeout=ConstTimeout.MQTT_DUTINFO,
        )

    def wait_dutinfo(
        self,
        station_seq,
        timeout: float = ConstTimeout.IDENTIFY_WAIT,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        key = str(station_seq)
        event = self._dut_event(key)
        deadline = time.time() + float(timeout)
        while True:
            with self._lock:
                cached = self._dut_by_station.get(key)
            if cached is not None:
                return cached
            remain = deadline - time.time()
            if remain <= 0:
                return None
            if stop_event is not None and stop_event.is_set():
                return None
            event.wait(timeout=min(0.5, remain))

    def take_dutinfo(self, station_seq) -> Optional[Dict[str, Any]]:
        key = str(station_seq)
        with self._lock:
            data = self._dut_by_station.pop(key, None)
            ev = self._dut_events.get(key)
            if ev is not None:
                ev.clear()
        return data

    def wait_end_notify(
        self,
        timeout: Optional[float] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        self._end_event.clear()
        with self._lock:
            self._end_notify = None
        self.wait_until(
            lambda: self._end_notify is not None,
            timeout=timeout, stop_event=stop_event, poll=0.2,
        )
        with self._lock:
            return dict(self._end_notify) if self._end_notify else None

    def clear_end_notify(self):
        with self._lock:
            self._end_notify = None
        self._end_event.clear()

    def reply_code(self, reply: Optional[Dict[str, Any]]) -> Optional[int]:
        if not reply:
            return None
        data = reply.get("data") if "data" in reply else reply
        if not isinstance(data, dict):
            return None
        if "code" not in data:
            return None
        try:
            return int(data.get("code"))
        except (TypeError, ValueError):
            return None

    def request_with_code_retry(
        self,
        sender: Callable[[], Optional[Dict[str, Any]]],
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        MQTT 超时已在底层重发。这里处理回复 code：
          0 成功；1 检定系统问题，再发最多 2 次；2 本机报文错误，不再重试。
        """
        last = None
        for attempt in range(ConstTimeout.CALSYS_CODE1_RETRIES + 1):
            if stop_event is not None and stop_event.is_set():
                return None
            last = sender()
            code = self.reply_code(last)
            if last is None:
                return None
            if code == 0:
                return last
            if code == 2:
                logger.error(_LOG, f"检定系统返回 code=2（本机报文错误）: {last}")
                return last
            if code == 1:
                logger.warning(_LOG, f"检定系统返回 code=1，第 {attempt + 1} 次")
                continue
            logger.warning(_LOG, f"检定系统返回未知 code={code}: {last}")
            return last
        return last

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _calsys_request(self, msg_type: str, data: Dict[str, Any], timeout: float = None) -> Optional[Dict[str, Any]]:
        calsys_id = self.calsys_id
        if not calsys_id:
            logger.error(_LOG, f"尚未发现 calsysId，无法发送 {msg_type}")
            return None
        payload = {"requestId": new_request_id(), "data": data}
        topic = f"v1/ras/calsys/{calsys_id}/{msg_type}"
        logger.info(_LOG, "发送上位机 %s → %s %s" % (msg_type, topic, json.dumps(data, ensure_ascii=False)))
        reply = self._client.request(
            topic, payload,
            timeout=ConstTimeout.MQTT_REPLY if timeout is None else timeout,
            retries=ConstTimeout.MQTT_RETRIES,
        )
        if reply is None:
            logger.warning(_LOG, "上位机无回复 %s topic=%s" % (msg_type, topic))
        else:
            logger.info(_LOG, "收到上位机 %s %s" % (msg_type, json.dumps(reply, ensure_ascii=False)))
        return reply

    def _heartbeat_loop(self):
        while not self._hb_stop.is_set():
            try:
                self._publish_live()
            except Exception as e:  # noqa: BLE001
                logger.warning(_LOG, f"心跳发送失败: {e}")
            self._hb_stop.wait(timeout=1.0)

    def _publish_live(self):
        battery = 100
        if self._get_battery is not None:
            try:
                battery = int(self._get_battery())
            except Exception:
                battery = 100
        with self._lock:
            status = self._live_status
            station_seq = self._station_seq
            need_human = self.need_human
            human_reason = self.human_reason
            calsys_id = self.calsys_id
        data = {
            "robotId": self.robot_id,
            "timestamp": int(time.time() * 1000),
            "battery": battery,
            "status": status,
            "calsysId": calsys_id,
            "stationSeq": station_seq if station_seq is not None else 0,
            "needHuman": bool(need_human),
            "humanReason": human_reason or "",
        }
        self._client.publish(
            f"v1/ras/robot/{self.robot_id}/live",
            {"requestId": new_request_id(), "data": data},
            qos=0,
        )

    def _dut_event(self, key: str) -> threading.Event:
        with self._lock:
            ev = self._dut_events.get(key)
            if ev is None:
                ev = threading.Event()
                self._dut_events[key] = ev
            return ev

    def _on_info_reply(self, topic: str, payload: Dict[str, Any]):  # noqa: ARG002
        data = payload.get("data") or {}
        calsys_id = str(data.get("calsysId") or "")
        if calsys_id and not self.calsys_id:
            with self._lock:
                self.calsys_id = calsys_id
            logger.info(_LOG, f"info_reply 写入 calsysId={calsys_id}")

    def _on_robot_info_query(self, topic: str, payload: Dict[str, Any]):  # noqa: ARG002
        request_id = payload.get("requestId") or new_request_id()
        logger.info(_LOG, "收到上位机查询机器人信息 requestId=%s" % request_id)
        self._client.publish(
            "v1/ras/robot/info_reply",
            {"requestId": request_id, "data": dict(self.robot_info)},
        )

    def _on_calsys_live(self, topic: str, payload: Dict[str, Any]):  # noqa: ARG002
        data = payload.get("data") or {}
        calsys_id = str(data.get("calsysId") or "")
        if self.calsys_id and calsys_id and calsys_id != self.calsys_id:
            return
        fp = (
            bool(data.get("isRunning")),
            bool(data.get("isPendingConfirm")),
            tuple(
                (station_seq_of(s), s.get("robotStationStatus"), s.get("isEnabled"))
                for s in (data.get("stations") or [])
            ),
        )
        with self._lock:
            if calsys_id and not self.calsys_id:
                self.calsys_id = calsys_id
            self._calsys_live = data
            changed = fp != self._live_fp
            self._live_fp = fp
        self._calsys_live_event.set()
        if changed:
            stations = [
                "%s:%s" % (station_seq_of(s), s.get("robotStationStatus"))
                for s in (data.get("stations") or []) if s.get("isEnabled")
            ]
            logger.info(
                _LOG,
                "检定系统状态变化 isRunning=%s isPendingConfirm=%s stations=%s"
                % (data.get("isRunning"), data.get("isPendingConfirm"), stations),
            )

    def _on_dutinfo_notify(self, topic: str, payload: Dict[str, Any]):  # noqa: ARG002
        data = payload.get("data") or {}
        seq = str(data.get("stationSeq") or "")
        if not seq:
            return
        with self._lock:
            self._dut_by_station[seq] = data
        self._dut_event(seq).set()
        logger.info(_LOG, "收到识别/检漏通知 %s" % json.dumps(data, ensure_ascii=False))

    def _on_end_notify(self, topic: str, payload: Dict[str, Any]):  # noqa: ARG002
        data = payload.get("data") or {}
        calsys_id = str(data.get("calsysId") or "")
        if self.calsys_id and calsys_id and calsys_id != self.calsys_id:
            return
        with self._lock:
            self._end_notify = data
        self._end_event.set()
        logger.info(_LOG, "收到检定结束通知 %s" % json.dumps(data, ensure_ascii=False))
