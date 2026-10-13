"""Protocol-independent PLC nodes for the graphical flow editor."""

from __future__ import annotations

from infrastructure.error_logger import get_error_logger
from plc_controller.common_plc import PlcConfigurationError, bool_value, client_from_config


logger = get_error_logger()


def build_plc_handlers(client=None, stop_event=None):
    """Clients are lazy; dry runs never construct one or access the PLC."""

    def param(node, ctx, name, default=None):
        raw = node.params.get(name)
        if raw is None or raw == "":
            return default
        value = ctx.render(raw)
        return default if value is None or value == "" else value

    def execute(kind, node, ctx):
        try:
            stop = ctx.extra.get("stop_event") or stop_event
            if stop is not None and stop.is_set():
                raise PlcConfigurationError("PLC 操作已停止")
            point = param(node, ctx, "command" if kind == "handshake" else "point")
            if not isinstance(point, str) or not point.strip():
                raise PlcConfigurationError("PLC 节点必须填写点名")
            value = param(node, ctx, "value")
            ctx.set("last_plc_point", point)
            ctx.set("last_plc_error", "")
            count = param(node, ctx, "count") if kind == "read" else None
            if ctx.extra.get("dryrun"):
                if kind == "read":
                    value = param(node, ctx, "dryrun_value", 0)
                    if count is not None and not isinstance(value, list):
                        value = [value] * int(count)
                    ctx.set(param(node, ctx, "variable", "last_plc_value"), value)
                ctx.set("last_plc_value", value)
                ctx.set("last_plc_success", True)
                logger.info("PLC", f"演练 {kind}: {point}，未访问 PLC")
                return True
            plc = client or ctx.extra.get("plc_client")
            if plc is None:
                plc = client_from_config(ctx.extra.get("plc_config"))
            if kind == "read":
                value = plc.read(point) if count is None else plc.read_block(point, count)
                ctx.set(param(node, ctx, "variable", "last_plc_value"), value)
            elif kind == "write":
                if plc.write(point, value) is False:
                    raise RuntimeError(f"PLC 写入 {point} 失败")
            elif kind == "wait":
                if plc.wait(point, value, timeout=param(node, ctx, "timeout", 120),
                            stop_event=stop, poll=param(node, ctx, "poll", 0.2)) is False:
                    raise RuntimeError(f"PLC 等待 {point} 失败")
            else:
                if plc.handshake(
                    point, param(node, ctx, "state"),
                    ready_value=param(node, ctx, "ready_value"),
                    done_value=param(node, ctx, "done_value"),
                    timeout=param(node, ctx, "timeout", 120),
                    reset_after=bool_value(param(node, ctx, "reset_after", True)),
                    stop_event=stop, poll=param(node, ctx, "poll", 0.2),
                    pulse_seconds=param(node, ctx, "pulse_seconds", 0.2),
                ) is False:
                    raise RuntimeError(f"PLC 握手 {point} 失败")
            if stop is not None and stop.is_set():
                raise RuntimeError("PLC 操作已停止")
            ctx.set("last_plc_value", value)
            ctx.set("last_plc_success", True)
            return True
        except Exception as exc:
            ctx.set("last_plc_success", False)
            ctx.set("last_plc_error", str(exc))
            logger.error("PLC", f"节点 {node.id} 失败: {exc}")
            return False

    return {
        "plc_read": lambda node, ctx: execute("read", node, ctx),
        "plc_write": lambda node, ctx: execute("write", node, ctx),
        "plc_wait": lambda node, ctx: execute("wait", node, ctx),
        "plc_handshake": lambda node, ctx: execute("handshake", node, ctx),
    }


_POINT = {"name": "point", "type": "text", "label": "PLC 点名", "required": True,
          "hint": "robot_config.json 中 plc.points 的名称；协议地址在点位配置中维护"}
_TIMEOUT = {"name": "timeout", "type": "number", "label": "超时秒数", "default": 120}
_POLL = {"name": "poll", "type": "number", "label": "查询间隔秒数", "default": 0.2}
NODE_TYPE_SCHEMAS = {
    "plc_read": {
        "label": "PLC 读取", "category": "PLC",
        "fields": [_POINT, {"name": "variable", "type": "text", "label": "保存变量名",
                            "default": "last_plc_value"},
                   {"name": "count", "type": "number", "label": "连续读取点数",
                    "hint": "留空读单点。填 N 时从本点起一次性读 N 个连续 INT 并存成列表；"
                            "二维码这类成组数据必须一次读完，逐点读会拼出新旧混合的值"},
                   {"name": "dryrun_value", "type": "json", "label": "演练返回值", "default": 0}],
        "outputs": ["success", "failure"],
    },
    "plc_write": {
        "label": "PLC 写入", "category": "PLC",
        "fields": [_POINT, {"name": "value", "type": "json", "label": "写入值", "required": True,
                            "hint": "BOOL 填 true/false，INT 填整数；本节点保持输出，脉冲请选择 PLC 握手"}],
        "outputs": ["success", "failure"],
    },
    "plc_wait": {
        "label": "PLC 等待状态", "category": "PLC",
        "fields": [_POINT, {"name": "value", "type": "json", "label": "目标状态", "required": True},
                   _TIMEOUT, _POLL],
        "outputs": ["success", "failure"],
    },
    "plc_handshake": {
        "label": "PLC 指令握手", "category": "PLC",
        "fields": [
            {"name": "command", "type": "text", "label": "BOOL 输出点名", "required": True},
            {"name": "state", "type": "text", "label": "状态输入点名"},
            {"name": "ready_value", "type": "number", "label": "置位前等待状态（可选）"},
            {"name": "done_value", "type": "number", "label": "完成状态（可选）",
             "hint": "配置时主动查询直到完成；留空时仅发送定时脉冲"},
            {"name": "reset_after", "type": "checkbox", "label": "成功后清零输出", "default": True,
             "hint": "停止、超时和异常也会尝试清零；清零失败报告错误"},
            {"name": "pulse_seconds", "type": "number", "label": "无完成状态时的脉冲秒数", "default": 0.2},
            _TIMEOUT, _POLL,
        ],
        "outputs": ["success", "failure"],
    },
}
