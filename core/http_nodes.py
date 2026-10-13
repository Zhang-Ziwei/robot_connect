"""通用 HTTP 流程节点：明确验证业务完成结果，不重试有副作用的请求。"""

from __future__ import annotations

import json
import math
import threading
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests

from core.flow_engine import FlowContext, FlowNode
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()
_LOG = "HTTPNode"
_MISSING = object()
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _render_json(value: Any, ctx: FlowContext, name: str) -> Any:
    """先解析 JSON 再渲染，保留 {{cube}} 等完整模板的数字/布尔类型。"""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except ValueError:
            rendered = ctx.render(value)
            if isinstance(rendered, str):
                try:
                    rendered = json.loads(rendered)
                except ValueError as exc:
                    raise ValueError(f"{name} 必须是合法 JSON") from exc
            value = rendered
    return ctx.render(value)


def _unresolved(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "}}" in value
    if isinstance(value, dict):
        return any(_unresolved(item) for item in value.values())
    if isinstance(value, list):
        return any(_unresolved(item) for item in value)
    return False


def _lookup(body: Any, path: str) -> Any:
    current = body
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _same_json_value(actual: Any, expected: Any) -> bool:
    # Python 中 True == 1；完成布尔值必须避免这种隐式等价。
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def build_http_handlers(
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Callable]:
    def handle_send_http(node: FlowNode, ctx: FlowContext) -> bool:
        stopped = ctx.extra.get("stop_event") or stop_event
        result_var = ctx.render(node.params.get("result_var", ""))
        ctx.set("last_http_success", False)
        ctx.set("last_http_status", None)
        ctx.set("last_http_error", "")
        if isinstance(result_var, str) and result_var.strip():
            ctx.set(result_var.strip(), None)

        def fail(message: str) -> bool:
            ctx.set("last_http_error", message)
            logger.error(_LOG, f"节点 {node.id}: {message}")
            return False

        if stopped is not None and stopped.is_set():
            return fail("流程已停止")
        try:
            params = ctx.render(node.params)
            method = str(params.get("method", "POST")).strip().upper()
            if method not in _METHODS:
                raise ValueError(f"不支持的 HTTP method: {method}")
            timeout = float(params.get("timeout", 30))
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("timeout 必须是大于 0 的有限秒数")
            raw_headers = node.params.get("headers")
            headers = _render_json({} if raw_headers is None or raw_headers == "" else raw_headers,
                                   ctx, "headers")
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not key.strip() or not isinstance(value, str)
                for key, value in headers.items()
            ):
                raise ValueError("headers 必须是字符串键值组成的 JSON 对象")
            body = _render_json(node.params.get("body"), ctx, "body")
            if body is not None and not isinstance(body, (dict, list)):
                raise ValueError("body 必须是 JSON 对象、数组或 null")
            json.dumps(body, allow_nan=False)
            success_field = params.get("success_field")
            success_value = params.get("success_value")

            if result_var is not None and not isinstance(result_var, str):
                raise ValueError("result_var 必须是变量名字符串")
            if isinstance(result_var, str):
                result_var = result_var.strip()

            if ctx.extra.get("dryrun"):
                # 演练允许未填设备地址和动作映射，绝不发送 HTTP。
                path = success_field.strip() if isinstance(success_field, str) and success_field.strip() else "success"
                simulated: Dict[str, Any] = {}
                current = simulated
                parts = path.split(".")
                for part in parts[:-1]:
                    current[part] = {}
                    current = current[part]
                current[parts[-1]] = success_value if success_value is not None else True
                if result_var:
                    ctx.set(result_var, simulated)
                ctx.set("last_http_status", 200)
                ctx.set("last_http_success", True)
                logger.info(_LOG, f"演练跳过 HTTP 请求: {node.id}")
                return True

            url = params.get("url")
            if not isinstance(url, str) or _unresolved(url):
                raise ValueError("未配置有效 HTTP URL")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("URL 必须是完整的 http/https 地址")
            if params.get("response_mode", "completed") != "completed":
                raise ValueError("仅支持响应表示动作已完成的 completed 模式")
            if not isinstance(success_field, str) or not success_field.strip() or _unresolved(success_field):
                raise ValueError("必须配置表示动作完成的 success_field")
            if "success_value" not in params or success_value is None or _unresolved(success_value):
                raise ValueError("必须配置表示动作完成的 success_value")
            if _unresolved(body) or _unresolved(headers):
                raise ValueError("请求仍包含未解析的模板")
            if isinstance(body, dict) and "task" in body:
                task = body["task"]
                if not isinstance(task, str) or not task.strip():
                    raise ValueError("task 不能为空，必须配置真实动作名称")
            if stopped is not None and stopped.is_set():
                return fail("流程已停止")

            response = requests.request(
                method, url, headers=headers or None, json=body,
                timeout=timeout, allow_redirects=False,
            )
            ctx.set("last_http_status", response.status_code)
            if stopped is not None and stopped.is_set():
                return fail("HTTP 返回前流程已停止，未确认动作完成")
            if not 200 <= response.status_code < 300 or response.status_code == 202:
                return fail(f"HTTP {response.status_code} 未确认动作完成")
            try:
                response_body = response.json()
            except ValueError:
                return fail("响应不是 JSON，无法确认动作完成")
            if result_var:
                ctx.set(result_var.strip(), response_body)
            actual = _lookup(response_body, success_field.strip())
            if actual is _MISSING:
                return fail(f"响应缺少完成字段 {success_field}")
            if not _same_json_value(actual, success_value):
                return fail(f"响应字段 {success_field} 未达到指定完成值")
            ctx.set("last_http_success", True)
            return True
        except (ValueError, TypeError, OverflowError, requests.RequestException) as exc:
            return fail(f"HTTP 请求或参数错误: {exc}")

    return {"send_http": handle_send_http}


NODE_TYPE_SCHEMAS = {
    "send_http": {
        "label": "发送 HTTP 指令",
        "category": "HTTP 通信",
        "fields": [
            {"name": "url", "type": "text", "label": "请求 URL", "required": True},
            {"name": "method", "type": "select", "label": "请求方法", "default": "POST",
             "options": sorted(_METHODS)},
            {"name": "headers", "type": "json", "label": "请求头", "default": {}},
            {"name": "body", "type": "json", "label": "JSON 请求体", "default": {}},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 30},
            {"name": "success_field", "type": "text", "label": "动作完成字段", "required": True,
             "hint": "JSON 点路径，例如 data.completed；必须表示动作已完成"},
            {"name": "success_value", "type": "json", "label": "动作完成值", "required": True,
             "hint": "例如 true、0 或字符串；仅收到/接受指令不代表完成"},
            {"name": "response_mode", "type": "select", "label": "响应语义",
             "options": ["completed"], "default": "completed"},
            {"name": "result_var", "type": "text", "label": "完整响应保存到变量"},
        ],
        "outputs": ["success", "failure"],
    },
}
