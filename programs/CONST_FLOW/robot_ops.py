"""
CONST 对机器人的动作：导航、装表/拆表 ROS Service。
不发 MQTT、不做识别/检漏判断。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    build_navigation_goal, send_navigation_action, is_robot_at_pose,
)
from core.flow_engine import FlowContext

from programs.CONST_FLOW.constants import (
    NavigationPose, ConstNavTolerance, ConstService, ConstTimeout, ConstTask,
)

logger = get_error_logger()
_LOG = "CONST_ROBOT"


def nav_to(ctx: FlowContext, robot, pose_name: str, timeout: float) -> bool:
    waypoints = getattr(NavigationPose, pose_name, None)
    if not waypoints:
        logger.error(_LOG, f"未知点位 '{pose_name}'（请在编辑器「点位」里添加）")
        return False
    dryrun = bool(ctx.extra.get("dryrun"))
    if not dryrun and is_robot_at_pose(
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
    logger.info(_LOG, f"导航 → {pose_name} timeout={nav_timeout}s")
    result = send_navigation_action(robot, goal, timeout=nav_timeout, retry_on_disconnect=retry)
    ctx.set("last_nav_pose", pose_name)
    ctx.set("last_nav_result", str(getattr(result, "state", result)))
    if not result.succeeded:
        logger.error(_LOG, f"导航到 {pose_name} 失败: {ctx.get('last_nav_result')}")
        return False
    logger.info(_LOG, f"导航完成 {pose_name}")
    return True


def call_task(
    ctx: FlowContext,
    robot,
    task: str,
    area: str,
    extra_params: Optional[Dict[str, Any]] = None,
    timeout: float = ConstTimeout.ROBOT_ACTION,
) -> Tuple[bool, Dict[str, Any]]:
    if ctx.extra.get("dryrun"):
        timeout = min(float(timeout or ConstTimeout.ROBOT_ACTION), 15.0)
    extra = extra_params if isinstance(extra_params, dict) else {}
    logger.info(
        _LOG,
        f"发送机器人 {task} area={area} extra={extra} timeout={timeout}s",
    )
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
    ok = bool(result)
    err = getattr(result, "error_msg", "") or ""
    ctx.set("last_op_task", task)
    ctx.set("last_op_success", ok)
    ctx.set("last_return_params", parsed)
    ctx.set("last_error_msg", err)
    if ok:
        logger.info(_LOG, f"机器人回复 {task} success return_params={parsed}")
    else:
        logger.error(_LOG, f"机器人失败 {task} error={err} return_params={parsed}")
    return ok, parsed


def install_gauge(ctx: FlowContext, robot, pose: str, timeout: float):
    return call_task(ctx, robot, ConstTask.INSTALL_GAUGE, pose, {}, timeout)


def uninstall_gauge(ctx: FlowContext, robot, pose: str, extra: Dict[str, Any], timeout: float):
    return call_task(ctx, robot, ConstTask.UNINSTALL_GAUGE, pose, extra, timeout)


def can_reinsert(return_params: Dict[str, Any]) -> bool:
    if "can_reinsert" in return_params:
        return bool(return_params.get("can_reinsert"))
    if "canReinsert" in return_params:
        return bool(return_params.get("canReinsert"))
    return True
