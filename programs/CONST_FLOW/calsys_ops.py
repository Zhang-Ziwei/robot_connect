"""
上位机工位协议：让位/合龙、拆表开合、等识别/检漏。

只走 MQTT 适配器。不调机器人，不做重插次数、剩余表数等业务判断。
"""

from __future__ import annotations

from typing import Optional, Set

from infrastructure.error_logger import get_error_logger
from core.flow_engine import FlowContext

from programs.CONST_FLOW.constants import ConstTimeout, HumanReason, StationStatus
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter

logger = get_error_logger()
_LOG = "CONST_CALSYS"


def _stop_event(ctx: FlowContext):
    return ctx.extra.get("stop_event")


def expected_live_statuses(kind: str, action: int) -> Set[str]:
    """规范：让位后界面闪「正在装/拆表」，合龙后装表变已装表、拆表变待装表。"""
    if kind == "install":
        if int(action) == 0:
            return {StationStatus.INSTALLING}
        return {StationStatus.INSTALLED, StationStatus.LEAK_TEST}
    if int(action) == 0:
        return {StationStatus.UNINSTALLING}
    return {StationStatus.WAIT_INSTALL}


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


def _need_human(adapter: ConSTMqttAdapter, ctx: FlowContext, reason: str) -> str:
    ctx.set("human_reason", reason)
    adapter.call_human(reason)
    return "human"


def conclude_station_action(
    adapter: ConSTMqttAdapter,
    seq,
    kind: str,
    action: int,
    reply: Optional[dict],
    ctx: FlowContext,
) -> str:
    """
    点对点回复优先。无回包时再看 live 工位状态：上位机有时已经合龙/让位，
    只是 unInstall_reply 晚到或主题对不上，这时不该呼人工。
    """
    if reply is not None:
        outcome = apply_reply(adapter, reply)
        if outcome == "human":
            ctx.set("human_reason", adapter.human_reason or HumanReason.CALSYS_ERROR)
        return outcome

    wanted = expected_live_statuses(kind, action)
    logger.warning(
        _LOG,
        f"上位机 {kind} action={action} 工位 {seq} 点对点无回复，改等 live {sorted(wanted)} "
        f"（{ConstTimeout.MQTT_LIVE_CONFIRM}s）",
    )
    if adapter.wait_station_status(
        seq, wanted,
        timeout=ConstTimeout.MQTT_LIVE_CONFIRM,
        stop_event=_stop_event(ctx),
    ):
        logger.info(
            _LOG,
            f"工位 {seq} live 已是 {adapter.station_status(seq)}，视为 {kind} action={action} 成功",
        )
        return "ok"
    return _need_human(adapter, ctx, HumanReason.MQTT_TIMEOUT)


def install_action(adapter: ConSTMqttAdapter, seq, action: int, ctx: FlowContext) -> str:
    logger.info(_LOG, f"上位机装表工位 {seq} action={action}")
    reply = adapter.request_with_code_retry(
        lambda: adapter.install(seq, action),
        stop_event=_stop_event(ctx),
    )
    return conclude_station_action(adapter, seq, "install", action, reply, ctx)


def uninstall_action(adapter: ConSTMqttAdapter, seq, action: int, ctx: FlowContext) -> str:
    logger.info(_LOG, f"上位机拆表工位 {seq} action={action}")
    reply = adapter.request_with_code_retry(
        lambda: adapter.uninstall(seq, action),
        stop_event=_stop_event(ctx),
    )
    return conclude_station_action(adapter, seq, "uninstall", action, reply, ctx)


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
