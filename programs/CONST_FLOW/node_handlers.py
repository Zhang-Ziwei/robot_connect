"""CONST_FLOW 节点处理器。图节点只做参数解析，动作分别交给三个模块。"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, List, Optional

from infrastructure.config_loader import load_config
from infrastructure.error_logger import get_error_logger
from hardware.task_utils import send_task_action
from core.flow_engine import FlowNode, FlowContext

from programs.CONST_FLOW.constants import (
    ConstTimeout, CONST_TASK_ACTION_SPEC, PoseType, RobotLiveStatus, HumanReason,
)
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter, station_seq_of
from programs.CONST_FLOW import calsys_ops, robot_ops, station_logic

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


def _mqtt(ctx: FlowContext) -> Optional[ConSTMqttAdapter]:
    return ctx.extra.get("const_mqtt")


def _as_extra_params(value: Any) -> Dict[str, Any]:
    """
    extra_params 兜底成 dict。

    编辑器里这个字段是 json 类型、存下来通常已经是 dict，但两种情况会拿到字符串：
    解析失败时前端原样回存，以及用 {{变量}} 从上下文取值时变量里放的是 JSON 文本。
    不兜底的话 call_task 会静默丢弃参数（reinstall / is_passed 传不过去）。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            logger.warning(_LOG, f"extra_params 不是合法 JSON，已按空处理: {value!r}")
    return {}


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
        return robot_ops.nav_to(ctx, robot, pose_name, timeout)

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
        extra_params = _as_extra_params(_param(node, ctx, "extra_params", {}))
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
        ok, parsed = robot_ops.call_task(ctx, robot, task, area, extra_params, timeout)
        # 装表/拆表时机器人会带回 can_reinsert=false 表示表掉了。
        # 这种情况动作本身是成功的，但后面不能再重插，只能呼人工——
        # 所以不并进本节点的成功/失败，而是写成 gauge_dropped 让图上用条件分支判。
        # 搬箱之类不带这个字段的任务，can_reinsert 缺省为 true，此处恒为 False，无副作用。
        ctx.set("gauge_dropped", ok and not robot_ops.can_reinsert(parsed))
        if not ok:
            logger.error(_LOG, f"操作 {task} 失败: {ctx.get('last_error_msg')}")
            return False
        if ctx.get("gauge_dropped"):
            logger.warning(_LOG, f"操作 {task} 完成，但机器人报表已掉落（can_reinsert=false）")
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
        seq = station_seq_of(st)
        pose = PoseType.station(seq)
        ctx.set("has_station", True)
        ctx.set("station_seq", seq)
        ctx.set("station_pose", pose)
        ctx.set("station_name", st.get("name") or pose)
        if seq <= 0:
            logger.warning(
                _LOG,
                f"选中工位序号无效 stationSeq={st.get('stationSeq')!r} "
                f"sequenceNumber={st.get('sequenceNumber')!r} "
                f"status={st.get('robotStationStatus')} name={st.get('name')}",
            )
        logger.info(_LOG, f"选中待装表工位 seq={seq} pose={pose} remaining={remaining}")
        return True

    def handle_stations_all_free(node: FlowNode, ctx: FlowContext) -> bool:
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        ok = adapter.all_enabled_free()
        ctx.set("stations_all_free", ok)
        return ok

    def handle_install_station(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot = _get_robot(_robot_id(node, ctx))
        if adapter is None or robot is None:
            return False
        seq = _as_int(_param(node, ctx, "station_seq", ctx.get("station_seq")), 0)
        pose = _param(node, ctx, "station_pose", ctx.get("station_pose")) or PoseType.station(seq)
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        return station_logic.install_station(ctx, robot, adapter, seq, pose, timeout)

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
        return station_logic.reinstall_all(ctx, robot, adapter, timeout)

    def handle_uninstall_all(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        robot = _get_robot(_robot_id(node, ctx))
        if adapter is None or robot is None:
            return False
        timeout = float(_param(node, ctx, "timeout", ConstTimeout.ROBOT_ACTION) or ConstTimeout.ROBOT_ACTION)
        return station_logic.uninstall_all(ctx, robot, adapter, timeout)

    # ── 装表链的原子节点 ────────────────────────────────────────────────────
    #
    # 这一组节点把原先 station_logic.install_station() 里写死的编排拆了出来，
    # 让"装表并等识别/检漏"能在子流程图上排（flows/const_install_once.json）。
    # 每个节点只做一件事、不含内部分支，顺序和分支交给图去表达：
    # 想在装表后加一步、想改重插次数、想调让位与合龙的时机，改图即可，不用动 Python。
    #
    # 分层没变：机器人动作仍走 robot_ops、上位机协议仍走 calsys_ops，
    # 这里只负责把节点参数翻译成对它们的一次调用。

    def _seq_pose(node: FlowNode, ctx: FlowContext):
        """工位序号与点位：节点参数优先，留空则取流程变量（主图选工位时写入）。"""
        seq = _as_int(_param(node, ctx, "station_seq", ctx.get("station_seq")), 0)
        pose = _param(node, ctx, "station_pose", ctx.get("station_pose")) or PoseType.station(seq)
        return seq, pose

    def _calsys_action(node: FlowNode, ctx: FlowContext, kind: str) -> bool:
        """
        上位机装/拆表动作。需要人工介入时置 need_human 并走 failure 出口，
        由图上接一个「呼叫人工」节点处理——原先这个决定藏在 Python 里。
        """
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        seq, _ = _seq_pose(node, ctx)
        action = _as_int(_param(node, ctx, "action", 0), 0)
        fn = calsys_ops.install_action if kind == "install" else calsys_ops.uninstall_action
        outcome = fn(adapter, seq, action, ctx)
        ctx.set("need_human", outcome == "human")
        return outcome == "ok"

    def handle_calsys_install(node: FlowNode, ctx: FlowContext) -> bool:
        return _calsys_action(node, ctx, "install")

    def handle_calsys_uninstall(node: FlowNode, ctx: FlowContext) -> bool:
        return _calsys_action(node, ctx, "uninstall")

    def handle_wait_identify(node: FlowNode, ctx: FlowContext) -> bool:
        """
        等上位机的识别/检漏结果。success=检漏通过，failure=未通过或没等到，
        图上接重插分支。判定规则仍复用 station_logic.dut_ok。
        """
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        seq, _ = _seq_pose(node, ctx)
        dut = calsys_ops.wait_identify(adapter, seq, ctx)
        passed = station_logic.dut_ok(dut)
        ctx.set("identify_passed", passed)
        ctx.set("identify_result", dut or {})
        logger.info(_LOG, f"工位 {seq} 识别/检漏结果 passed={passed} dut={dut}")
        return passed

    def handle_take_dutinfo(node: FlowNode, ctx: FlowContext) -> bool:
        """通知上位机开始取被检表信息（装表前置动作）。"""
        _ensure_extra(ctx)
        adapter = _adapter(ctx)
        if adapter is None:
            return False
        seq, _ = _seq_pose(node, ctx)
        adapter.take_dutinfo(seq)
        return True

    def handle_mark_installed(node: FlowNode, ctx: FlowContext) -> bool:
        """把当前工位记入已装表清单（供后续重装/拆表遍历）。"""
        seq, _ = _seq_pose(node, ctx)
        installed = list(ctx.get("installed_stations") or [])
        if seq not in installed:
            installed.append(seq)
        ctx.set("installed_stations", installed)
        ctx.set("placed_this_round", _as_int(ctx.get("placed_this_round"), 0) + 1)
        logger.info(_LOG, f"工位 {seq} 装表成功 installed={installed}")
        return True

    def handle_consume_gauge(node: FlowNode, ctx: FlowContext) -> bool:
        """
        料箱里的表少一只。

        装表的三种结局（成功 / 呼人工 / 超次数按坏表拆回）都要走这一步，
        否则 remaining 不减，主图的装表循环会一直选到工位、永远退不出来。
        """
        before = _as_int(ctx.get("remaining"), 0)
        ctx.set("remaining", max(0, before - 1))
        logger.info(_LOG, f"消耗一只表 remaining {before} → {ctx.get('remaining')}")
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
        "const_take_dutinfo": handle_take_dutinfo,
        "const_calsys_install": handle_calsys_install,
        "const_calsys_uninstall": handle_calsys_uninstall,
        "const_wait_identify": handle_wait_identify,
        "const_mark_installed": handle_mark_installed,
        "const_consume_gauge": handle_consume_gauge,
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
        # 整块封装版：编排写死在 station_logic.install_station() 里，图上改不了顺序。
        # 已由子流程 flows/const_install_station.json 取代，保留仅为兼容旧的流程图，
        # 所以不出现在编辑器面板上（见下方 HIDDEN_NODE_TYPES）。
        "label": "装表（整块封装 · 旧版）",
        "category": "ConST / 工位",
        "fields": [
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
            {"name": "timeout", "type": "number", "label": "动作超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },

    # ── 装表链的原子节点（供子流程图排布）──────────────────────────────
    "const_take_dutinfo": {
        "label": "通知上位机取表信息",
        "category": "ConST / 装表步骤",
        "fields": [
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}",
             "hint": "留空则用主图选工位时写入的 station_seq"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
        ],
        "outputs": ["success", "failure"],
    },
    "const_calsys_install": {
        "label": "上位机装表动作（让位/合龙）",
        "category": "ConST / 装表步骤",
        "fields": [
            {"name": "action", "type": "select", "label": "动作", "required": True,
             "options": ["0", "1"], "default": "0",
             "hint": "0=让位（装表前让开龙门架），1=合龙（装完复位）"},
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}",
             "hint": "留空则用主图选工位时写入的 station_seq"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
        ],
        "outputs": ["success", "failure"],
    },
    "const_calsys_uninstall": {
        "label": "上位机拆表动作（让位/合龙）",
        "category": "ConST / 装表步骤",
        "fields": [
            {"name": "action", "type": "select", "label": "动作", "required": True,
             "options": ["0", "1"], "default": "0",
             "hint": "0=让位，1=合龙"},
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}",
             "hint": "留空则用主图选工位时写入的 station_seq"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
        ],
        "outputs": ["success", "failure"],
    },
    "const_wait_identify": {
        "label": "等识别/检漏结果",
        "category": "ConST / 装表步骤",
        "fields": [
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}",
             "hint": "留空则用主图选工位时写入的 station_seq"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
        ],
        "outputs": ["success", "failure"],
    },
    "const_mark_installed": {
        "label": "记为已装表",
        "category": "ConST / 装表步骤",
        "fields": [
            {"name": "station_seq", "type": "text", "label": "工位序号", "default": "{{station_seq}}",
             "hint": "留空则用主图选工位时写入的 station_seq"},
            {"name": "station_pose", "type": "text", "label": "导航点名", "default": "{{station_pose}}"},
        ],
        "outputs": ["default"],
    },
    "const_consume_gauge": {
        "label": "料箱表数 -1",
        "category": "ConST / 装表步骤",
        "fields": [],
        "outputs": ["default"],
        "hint": "装表的每种结局都要走一次，否则 remaining 不减，主图装表循环退不出来",
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


#: 不在编辑器面板上显示的节点类型。
#: 处理器仍然注册着（旧流程图还能跑），只是不希望有人再拖它到新图上——
#: const_install_station 的编排写死在 Python 里，正是这次要解决的问题。
HIDDEN_NODE_TYPES = ["const_install_station"]


def validate_graph(graph: Dict[str, Any]) -> List[str]:
    """
    CONST_FLOW 专有的图校验，补在通用结构校验之后。

    装表编排从 Python 搬到图上之后，多了一类只有跑起来才发现的错误：
    料箱表数（remaining）的消耗原本和装表是一次原子操作，现在拆成了两个节点。
    装完表却没走到「料箱表数 -1」，remaining 就永远不减，
    主图的装表循环会一直选到工位、退不出来——现场表现是机器人反复装同一个工位。

    判据用"记为已装表之后能不能走到料箱表数 -1"，而不是"图里有没有这个节点"：
    后者会误伤只负责装一次表的内层子流程（消耗表数是外层的职责），
    也抓不到节点画了但没接上的情况。
    """
    nodes = {n.get("id"): n for n in graph.get("nodes", [])}
    starts = [nid for nid, n in nodes.items() if n.get("type") == "const_mark_installed"]
    if not starts:
        return []      # 这张图不负责装表收尾（比如内层的"装一次表"），不管

    adj: Dict[str, List[str]] = {}
    for e in graph.get("edges", []):
        adj.setdefault(e.get("source"), []).append(e.get("target"))

    warnings: List[str] = []
    for start in starts:
        seen, stack, reached = {start}, [start], False
        while stack:
            cur = stack.pop()
            if nodes.get(cur, {}).get("type") == "const_consume_gauge":
                reached = True
                break
            for nxt in adj.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if not reached:
            warnings.append(
                f"节点 [{nodes[start].get('label') or start}] 装完表后走不到「料箱表数 -1」："
                f"remaining 不会减少，主图的装表循环可能退不出来。"
                f"装表的每种结局（成功/呼人工/超次数拆回）都要汇到一个「料箱表数 -1」"
            )
    return warnings
