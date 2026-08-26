"""
通用 HTTP 客户端

对 requests 库做轻量封装，提供统一的请求发送、错误处理和响应解析逻辑。
供各业务模块（如 AJL 色谱仪客户端）继承或直接实例化使用。

设计原则：
  - 只做 HTTP 通信，不含任何业务逻辑
  - 支持自定义"成功判断"策略（默认 statusCode == 0）
  - 网络/HTTP 异常统一捕获，返回结构化的 HttpResult
"""

import requests
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


# ──────────────────────────────────────────────────────────────────────────────
# 响应结构
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class HttpResult:
    """
    HTTP 请求结果。

    属性:
        success   — 业务层成功（由 success_checker 判定）
        message   — 成功时为 "success"，失败时为错误描述
        data      — 响应体中的 data 字段（业务数据），失败时为 None
        status_code — HTTP 状态码，网络异常时为 -1
        raw       — 完整响应 JSON（dict），网络异常时为 {}
    """
    success: bool
    message: str
    data: Any = None
    status_code: int = -1
    raw: Dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.success


# ──────────────────────────────────────────────────────────────────────────────
# 默认成功判断策略
# ──────────────────────────────────────────────────────────────────────────────

def _default_success_checker(body: dict) -> tuple:
    """
    默认策略：响应体中 statusCode == 0 视为成功。
    返回 (success: bool, message: str)。
    """
    code = body.get("statusCode", -1)
    if code == 0:
        return True, "success"
    msg = body.get("errorMessage") or body.get("message") or f"statusCode={code}"
    return False, msg


# ──────────────────────────────────────────────────────────────────────────────
# 通用 HTTP 客户端
# ──────────────────────────────────────────────────────────────────────────────

class HttpClient:
    """
    通用 HTTP 客户端。

    用法一（直接实例化）：
        client = HttpClient(base_url="http://192.168.1.100")
        result = client.post("/api/v1/foo", json_body={"key": "val"})
        if result:
            print(result.data)

    用法二（继承，覆盖 _success_checker 定制成功判断）：
        class MyClient(HttpClient):
            def _success_checker(self, body):
                return body.get("code") == 200, body.get("msg", "")

    参数:
        base_url:        服务器根地址，如 "http://192.168.1.100:8080"
        timeout:         单次请求超时（秒），默认 30
        default_headers: 每次请求附加的默认 Header（如 Authorization）
        success_checker: 自定义成功判断函数 (body: dict) -> (bool, str)
                         不传则使用默认的 statusCode == 0 策略
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        default_headers: Optional[Dict[str, str]] = None,
        success_checker: Optional[Callable[[dict], tuple]] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._default_headers = default_headers or {}
        self._success_checker: Callable[[dict], tuple] = (
            success_checker or _default_success_checker
        )

    # ── 公共请求方法 ──────────────────────────────────────────────────────────

    def post(
        self,
        path: str,
        params: Optional[Dict] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        """
        发送 POST 请求。

        参数:
            path:      相对路径，如 "/api/v1/Sample/batch"
            params:    URL Query 参数（dict）
            json_body: 请求体（自动序列化为 JSON）
            headers:   额外 Header（与 default_headers 合并）

        返回: HttpResult
        """
        return self._request("POST", path, params=params, json_body=json_body, headers=headers)

    def get(
        self,
        path: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        """发送 GET 请求，返回 HttpResult。"""
        return self._request("GET", path, params=params, headers=headers)

    def put(
        self,
        path: str,
        params: Optional[Dict] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        """发送 PUT 请求，返回 HttpResult。"""
        return self._request("PUT", path, params=params, json_body=json_body, headers=headers)

    # ── 内部实现 ──────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        url = f"{self._base_url}{path}"
        merged_headers = {**self._default_headers, **(headers or {})}

        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=merged_headers or None,
                timeout=self._timeout,
            )
            resp.raise_for_status()

            try:
                body: dict = resp.json()
            except ValueError:
                # 响应体不是 JSON（如空 body）
                logger.warning("HttpClient", f"响应体非 JSON [{method} {path}]")
                return HttpResult(
                    success=True,
                    message="success (no json body)",
                    status_code=resp.status_code,
                )

            success, msg = self._success_checker(body)
            if not success:
                logger.error("HttpClient", f"业务失败 [{method} {path}]: {msg}")
            return HttpResult(
                success=success,
                message=msg,
                data=body.get("data"),
                status_code=resp.status_code,
                raw=body,
            )

        except requests.HTTPError as e:
            msg = f"HTTP 错误 [{method} {path}]: {e}"
            logger.error("HttpClient", msg)
            return HttpResult(success=False, message=msg)

        except requests.ConnectionError as e:
            msg = f"连接失败 [{method} {path}]: {e}"
            logger.error("HttpClient", msg)
            return HttpResult(success=False, message=msg)

        except requests.Timeout:
            msg = f"请求超时 [{method} {path}] (>{self._timeout}s)"
            logger.error("HttpClient", msg)
            return HttpResult(success=False, message=msg)

        except requests.RequestException as e:
            msg = f"请求异常 [{method} {path}]: {e}"
            logger.error("HttpClient", msg)
            return HttpResult(success=False, message=msg)
