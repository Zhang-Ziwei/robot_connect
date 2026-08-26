"""
异步任务完成回调发送器

任务执行完毕（成功或失败）后，主动 POST 结果到外部系统。
配置来源：robot_config.json 的 callback 节。

设计原则：
  - 全项目通用，不含任何业务逻辑
  - 位于 handlers 层：知晓回调报文格式，复用 network.http_client 传输
  - 始终在后台线程中发送，不阻塞业务流程
  - 发送后等待对方 HTTP 响应，响应体须含 received=true 视为对方已收到
  - 超时或未确认时，按 retry_interval 间隔重发，最多 retry 次
  - enabled=false 或 url 未配置时静默跳过
"""

import threading
import time
from typing import Any, Dict, Optional

from infrastructure.config_loader import get_callback_config
from infrastructure.error_logger import get_error_logger
from network.http_client import HttpClient

logger = get_error_logger()


class CallbackSender:
    """
    通用任务回调发送器。

    使用方式：
        sender = CallbackSender()          # 从 robot_config 读取配置
        sender.send(
            cmd_id     = "PICK_BOX_TO_SP_001",
            cmd_type   = "PICK_BOX_TO_SP",
            success    = True,
            message    = "任务完成",
            data       = {...},            # 可选额外数据
        )

    回调 Body（POST JSON）：
        {
            "cmd_id":   "PICK_BOX_TO_SP_001",
            "cmd_type": "PICK_BOX_TO_SP",
            "success":  true,
            "message":  "任务完成",
            "data":     {...}
        }

    对方响应（HTTP 200，JSON）：
        { "received": true }
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        参数:
            config: 回调配置字典；为 None 时从 robot_config.json 自动读取。
                    支持字段：enabled / url / timeout / retry / retry_interval
        """
        cfg = config if config is not None else get_callback_config()
        self._enabled: bool = bool(cfg.get("enabled", False))
        self._url: str = (cfg.get("url") or "").strip()
        self._timeout: float = float(cfg.get("timeout", 10.0))
        self._retry: int = int(cfg.get("retry", 0))
        self._retry_interval: float = float(cfg.get("retry_interval", 10.0))

        if self._enabled and self._url:
            # 从 url 中拆出 base_url 和 path，供 HttpClient 使用
            from urllib.parse import urlparse
            parsed = urlparse(self._url)
            scheme = parsed.scheme or "http"
            host = parsed.hostname or ""
            port = parsed.port
            base = f"{scheme}://{host}" + (f":{port}" if port else "")
            self._path = parsed.path or "/"
            self._client = HttpClient(
                base_url=base,
                timeout=self._timeout,
                success_checker=self._callback_success_checker,
            )
            logger.info("CallbackSender", f"回调已启用: {self._url}")
        else:
            self._client = None
            self._path = "/"
            if self._enabled and not self._url:
                logger.warning("CallbackSender", "callback.enabled=true 但 url 为空，回调已禁用")

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def send(
        self,
        cmd_id: str,
        cmd_type: str,
        success: bool,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        在后台线程中发送回调（非阻塞）。

        参数:
            cmd_id:   触发回调的命令 ID
            cmd_type: 命令类型（如 PICK_BOX_TO_SP）
            success:  任务是否成功
            message:  结果描述文本
            data:     附加数据（可选）
        """
        if not self._enabled or self._client is None:
            return

        body: Dict[str, Any] = {
            "cmd_id":   cmd_id,
            "cmd_type": cmd_type,
            "success":  success,
            "message":  message,
        }
        if data:
            body["data"] = data

        t = threading.Thread(
            target=self._send_with_retry,
            args=(body,),
            daemon=True,
            name=f"callback-{cmd_id}",
        )
        t.start()

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _callback_success_checker(body: dict) -> tuple:
        """
        回调接收方成功确认：响应 JSON 中 received 须为 true。
        """
        if body.get("received") is True:
            return True, "received"
        msg = body.get("message") or f"对方未确认收到(received={body.get('received')!r})"
        return False, msg

    def _send_with_retry(self, body: Dict[str, Any]) -> None:
        cmd_id = body.get("cmd_id", "")
        success = body.get("success")
        attempts = self._retry + 1
        logger.info(
            "CallbackSender",
            f"[{cmd_id}] 准备发送回调 body.success={success!r} "
            f"cmd_type={body.get('cmd_type')!r} message={body.get('message')!r}",
        )
        print(
            f"[{cmd_id}] 准备发送回调 success={success!r} "
            f"cmd_type={body.get('cmd_type')!r} message={body.get('message')!r}"
        )
        for attempt in range(1, attempts + 1):
            result = self._client.post(self._path, json_body=body)
            if result.success:
                ack_body = result.raw or {}
                logger.info(
                    "CallbackSender",
                    f"[{cmd_id}] 回调已发送且对方已确认收到 "
                    f"(attempt={attempt}, body.success={success!r}, "
                    f"received={ack_body.get('received')}, response={ack_body})",
                )
                print(
                    f"[{cmd_id}] 回调已发送且对方已确认收到 "
                    f"(attempt={attempt}), body.success={success!r}, "
                    f"received={ack_body.get('received')}, response={ack_body}"
                )
                return
            logger.warning(
                "CallbackSender",
                f"[{cmd_id}] 回调未获确认 (attempt={attempt}/{attempts}): {result.message}",
            )
            if attempt < attempts:
                time.sleep(self._retry_interval)

        logger.error("CallbackSender", f"[{cmd_id}] 回调全部失败，已放弃")


# ── 全局单例 ──────────────────────────────────────────────────────────────────

_sender: Optional[CallbackSender] = None


def reset_callback_sender() -> None:
    """丢弃全局单例，下次 get_callback_sender() 将从最新配置重建。"""
    global _sender
    _sender = None


def get_callback_sender() -> CallbackSender:
    """获取全局 CallbackSender 单例（首次调用时从配置文件初始化）。"""
    global _sender
    if _sender is None:
        _sender = CallbackSender()
    return _sender
