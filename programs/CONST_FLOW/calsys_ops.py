"""
上位机工位协议：让位/合龙、拆表开合、等识别/检漏。

只走 MQTT 适配器。不调机器人，不做重插次数、剩余表数等业务判断。
"""

from __future__ import annotations

from typing import Optional

from infrastructure.error_logger import get_error_logger
from core.flow_engine import FlowContext

from programs.CONST_FLOW.constants import ConstTimeout, HumanReason
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter

logger = get_error_logger()
_LOG = "CONST_CALSYS"


def _stop_event(ctx: FlowContext):
    return ctx.extra.get("stop_event")


def apply_reply(adapter: ConSTMqttAdapter, reply: Optional[dict]) -> str:
    """把上位机回复收成 ok / human。超时或 code!=0 会呼叫人工。"""
    code = adapter.reply_code(reply)
    if reply is None:
        adapter.call_human(HumanReason.MQTT_TIMEOUT)
        return "human"
    if code != 0:
        adapter.call_human(HumanReason.CALSYS_ERROR)
        return "human"
    return "ok"


def install_action(adapter: ConSTMqttAdapter, seq, action: int, ctx: FlowContext) -> str:
    logger.info(_LOG, f"上位机装表工位 {seq} action={action}")
    reply = adapter.request_with_code_retry(
        lambda: adapter.install(seq, action),
        stop_event=_stop_event(ctx),
    )
    return apply_reply(adapter, reply)


def uninstall_action(adapter: ConSTMqttAdapter, seq, action: int, ctx: FlowContext) -> str:
    logger.info(_LOG, f"上位机拆表工位 {seq} action={action}")
    reply = adapter.request_with_code_retry(
        lambda: adapter.uninstall(seq, action),
        stop_event=_stop_event(ctx),
    )
    return apply_reply(adapter, reply)


def wait_identify(adapter: ConSTMqttAdapter, seq, ctx: FlowContext) -> Optional[dict]:
    """先等 dutinfo_notify；超时再主动查询 dutInfo。"""
    logger.info(_LOG, f"等待工位 {seq} 识别/检漏通知 timeout={ConstTimeout.IDENTIFY_WAIT}s")
    dut = adapter.wait_dutinfo(
        seq, timeout=ConstTimeout.IDENTIFY_WAIT, stop_event=_stop_event(ctx),
    )
    if dut is not None:
        return dut
    logger.info(_LOG, f"工位 {seq} 未收到通知，主动查询 dutInfo")
    dut_reply = adapter.query_dutinfo(seq)
    if not dut_reply:
        return None
    data = dut_reply.get("data") if isinstance(dut_reply, dict) else None
    return data if isinstance(data, dict) else None
