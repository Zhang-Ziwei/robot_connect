"""XINXIANG 节点处理器：机器人走标准 /robot_task，检测模块走 PLC 握手。"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, Optional

from core.flow_engine import FlowContext, FlowNode
from core.plc_nodes import build_plc_handlers
from core.http_nodes import build_http_handlers
from infrastructure.config_loader import load_config
from plc_controller.common_plc import ModbusServerPlcClient
from hardware.navigation_utils import build_navigation_goal, is_robot_at_pose, send_navigation_action
from hardware.task_utils import send_task_action
from infrastructure.error_logger import get_error_logger

from programs.XINXIANG.constants import (
    DETECT_STEPS,
    PLC_POINTS,
    NavigationPose,
    XINXIANG_TASK_ACTION_SPEC,
    XinxiangNavTolerance,
    XinxiangService,
    XinxiangTimeout,
)
from programs.XINXIANG.plc_client import XinxiangPlcClient, run_detect_step

logger = get_error_logger()
_LOG = "XINXIANG"


def _param(node: FlowNode, ctx: FlowContext, name: str, default: Any = None) -> Any:
    raw = node.params.get(name)
    if raw is None or raw == "":
        return default
    value = ctx.render(raw)
    return default if value is None or value == "" else value


def _robot_id(node: FlowNode, ctx: FlowContext) -> str:
    return _param(node, ctx, "robot_id") or ctx.get("robot_id") or "robot_a"


def _as_extra_params(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            logger.warning(_LOG, f"extra_params 不是合法 JSON，已按空处理: {value!r}")
    return {}


def _service_path(alias: Any) -> str:
    name = str(alias or "robot_task").strip()
    if name in ("", "robot_task", XinxiangService.ROBOT_TASK):
        return XinxiangService.ROBOT_TASK
    return name if name.startswith("/") else XinxiangService.ROBOT_TASK


def build_handler_registry(
    robots: Dict[str, Any],
    task_state_machine,
    get_robot: Optional[Callable[[str], Any]] = None,
    stop_event: Optional[threading.Event] = None,
    plc_client: Optional[XinxiangPlcClient] = None,
) -> Dict[str, Callable]:

    def _get_robot(robot_id: str):
        if get_robot is not None:
            return get_robot(robot_id)
        return robots.get(robot_id)

    def _ensure_extra(ctx: FlowContext):
        if stop_event is not None:
            ctx.extra.setdefault("stop_event", stop_event)
        if plc_client is not None:
            ctx.extra.setdefault("plc_client", plc_client)

    def _stop(ctx: FlowContext) -> Optional[threading.Event]:
        return ctx.extra.get("stop_event") or stop_event

    def handle_navigate(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None or not robot.is_connected():
            logger.error(_LOG, f"navigate：机器人 {robot_id} 不存在或未连接")
            return False
        pose_name = _param(node, ctx, "pose")
        if not pose_name:
            logger.error(_LOG, f"navigate 节点 {node.id} 未指定 pose")
            return False
        waypoints = getattr(NavigationPose, pose_name, None)
        if not waypoints:
            logger.error(_LOG, f"navigate 节点 {node.id}：未知点位 '{pose_name}'")
            return False
        skip_if_at_pose = node.params.get("skip_if_at_pose", True)
        dryrun = bool(ctx.extra.get("dryrun"))
        if skip_if_at_pose and not dryrun and is_robot_at_pose(
            robot, waypoints, XinxiangNavTolerance.DISTANCE, XinxiangNavTolerance.HEADING, timeout=3.0,
        ):
            logger.info(_LOG, f"[{robot_id}] 已在 {pose_name}，跳过导航")
            ctx.set("last_nav_pose", pose_name)
            return True
        goal = build_navigation_goal(
            waypoints,
            distance_tolerance=XinxiangNavTolerance.DISTANCE,
            heading_tolerance=XinxiangNavTolerance.HEADING,
        )
        timeout = float(_param(node, ctx, "timeout", XinxiangTimeout.NAVIGATION) or XinxiangTimeout.NAVIGATION)
        retry = True
        if dryrun:
            timeout = min(timeout, 12.0)
            retry = False
        result = send_navigation_action(robot, goal, timeout=timeout, retry_on_disconnect=retry)
        ctx.set("last_nav_pose", pose_name)
        if not getattr(result, "succeeded", False):
            logger.error(_LOG, f"[{robot_id}] 导航到 {pose_name} 失败")
            return False
        return True

    def handle_send_operation(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None or not robot.is_connected():
            logger.error(_LOG, f"send_operation：机器人 {robot_id} 不存在或未连接")
            return False
        call_type = node.params.get("call_type", "action")
        task = _param(node, ctx, "task")
        area = _param(node, ctx, "area", "") or ""
        extra_params = _as_extra_params(_param(node, ctx, "extra_params", {}))
        timeout = float(_param(node, ctx, "timeout", XinxiangTimeout.ROBOT_ACTION) or XinxiangTimeout.ROBOT_ACTION)
        if ctx.extra.get("dryrun"):
            timeout = min(timeout, 15.0)
        logger.info(_LOG, f"[{robot_id}] robot_task {call_type} task={task} extra={extra_params}")
        if call_type == "service":
            result = robot.send_service_request_task(
                _service_path(_param(node, ctx, "service", "robot_task")),
                task=task, area=area, extra_params=extra_params, maxtime=timeout,
            )
            ok = bool(result)
        else:
            ok = bool(send_task_action(
                robot, task=task, area=area, extra_params=extra_params,
                spec=XINXIANG_TASK_ACTION_SPEC, timeout=timeout,
            ))
        ctx.set("last_op_task", task)
        ctx.set("last_op_success", ok)
        if not ok:
            logger.error(_LOG, f"[{robot_id}] 操作 {task} 失败")
            return False
        return True

    def handle_xinxiang_detect(node: FlowNode, ctx: FlowContext) -> bool:
        _ensure_extra(ctx)
        step = str(_param(node, ctx, "step", "dispense") or "dispense")
        timeout = _param(node, ctx, "timeout", XinxiangTimeout.PLC)
        skip = bool(ctx.extra.get("dryrun"))
        client = ctx.extra.get("plc_client") or plc_client
        ok = run_detect_step(
            step, timeout, stop_event=_stop(ctx), client=client, skip=skip,
        )
        ctx.set("last_detect_step", step)
        ctx.set("last_detect_success", bool(ok))
        if not ok:
            spec = DETECT_STEPS.get(step) or {}
            logger.error(_LOG, f"{spec.get('label', step)} 失败")
            return False
        return True

    def handle_update_step(node: FlowNode, ctx: FlowContext) -> bool:
        robot_id = _robot_id(node, ctx)
        step_label = _param(node, ctx, "step", node.label or node.id)
        message = _param(node, ctx, "message", "") or ""
        if task_state_machine is not None:
            task_state_machine.update_step(robot_id, step_label, message)
        return True

    # 已连接的服务端直接注入，通用节点和旧 xinxiang_detect 使用同一数据区。
    common_plc = None
    if plc_client is not None:
        cfg = dict((load_config() or {}).get("plc") or {})
        cfg.update(enabled=True, protocol="modbus_server", host=plc_client.host,
                   port=plc_client.port, unit_id=plc_client.unit_id)
        cfg["points"] = {**PLC_POINTS, **(cfg.get("points") or {})}
        common_plc = ModbusServerPlcClient(cfg, server=plc_client)
    return {
        **build_plc_handlers(client=common_plc, stop_event=stop_event),
        **build_http_handlers(stop_event=stop_event),
        "navigate": handle_navigate,
        "send_operation": handle_send_operation,
        "update_step": handle_update_step,
        "xinxiang_detect": handle_xinxiang_detect,
    }


NODE_TYPE_SCHEMAS: Dict[str, Dict] = {
    "xinxiang_detect": {
        "label": "检测模块握手",
        "category": "新乡 / 检测模块",
        "fields": [
            {"name": "step", "type": "select", "label": "步骤", "required": True,
             "options": ["dispense", "start", "pick", "finish"], "default": "dispense",
             "hint": "dispense=放料位移(线圈5,等状态2)；start=启动(线圈6,等状态4)；"
                     "pick=取料位移(线圈7,等状态5)；finish=取料完成(线圈8,等状态1)"},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 120},
        ],
        "outputs": ["success", "failure"],
    },
}

COMMON_NODES = ["navigate", "send_operation", "update_step", "send_http",
                "plc_read", "plc_write", "plc_wait", "plc_handshake"]
SERVICE_OPTIONS = ["robot_task"]
STEP_OPTIONS = [
    "定量分液", "检测放料位移", "放料", "检测启动", "检测取料位移", "取料", "检测取料完成", "完成",
]
