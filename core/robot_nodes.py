"""复用基础导航与 /robot_task 接口的通用机器人流程处理器。"""

from __future__ import annotations

import json
import math
import threading
from typing import Any, Callable, Dict, Optional

from core.flow_engine import FlowContext, FlowNode
from hardware.navigation_utils import build_navigation_goal, is_robot_at_pose, send_navigation_action
from hardware.task_utils import send_task_action
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()
_LOG = "RobotNode"


def _extra_params(value: Any, ctx: FlowContext) -> Dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = ctx.render(value)
            if isinstance(value, str):
                value = json.loads(value)
    value = ctx.render(value)
    if not isinstance(value, dict):
        raise ValueError("extra_params 必须是 JSON 对象")
    json.dumps(value, allow_nan=False)
    return value


def _timeout(value: Any) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是大于 0 的有限秒数")
    return timeout


def build_robot_handlers(
    robots: Dict[str, Any],
    task_state_machine,
    navigation_poses,
    get_robot: Optional[Callable[[str], Any]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Callable]:
    """navigation_poses 接受点名到路径的字典，或 NavigationPose 常量类。"""

    def stopped(ctx: FlowContext) -> bool:
        event = ctx.extra.get("stop_event") or stop_event
        return event is not None and event.is_set()

    def robot_id(params: dict, ctx: FlowContext) -> str:
        return params.get("robot_id") or ctx.get("robot_id") or "robot_a"

    def resolve_robot(params: dict, ctx: FlowContext):
        rid = robot_id(params, ctx)
        robot = get_robot(rid) if get_robot is not None else robots.get(rid)
        if robot is None or not robot.is_connected():
            raise ValueError(f"机器人 {rid} 不存在或未连接")
        return robot

    def handle_navigate(node: FlowNode, ctx: FlowContext) -> bool:
        ctx.set("last_nav_success", False)
        if stopped(ctx):
            return False
        try:
            params = ctx.render(node.params)
            pose = params.get("pose")
            if not isinstance(pose, str) or not pose.strip() or "{{" in pose:
                raise ValueError("必须指定有效导航点名 pose")
            waypoints = (navigation_poses.get(pose) if isinstance(navigation_poses, dict)
                         else getattr(navigation_poses, pose, None))
            if not waypoints:
                raise ValueError(f"未配置导航点位 {pose}")
            timeout = _timeout(params.get("timeout", 180))
            robot = resolve_robot(params, ctx)
            dryrun = bool(ctx.extra.get("dryrun"))
            if params.get("skip_if_at_pose", True) and not dryrun and is_robot_at_pose(
                robot, waypoints, 0.04, 0.04, timeout=3.0,
            ):
                ctx.set("last_nav_pose", pose)
                ctx.set("last_nav_success", True)
                return not stopped(ctx)
            goal = build_navigation_goal(waypoints, distance_tolerance=0.04, heading_tolerance=0.04)
            if stopped(ctx):
                return False
            result = send_navigation_action(
                robot, goal, timeout=min(timeout, 12.0) if dryrun else timeout,
                retry_on_disconnect=not dryrun,
            )
            ok = bool(getattr(result, "succeeded", False)) and not stopped(ctx)
            ctx.set("last_nav_success", ok)
            if ok:
                ctx.set("last_nav_pose", pose)
            return ok
        except (TypeError, ValueError, OverflowError) as exc:
            logger.error(_LOG, f"导航节点 {node.id}: {exc}")
            return False

    def handle_send_operation(node: FlowNode, ctx: FlowContext) -> bool:
        ctx.set("last_op_success", False)
        if stopped(ctx):
            return False
        try:
            params = ctx.render(node.params)
            task = params.get("task")
            if not isinstance(task, str) or not task.strip() or "{{" in task or "}}" in task:
                raise ValueError("task 必须是已配置的动作名称")
            call_type = params.get("call_type", "action")
            if call_type not in ("action", "service"):
                raise ValueError("call_type 必须是 action 或 service")
            extra = _extra_params(node.params.get("extra_params", {}), ctx)
            area = params.get("area") or ""
            if not isinstance(area, str):
                raise ValueError("area 必须是字符串")
            timeout = _timeout(params.get("timeout", 1200))
            dryrun = bool(ctx.extra.get("dryrun"))
            if dryrun:
                timeout = min(timeout, 15.0)
            service = params.get("service") or "robot_task"
            if call_type == "service":
                if service == "robot_task":
                    service = "/robot_task"
                if not isinstance(service, str) or not service.startswith("/") or "{{" in service:
                    raise ValueError("service 必须是 robot_task 或完整 ROS service 路径")
            robot = resolve_robot(params, ctx)
            if stopped(ctx):
                return False
            if call_type == "service":
                result = robot.send_service_request_task(
                    service, task=task, area=area, extra_params=extra, maxtime=timeout,
                )
            else:
                result = send_task_action(
                    robot, task=task, area=area, extra_params=extra, timeout=timeout,
                    retry_on_disconnect=not dryrun,
                )
            ok = bool(result) and not stopped(ctx)
            ctx.set("last_op_task", task)
            ctx.set("last_op_success", ok)
            return ok
        except (TypeError, ValueError, OverflowError) as exc:
            logger.error(_LOG, f"操作节点 {node.id}: {exc}")
            return False

    def handle_update_step(node: FlowNode, ctx: FlowContext) -> bool:
        if stopped(ctx):
            return False
        params = ctx.render(node.params)
        step = params.get("step") or node.label or node.id
        if task_state_machine is not None:
            task_state_machine.update_step(robot_id(params, ctx), step, params.get("message") or "")
        return True

    return {
        "navigate": handle_navigate,
        "send_operation": handle_send_operation,
        "update_step": handle_update_step,
    }
