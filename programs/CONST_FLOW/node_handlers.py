"""CONST_FLOW 节点处理器。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from infrastructure.config_loader import load_config
from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    build_navigation_goal, send_navigation_action, is_robot_at_pose,
)
from hardware.task_utils import send_task_action
from core.flow_engine import FlowNode, FlowContext

from programs.CONST_FLOW.constants import (
    NavigationPose, ConstNavTolerance, ConstService, ConstTimeout, ConstTask,
    CONST_TASK_ACTION_SPEC, PoseType, RobotLiveStatus, HumanReason,
)
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter

logger = get_error_logger()
_LOG = "CONST_FLOW"


def _param(node: FlowNode, ctx: FlowContext, name: str, default: Any = None) -> Any:
    raw = node.params.get(name)
    if raw is None or raw == "":
        return default
    value = ctx.render(raw)
    return default if value is None or value == "" else value


def _robot_id(node: FlowNode, ctx: FlowContext) -> str:
    return _param(node, ctx, "robot_id") or ctx.get("robot_id") or "robot_a"


def _stop_event(ctx: FlowContext) -> Optional[threading.Event]:
    return ctx.extra.get("stop_event")


def _sleep(ctx: FlowContext, seconds: float) -> bool:
    ev = _stop_event(ctx)
    if ev is None:
        time.sleep(max(0.0, seconds))
        return True
    return not ev.wait(timeout=max(0.0, seconds))


def _mqtt(ctx: FlowContext) -> Optional[ConSTMqttAdapter]:
    return ctx.extra.get("const_mqtt")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_DRYRUN_WAIT_S = 8.0


def _wait_timeout(ctx: FlowContext, timeout: Any) -> Optional[float]:
    timeout_f = None if timeout in (None, "", 0, "0") else float(timeout)
    if ctx.extra.get("dryrun") and timeout_f is None:
        return _DRYRUN_WAIT_S
    return timeout_f


def _dut_ok(data: Optional[Dict[str, Any]]) -> bool:
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


def _can_reinsert(return_params: Dict[str, Any]) -> bool:
    if "can_reinsert" in return_params:
        return bool(return_params.get("can_reinsert"))
    if "canReinsert" in return_params:
        return bool(return_params.get("canReinsert"))
    return True


def build_handler_registry(
    robots: Dict[str, Any],
    task_state_machine,
    mqtt: Optional[ConSTMqttAdapter] = None,
    get_robot: Optional[Callable[[str], Any]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Callable]:

    def _get_robot(robot_id: str):
        if get_robot is not None:
            return get_robot(robot_id)
        return robots.get(robot_id)

    def _adapter(ctx: FlowContext) -> Optional[ConSTMqttAdapter]:
        return _mqtt(ctx) or mqtt

    def _ensure_extra(ctx: FlowContext):
        if stop_event is not None:
            ctx.extra.setdefault("stop_event", stop_event)
        if mqtt is not None:
            ctx.extra.setdefault("const_mqtt", mqtt)

    def _nav_to(ctx: FlowContext, robot, pose_name: str, timeout: float) -> bool:
        waypoints = getattr(NavigationPose, pose_name, None)
        if not waypoints:
            logger.error(_LOG, f"未知点位 '{pose_name}'（请在编辑器「点位」里添加）")
            return False
        dryrun = bool(ctx.extra.get("dryrun"))
        skip = True
        if skip and not dryrun and is_robot_at_pose(
            robot, waypoints, ConstNavTolerance.DISTANCE, ConstNavTolerance.HEADING, timeout=3.0,
        ):
            logger.info(_LOG, f"已在 {pose_name}，跳过导航")
            ctx.set("last_nav_pose", pose_name)
            return True
        goal = build_navigation_goal(
            waypoints,
            distance_tolerance=ConstNavTolerance.DISTANCE,
            heading_tolerance=ConstNavTolerance.HEADING,
        )
        nav_timeout = float(timeout or ConstTimeout.NAVIGATION)
        retry = not dryrun
        if dryrun:
            nav_timeout = min(nav_timeout, 12.0)
        result = send_navigation_action(robot, goal, timeout=nav_timeout, retry_on_disconnect=retry)
        ctx.set("last_nav_pose", pose_name)
        ctx.set("last_nav_result", str(getattr(result, "state", result)))
        if not result.succeeded:
            logger.error(_LOG, f"导航到 {pose_name} 失败: {ctx.get('last_nav_result')}")
            return False
        return True

    def _service(ctx: FlowContext, robot, task: str, area: str, extra_params: Any, timeout: float):
        if ctx.extra.get("dryrun"):
            timeout = min(float(timeout or ConstTimeout.ROBOT_ACTION), 15.0)
        extra = extra_params if isinstance(extra_params, dict) else {}
        result = robot.send_service_request_task(
            ConstService.ROBOT_TASK, task=task, area=area,
            extra_params=extra, maxtime=timeout,
        )
        parsed = result.parse_return_params() if result is not None else {}
        parsed = parsed or {}
        if ctx.extra.get("dryrun") and task == ConstTask.PICK_UP_BOX:
            if not parsed.get("has_box") and not parsed.get("hasBox"):
                parsed["has_box"] = True
                parsed.setdefault("gauge_count", 4)
                logger.info(_LOG, "演练：mock 未带回 has_box，按有箱 gauge_count=4 继续")
        ctx.set("last_op_task", task)
        ctx.set("last_op_success", bool(result))
        ctx.set("last_return_params", parsed)
        ctx.set("last_error_msg", getattr(result, "error_msg", "") or "")
        return result, parsed

    def handle_navigate(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None or not robot.is_connected():
            logger.error(_LOG, f"navigate：机器人 {robot_id} 不存在或未连接")
            return False
        pose_name = _param(node, ctx, "pose")
        if not pose_name:
            logger.error(_LOG, f"navigate 节点 {node.id} 未指定 pose")
            return False
        if adapter:
            adapter.set_status(RobotLiveStatus.MOVING)
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.NAVIGATION) or ConstTimeout.NAVIGATION)
        return _nav_to(ctx, robot, pose_name, timeout)

    def handle_send_operation(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None or not robot.is_connected():
            logger.error(_LOG, f"send_operation：机器人 {robot_id} 不存在或未连接")
            return False
        call_type = node.params.get("call_type", "service")
        task = _param(node, ctx, "task")
        area = _param(node, ctx, "area", "") or ""
        extra_params = _param(node, ctx, "extra_params", {}) or {}
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        if call_type == "action":
            result = send_task_action(
                robot, task=task, area=area, extra_params=extra_params,
                spec=CONST_TASK_ACTION_SPEC, timeout=timeout,
            )
            ctx.set("last_op_task", task)
            ctx.set("last_op_success", bool(result))
            parsed = {}
            if result is not None and hasattr(result, "parse_return_params"):
                parsed = result.parse_return_params() or {}
            ctx.set("last_return_params", parsed)
            return bool(result)
        result, _parsed = _service(ctx, robot, task, area, extra_params, timeout)
        if not result:
            logger.error(_LOG, f"操作 {task} 失败: {ctx.get('last_error_msg')}")
            return False
        return True

    def handle_update_step(node: FlowNode, ctx: FlowContext) -> bool:
        robot_id = _robot_id(node, ctx)
        step_label = _param(node, ctx, "step", node.label or node.id)
        message = _param(node, ctx, "message", "") or ""
        if task_state_machine is not None:
            task_state_machine.update_step(robot_id, step_label, message)
        return True

    def handle_mqtt_connect(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            logger.error(_LOG, "const_mqtt_connect：没有 MQTT 适配器")
            return False
        if not adapter.connected and not adapter.connect():
            return False
        adapter.start_heartbeat()
        adapter.set_status(RobotLiveStatus.WAIT_CALSYS)
        if not adapter.calsys_id and not adapter.discover():
            return False
        ctx.set("calsys_id", adapter.calsys_id)
        ctx.set("robot_id", ctx.get("robot_id") or "robot_a")
        return True

    def handle_set_live(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        status = _param(node, ctx, "status", RobotLiveStatus.IDLE)
        seq = _param(node, ctx, "station_seq")
        seq_i = _as_int(seq) if seq not in (None, "") else None
        if adapter:
            adapter.set_status(status, seq_i)
        ctx.set("robot_live_status", status)
        return True

    def handle_wait_calsys(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        want = _param(node, ctx, "want", "idle") or "idle"
        timeout_f = _wait_timeout(ctx, _param(node, ctx, "timeout"))
        stop_ev = _stop_event(ctx)
        adapter.set_status(RobotLiveStatus.WAIT_CALSYS)

        def _pred():
            if want == "idle":
                return adapter.is_idle() and adapter.is_bound()
            if want == "pending_clear":
                return (not adapter.is_pending_confirm()) and adapter.is_bound()
            if want == "bound":
                return adapter.is_bound()
            return adapter.is_idle()

        ok = adapter.wait_until(_pred, timeout=timeout_f, stop_event=stop_ev)
        live = adapter.snapshot_calsys()
        ctx.set("calsys_status", live.get("status"))
        ctx.set("calsys_is_running", bool(live.get("isRunning")))
        ctx.set("calsys_pending_confirm", bool(live.get("isPendingConfirm")))
        return ok

    def handle_plc_ready(node: FlowNode, ctx: FlowContext) -> bool:
        cfg = (load_config() or {}).get("plc") or {}
        if not cfg.get("enabled"):
            logger.info(_LOG, "PLC 检查未启用（plc.enabled=false），视为就绪")
            ctx.set("plc_ready", True)
            return True
        host = str(cfg.get("host") or "127.0.0.1")
        port = int(cfg.get("port") or 502)
        unit_id = int(cfg.get("unit_id") or 1)
        registers = cfg.get("ready_holding_registers") or []
        if not registers:
            ctx.set("plc_ready", True)
            return True
        try:
            from pymodbus.client import ModbusTcpClient  # noqa: PLC0415
        except ImportError:
            logger.error(_LOG, "pymodbus 未安装，PLC 检查失败")
            ctx.set("plc_ready", False)
            return False
        client = ModbusTcpClient(host, port=port)
        try:
            if not client.connect():
                logger.error(_LOG, f"无法连接 PLC {host}:{port}")
                ctx.set("plc_ready", False)
                return False
            for item in registers:
                addr = int(item.get("address", 0))
                ready_value = int(item.get("ready_value", 1))
                name = item.get("name") or f"reg_{addr}"
                rr = None
                try:
                    rr = client.read_holding_registers(addr, count=1, device_id=unit_id)
                except TypeError:
                    rr = client.read_holding_registers(addr, 1, unit=unit_id)
                if rr is None or getattr(rr, "isError", lambda: True)():
                    logger.warning(_LOG, f"PLC {name} 读寄存器 {addr} 失败")
                    ctx.set("plc_ready", False)
                    return False
                regs = getattr(rr, "registers", None) or []
                value = int(regs[0]) if regs else None
                if value != ready_value:
                    logger.info(_LOG, f"PLC {name} 未就绪 value={value} want={ready_value}")
                    ctx.set("plc_ready", False)
                    return False
            ctx.set("plc_ready", True)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(_LOG, f"PLC 检查异常: {e}")
            ctx.set("plc_ready", False)
            return False
        finally:
            try:
                client.close()
            except Exception:
                pass

    def handle_parse_box(node: FlowNode, ctx: FlowContext) -> bool:
        params = ctx.get("last_return_params") or {}
        has_box = bool(params.get("has_box", params.get("hasBox", False)))
        gauge_count = _as_int(params.get("gauge_count", params.get("gaugeCount", 0)), 0)
        ctx.set("has_box", has_box)
        ctx.set("gauge_count", gauge_count)
        if has_box:
            ctx.set("remaining", gauge_count)
            ctx.set("installed_stations", [])
            ctx.set("placed_this_round", 0)
        logger.info(_LOG, f"搬箱结果 has_box={has_box} gauge_count={gauge_count}")
        return True

    def handle_pick_station(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        remaining = _as_int(ctx.get("remaining", 0), 0)
        if remaining <= 0:
            ctx.set("has_station", False)
            return False
        st = adapter.find_wait_install_station()
        if not st:
            ctx.set("has_station", False)
            return False
        seq = _as_int(st.get("sequenceNumber"), 0)
        pose = PoseType.station(seq)
        ctx.set("has_station", True)
        ctx.set("station_seq", seq)
        ctx.set("station_pose", pose)
        ctx.set("station_name", st.get("name") or pose)
        return True

    def handle_stations_all_free(node: FlowNode, ctx: FlowContext) -> bool:
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        ok = adapter.all_enabled_free()
        ctx.set("stations_all_free", ok)
        return ok

    def _install_once(ctx, robot, adapter, seq, pose, timeout) -> str:
        adapter.set_status(RobotLiveStatus.INSTALL, seq)
        adapter.take_dutinfo(seq)
        reply = adapter.request_with_code_retry(
            lambda: adapter.install(seq, 0), stop_event=_stop_event(ctx),
        )
        code = adapter.reply_code(reply)
        if reply is None:
            adapter.call_human(HumanReason.MQTT_TIMEOUT)
            return "human"
        if code != 0:
            adapter.call_human(HumanReason.CALSYS_ERROR)
            return "human"
        if not _sleep(ctx, ConstTimeout.GANTRY_WAIT):
            return "fail"
        result, parsed = _service(ctx, robot, ConstTask.INSTALL_GAUGE, pose, {}, timeout)
        if result and not _can_reinsert(parsed):
            adapter.call_human(HumanReason.GAUGE_DROPPED)
            return "human"
        if not result:
            return "retry"
        reply = adapter.request_with_code_retry(
            lambda: adapter.install(seq, 1), stop_event=_stop_event(ctx),
        )
        code = adapter.reply_code(reply)
        if reply is None or code != 0:
            adapter.call_human(HumanReason.MQTT_TIMEOUT if reply is None else HumanReason.CALSYS_ERROR)
            return "human"
        adapter.set_status(RobotLiveStatus.WAIT_CALSYS, seq)
        dut = adapter.wait_dutinfo(seq, timeout=ConstTimeout.IDENTIFY_WAIT, stop_event=_stop_event(ctx))
        if dut is None:
            dut_reply = adapter.query_dutinfo(seq)
            dut = (dut_reply or {}).get("data") if dut_reply else None
        if _dut_ok(dut):
            return "ok"
        return "retry"

    def _uninstall_once(ctx, robot, adapter, seq, pose, extra, timeout) -> str:
        adapter.set_status(RobotLiveStatus.UNINSTALL, seq)
        reply = adapter.request_with_code_retry(
            lambda: adapter.uninstall(seq, 0), stop_event=_stop_event(ctx),
        )
        code = adapter.reply_code(reply)
        if reply is None or code != 0:
            adapter.call_human(HumanReason.MQTT_TIMEOUT if reply is None else HumanReason.CALSYS_ERROR)
            return "human"
        result, parsed = _service(ctx, robot, ConstTask.UNINSTALL_GAUGE, pose, extra, timeout)
        if result and not _can_reinsert(parsed):
            adapter.call_human(HumanReason.GAUGE_DROPPED)
            return "human"
        if not result:
            return "fail"
        reply = adapter.request_with_code_retry(
            lambda: adapter.uninstall(seq, 1), stop_event=_stop_event(ctx),
        )
        code = adapter.reply_code(reply)
        if reply is None or code != 0:
            adapter.call_human(HumanReason.MQTT_TIMEOUT if reply is None else HumanReason.CALSYS_ERROR)
            return "human"
        return "ok"

    def handle_install_station(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot = _get_robot(_robot_id(node, ctx))
        if adapter is None or robot is None:
            return False
        seq = _as_int(_param(node, ctx, "station_seq", ctx.get("station_seq")), 0)
        pose = _param(node, ctx, "station_pose", ctx.get("station_pose")) or PoseType.station(seq)
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        if not _nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
            return False
        retries = ConstTimeout.IDENTIFY_RETRIES
        outcome = "fail"
        for attempt in range(retries + 1):
            if attempt > 0:
                logger.warning(_LOG, f"工位 {seq} 识别/检漏失败，第 {attempt} 次重插")
                u = _uninstall_once(ctx, robot, adapter, seq, pose, {"reinstall": True}, timeout)
                if u == "human":
                    outcome = "human"
                    break
            outcome = _install_once(ctx, robot, adapter, seq, pose, timeout)
            if outcome in ("ok", "human"):
                break
        if outcome == "human":
            ctx.set("need_human", True)
            adapter.wait_human(_stop_event(ctx))
            ctx.set("need_human", False)
            ctx.set("remaining", max(0, _as_int(ctx.get("remaining"), 0) - 1))
            return True
        if outcome != "ok":
            logger.warning(_LOG, f"工位 {seq} 超过 {retries} 次重插，按坏表拆回后部料箱")
            _uninstall_once(ctx, robot, adapter, seq, pose, {"is_passed": False}, timeout)
            ctx.set("remaining", max(0, _as_int(ctx.get("remaining"), 0) - 1))
            return True
        installed: List[int] = list(ctx.get("installed_stations") or [])
        if seq not in installed:
            installed.append(seq)
        ctx.set("remaining", max(0, _as_int(ctx.get("remaining"), 0) - 1))
        ctx.set("installed_stations", installed)
        ctx.set("placed_this_round", _as_int(ctx.get("placed_this_round"), 0) + 1)
        return True

    def handle_mqtt_start(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        adapter.set_status(RobotLiveStatus.WAIT_CALSYS)
        adapter.clear_end_notify()
        reply = adapter.request_with_code_retry(adapter.start_test, stop_event=_stop_event(ctx))
        code = adapter.reply_code(reply)
        ctx.set("start_code", code)
        ctx.set("start_message", ((reply or {}).get("data") or {}).get("message", ""))
        if reply is None:
            adapter.call_human(HumanReason.MQTT_TIMEOUT)
            adapter.wait_human(_stop_event(ctx))
            return False
        if code != 0:
            adapter.call_human(HumanReason.CALSYS_ERROR)
            adapter.wait_human(_stop_event(ctx))
            return False
        return True

    def handle_wait_end(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        adapter.set_status(RobotLiveStatus.WAIT_CALSYS)
        timeout_f = _wait_timeout(ctx, _param(node, ctx, "timeout"))
        data = adapter.wait_end_notify(timeout=timeout_f, stop_event=_stop_event(ctx))
        if not data:
            return False
        ctx.set("end_is_normal", bool(data.get("isNormal")))
        ctx.set("end_is_leak", bool(data.get("isLeak")))
        ctx.set("end_description", data.get("description") or "")
        ctx.set("end_station_details", data.get("stationDetails") or [])
        return True

    def handle_reinstall_all(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot = _get_robot(_robot_id(node, ctx))
        if adapter is None or robot is None:
            return False
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        for seq in list(ctx.get("installed_stations") or []):
            pose = PoseType.station(seq)
            if not _nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
                return False
            u = _uninstall_once(ctx, robot, adapter, seq, pose, {"reinstall": True}, timeout)
            if u == "human":
                adapter.wait_human(_stop_event(ctx))
            i = _install_once(ctx, robot, adapter, seq, pose, timeout)
            if i == "human":
                adapter.wait_human(_stop_event(ctx))
            if i != "ok":
                logger.error(_LOG, f"泄漏后重装工位 {seq} 失败")
                return False
        return True

    def handle_uninstall_all(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot = _get_robot(_robot_id(node, ctx))
        if adapter is None or robot is None:
            return False
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        details = {}
        for d in (ctx.get("end_station_details") or []):
            seq = _as_int(d.get("sequence", d.get("sequenceNumber", -1)), -1)
            details[seq] = bool(d.get("isPassed"))
        for seq in list(ctx.get("installed_stations") or []):
            pose = PoseType.station(seq)
            is_passed = details.get(int(seq), False)
            if not _nav_to(ctx, robot, pose, ConstTimeout.NAVIGATION):
                return False
            u = _uninstall_once(ctx, robot, adapter, seq, pose, {"is_passed": is_passed}, timeout)
            if u == "human":
                adapter.wait_human(_stop_event(ctx))
            if u not in ("ok", "human"):
                logger.error(_LOG, f"拆表工位 {seq} 失败")
                return False
        ctx.set("installed_stations", [])
        ctx.set("placed_this_round", 0)
        return True

    def handle_call_human(node: FlowNode, ctx: FlowContext) -> bool:
        adapter = _adapter(ctx)
        reason = _param(node, ctx, "reason", HumanReason.CALSYS_ERROR)
        if adapter:
            adapter.call_human(reason)
            adapter.wait_human(_stop_event(ctx))
        ctx.set("need_human", False)
        return True

    return {
        "navigate": handle_navigate,
        "send_operation": handle_send_operation,
        "update_step": handle_update_step,
        "const_mqtt_connect": handle_mqtt_connect,
        "const_set_live": handle_set_live,
        "const_wait_calsys": handle_wait_calsys,
        "const_plc_ready": handle_plc_ready,
        "const_parse_box": handle_parse_box,
        "const_pick_station": handle_pick_station,
        "const_stations_all_free": handle_stations_all_free,
        "const_install_station": handle_install_station,
        "const_mqtt_start": handle_mqtt_start,
        "const_wait_end": handle_wait_end,
        "const_reinstall_all": handle_reinstall_all,
        "const_uninstall_all": handle_uninstall_all,
        "const_call_human": handle_call_human,
    }


NODE_TYPE_SCHEMAS: Dict[str, Dict] = {
    "const_mqtt_connect": {
        "label": "连接并发现检定系统",
        "category": "ConST / MQTT",
        "fields": [],
        "outputs": ["success", "failure"],
    },
    "const_set_live": {
        "label": "设置机器人上报状态",
        "category": "ConST / MQTT",
        "fields": [
            {"name": "status", "type": "select", "label": "status",
             "options": ["待机", "充电", "行进", "装表", "拆表", "等待检定系统回复", "呼叫人工"],
             "default": "待机"},
            {"name": "station_seq", "type": "text", "label": "当前工位序号（可 {{station_seq}}）"},
        ],
        "outputs": ["default"],
    },
    "const_wait_calsys": {
        "label": "等待检定系统状态",
        "category": "ConST / MQTT",
        "fields": [
            {"name": "want", "type": "select", "label": "等待条件", "required": True,
             "options": ["idle", "pending_clear", "bound"], "default": "idle",
             "hint": "idle=未在检表且未等人工确认；pending_clear=isPendingConfirm 变回 false；bound=已绑定本机"},
            {"name": "timeout", "type": "number", "label": "超时秒数（0=一直等）", "default": 0},
        ],
        "outputs": ["success", "failure"],
    },
    "const_plc_ready": {
        "label": "检查 PLC 是否就绪",
        "category": "ConST / 物流",
        "fields": [],
        "outputs": ["success", "failure"],
    },
    "const_parse_box": {
        "label": "解析搬箱结果",
        "category": "ConST / 物流",
        "fields": [],
        "outputs": ["default"],
    },
    "const_pick_station": {
        "label": "选一个待装表工位",
        "category": "ConST / 工位",
        "fields": [],
        "outputs": ["success", "failure"],
    },
    "const_stations_all_free": {
        "label": "全部工位是否待装表",
        "category": "ConST / 工位",
        "fields": [],
        "outputs": ["success", "failure"],
    },
    "const_install_station": {
        "label": "装表（含识别/检漏重插）",
        "category": "ConST / 工位",
        "fields": [
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
            {"name": "timeout", "type": "number", "label": "动作超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },
    "const_mqtt_start": {
        "label": "请求启动检定",
        "category": "ConST / MQTT",
        "fields": [],
        "outputs": ["success", "failure"],
    },
    "const_wait_end": {
        "label": "等待检定结束通知",
        "category": "ConST / MQTT",
        "fields": [
            {"name": "timeout", "type": "number", "label": "超时秒数（0=一直等）", "default": 0},
        ],
        "outputs": ["success", "failure"],
    },
    "const_reinstall_all": {
        "label": "泄漏后全部工位重装",
        "category": "ConST / 工位",
        "fields": [
            {"name": "timeout", "type": "number", "label": "动作超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },
    "const_uninstall_all": {
        "label": "按结果拆表回后部",
        "category": "ConST / 工位",
        "fields": [
            {"name": "timeout", "type": "number", "label": "动作超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },
    "const_call_human": {
        "label": "呼叫人工并等待处理",
        "category": "ConST / MQTT",
        "fields": [
            {"name": "reason", "type": "select", "label": "原因",
             "options": ["gauge_dropped", "mqtt_timeout", "calsys_error", "identify_failed"],
             "default": "calsys_error"},
        ],
        "outputs": ["default"],
    },
}

SERVICE_OPTIONS = ["robot_task"]
COMMON_NODES = ["navigate", "send_operation", "update_step"]
STEP_OPTIONS = [
    RobotLiveStatus.IDLE, RobotLiveStatus.MOVING, RobotLiveStatus.INSTALL,
    RobotLiveStatus.UNINSTALL, RobotLiveStatus.WAIT_CALSYS, RobotLiveStatus.CALL_HUMAN,
]

