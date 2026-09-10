"""
装表/拆表的业务判断：重插次数、识别/检漏是否通过、呼叫人工、剩余表数。

上位机往来走 calsys_ops；机器人动作走 robot_ops。本模块只编排这两者。
"""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

from infrastructure.error_logger import get_error_logger
from core.flow_engine import FlowContext

from programs.CONST_FLOW.constants import (
    ConstTimeout, HumanReason, PoseType, RobotLiveStatus,
)
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter, station_seq_of
from programs.CONST_FLOW import calsys_ops, robot_ops

logger = get_error_logger()
_LOG = "CONST_LOGIC"


def _stop_event(ctx: FlowContext) -> Optional[threading.Event]:
    return ctx.extra.get("stop_event")


def _sleep(ctx: FlowContext, seconds: float) -> bool:
    ev = _stop_event(ctx)
    if ev is None:
        time.sleep(max(0.0, seconds))
        return True
    return not ev.wait(timeout=max(0.0, seconds))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def dut_ok(data: Optional[dict]) -> bool:
    if not data:
        return False
    try:
        code = int(data.get("code", 1))
    except (TypeError, ValueError):
        return False
    if code != 0:
        return False
    dut = data.get("dut") or {}
    return bool(dut.get("leakTestPassed", True))


def install_once(ctx: FlowContext, robot, adapter: ConSTMqttAdapter, seq, pose, timeout) -> str:
    """一次装表：上位机让位 → 等龙门架 → 机器人装表 → 上位机合龙 → 等识别/检漏。"""
    adapter.set_status(RobotLiveStatus.INSTALL, seq)
    adapter.take_dutinfo(seq)

    if calsys_ops.install_action(adapter, seq, 0, ctx) == "human":
        return "human"
    if not _sleep(ctx, ConstTimeout.GANTRY_WAIT):
        return "fail"

    ok, parsed = robot_ops.install_gauge(ctx, robot, pose, timeout)
    if ok and not robot_ops.can_reinsert(parsed):
        adapter.call_human(HumanReason.GAUGE_DROPPED)
        return "human"
    if not ok:
        return "retry"

    if calsys_ops.install_action(adapter, seq, 1, ctx) == "human":
        return "human"

    adapter.set_status(RobotLiveStatus.WAIT_CALSYS, seq)
    dut = calsys_ops.wait_identify(adapter, seq, ctx)
    logger.info(_LOG, f"工位 {seq} 识别/检漏结果 dut={dut}")
    if dut_ok(dut):
        return "ok"
    logger.warning(_LOG, f"工位 {seq} 识别或检漏未通过")
    return "retry"


def uninstall_once(ctx, robot, adapter: ConSTMqttAdapter, seq, pose, extra, timeout) -> str:
    adapter.set_status(RobotLiveStatus.UNINSTALL, seq)
    if calsys_ops.uninstall_action(adapter, seq, 0, ctx) == "human":
        return "human"
    ok, parsed = robot_ops.uninstall_gauge(ctx, robot, pose, extra, timeout)
    if ok and not robot_ops.can_reinsert(parsed):
        adapter.call_human(HumanReason.GAUGE_DROPPED)
        return "human"
    if not ok:
        return "fail"
    if calsys_ops.uninstall_action(adapter, seq, 1, ctx) == "human":
        return "human"
    return "ok"


def _consume_one_gauge(ctx: FlowContext):
    ctx.set("remaining", max(0, _as_int(ctx.get("remaining"), 0) - 1))


def install_station(ctx: FlowContext, robot, adapter: ConSTMqttAdapter, seq, pose, timeout) -> bool:
    logger.info(_LOG, f"装表工位 seq={seq} pose={pose} remaining={ctx.get('remaining')}")
    if not robot_ops.nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
        return False
    retries = ConstTimeout.IDENTIFY_RETRIES
    outcome = "fail"
    for attempt in range(retries + 1):
        if attempt > 0:
            logger.warning(_LOG, f"工位 {seq} 识别/检漏失败，第 {attempt} 次重插")
            u = uninstall_once(ctx, robot, adapter, seq, pose, {"reinstall": True}, timeout)
            if u == "human":
                outcome = "human"
                break
        outcome = install_once(ctx, robot, adapter, seq, pose, timeout)
        if outcome in ("ok", "human"):
            break
    if outcome == "human":
        ctx.set("need_human", True)
        adapter.wait_human(_stop_event(ctx))
        ctx.set("need_human", False)
        _consume_one_gauge(ctx)
        return True
    if outcome != "ok":
        logger.warning(_LOG, f"工位 {seq} 超过 {retries} 次重插，按坏表拆回后部料箱")
        uninstall_once(ctx, robot, adapter, seq, pose, {"is_passed": False}, timeout)
        _consume_one_gauge(ctx)
        return True
    installed: List[int] = list(ctx.get("installed_stations") or [])
    if seq not in installed:
        installed.append(seq)
    _consume_one_gauge(ctx)
    ctx.set("installed_stations", installed)
    ctx.set("placed_this_round", _as_int(ctx.get("placed_this_round"), 0) + 1)
    logger.info(
        _LOG,
        f"工位 {seq} 装表成功 installed={installed} remaining={ctx.get('remaining')}",
    )
    return True


def reinstall_all(ctx: FlowContext, robot, adapter: ConSTMqttAdapter, timeout: float) -> bool:
    for seq in list(ctx.get("installed_stations") or []):
        pose = PoseType.station(seq)
        if not robot_ops.nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
            return False
        u = uninstall_once(ctx, robot, adapter, seq, pose, {"reinstall": True}, timeout)
        if u == "human":
            adapter.wait_human(_stop_event(ctx))
        i = install_once(ctx, robot, adapter, seq, pose, timeout)
        if i == "human":
            adapter.wait_human(_stop_event(ctx))
        if i != "ok":
            logger.error(_LOG, f"泄漏后重装工位 {seq} 失败")
            return False
    return True


def uninstall_all(ctx: FlowContext, robot, adapter: ConSTMqttAdapter, timeout: float) -> bool:
    details = {}
    for d in (ctx.get("end_station_details") or []):
        seq = station_seq_of(d, -1)
        details[seq] = bool(d.get("isPassed"))
    for seq in list(ctx.get("installed_stations") or []):
        pose = PoseType.station(seq)
        is_passed = details.get(int(seq), False)
        if not robot_ops.nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
            return False
        u = uninstall_once(ctx, robot, adapter, seq, pose, {"is_passed": is_passed}, timeout)
        if u == "human":
            adapter.wait_human(_stop_event(ctx))
        if u not in ("ok", "human"):
            logger.error(_LOG, f"拆表工位 {seq} 失败")
            return False
    ctx.set("installed_stations", [])
    ctx.set("placed_this_round", 0)
    return True
