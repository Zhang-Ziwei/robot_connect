"""
WRC_FLOW 节点处理器（"动作类"节点的具体实现）

core.flow_engine.FlowEngine 本身只负责控制流（顺序/条件/并行/循环/暂停），
真正会碰机器人/硬件的节点类型都在这里实现，通过 build_handler_registry() 注册给引擎。

本文件登记的节点类型：
    navigate        —— 导航到点位（对应 hardware.navigation_utils.send_navigation_action）
    send_operation  —— 发送操作动作，params.call_type 二选一：
                          "action"  -> hardware.task_utils.send_task_action（actionlib）
                          "service" -> robot.send_service_request_task（ROS service）
                        两种底层协议在图上收敛成同一个节点类型，用户只需要切一个下拉框。
    plc_action      —— PLC/传送带动作（对接 programs.WRC.plc_modbus.ConveyorController）；
                        未配置传送带（conveyor=None）时优雅降级为跳过并返回成功，
                        方便流程图先跑通、以后再接真实硬件。
    find_slot       —— 按状态在一组槽位变量（如 P3_1_state / P3_2_state）里查找第一个
                        匹配的槽位并写入上下文变量，用来还原 WRC.py 里
                        ``SlotTracker.find_slot_by_state`` 的"动态选槽位"分支逻辑。
    update_step     —— 写入 ParallelTaskStateMachine，供 GET_TASK_STATE 查询回显

新增一个动作类型：写一个 `def xxx(node, ctx) -> bool` 函数，在 build_handler_registry()
里加一行注册即可，同时在文末 NODE_TYPE_SCHEMAS 里补一份表单描述，不需要改
core/flow_engine.py。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import build_navigation_goal, send_navigation_action, is_robot_at_pose
from hardware.task_utils import send_task_action
from core.flow_engine import FlowNode, FlowContext

from programs.WRC_FLOW.constants import (
    NavigationPose, WRCFlowNavTolerance, WRCFlowService, WRC_FLOW_TASK_ACTION_SPEC,
)

logger = get_error_logger()
_LOG = "WRC_FLOW"


def build_handler_registry(
    robots: Dict[str, "RobotController"],
    task_state_machine,
    get_robot: Optional[Callable[[str], Optional["RobotController"]]] = None,
    conveyor: Optional[Any] = None,
) -> Dict[str, Callable]:
    """
    组装本项目的节点处理器注册表，传给 ``FlowEngine(handlers=...)``。

    参数：
        robots            : {robot_id: RobotController} —— 正常运行时传生产机器人实例，
                             演练（dry-run）时传指向 mock_rosbridge_server 的临时实例，
                             handler 代码完全不用区分，这也是"零额外开发"的关键。
        task_state_machine: core.task_state_machine.ParallelTaskStateMachine 实例
        get_robot         : 可选，按 robot_id 取机器人的函数；缺省直接从 robots 字典取
        conveyor          : 可选，programs.WRC.plc_modbus.ConveyorController 实例，
                             供 plc_action 节点使用；不传时 plc_action 节点直接跳过
                             （记日志 + 返回成功），不会阻塞流程。
    """

    def _get_robot(robot_id: str):
        if get_robot is not None:
            return get_robot(robot_id)
        return robots.get(robot_id)

    def _robot_id_of(ctx: FlowContext, node: FlowNode) -> str:
        # 节点参数里可以显式指定 robot_id，否则取上下文变量，最后兜底 robot_a
        return ctx.render(node.params.get("robot_id")) or ctx.get("robot_id", "robot_a")

    # ── navigate ─────────────────────────────────────────────────────────────

    def handle_navigate(node: FlowNode, ctx: FlowContext) -> bool:
        robot_id = _robot_id_of(ctx, node)
        robot = _get_robot(robot_id)
        if robot is None:
            logger.error(_LOG, f"navigate 节点 {node.id}：机器人 {robot_id} 不存在")
            return False

        pose_name = ctx.render(node.params.get("pose"))
        waypoints = getattr(NavigationPose, pose_name, None)
        if not waypoints:
            logger.error(_LOG, f"navigate 节点 {node.id}：未知点位 '{pose_name}'")
            return False

        skip_if_at_pose = node.params.get("skip_if_at_pose", True)
        if skip_if_at_pose and is_robot_at_pose(
            robot, waypoints,
            WRCFlowNavTolerance.DISTANCE, WRCFlowNavTolerance.HEADING,
            timeout=3.0,
        ):
            logger.info(_LOG, f"[{robot_id}] 已在 {pose_name}，跳过导航")
            ctx.set("last_nav_pose", pose_name)
            return True

        goal = build_navigation_goal(
            waypoints,
            distance_tolerance=WRCFlowNavTolerance.DISTANCE,
            heading_tolerance=WRCFlowNavTolerance.HEADING,
        )
        result = send_navigation_action(robot, goal, timeout=node.params.get("timeout", 180.0))
        ctx.set("last_nav_pose", pose_name)
        ctx.set("last_nav_result", str(getattr(result, "state", result)))
        if not result.succeeded:
            logger.error(_LOG, f"[{robot_id}] 导航到 {pose_name} 失败: {ctx.get('last_nav_result')}")
            return False
        return True

    # ── send_operation（合并 action / service 两种传输方式）─────────────────

    def handle_send_operation(node: FlowNode, ctx: FlowContext) -> bool:
        robot_id = _robot_id_of(ctx, node)
        robot = _get_robot(robot_id)
        if robot is None:
            logger.error(_LOG, f"send_operation 节点 {node.id}：机器人 {robot_id} 不存在")
            return False

        call_type = node.params.get("call_type", "action")
        task = ctx.render(node.params.get("task"))
        area = ctx.render(node.params.get("area", ""))
        extra_params = ctx.render(node.params.get("extra_params", {}))
        timeout = node.params.get("timeout", 1200.0)
        # service 走哪个 ROS service：默认 ROBOT_TASK（零件抓放/装配），
        # 搬箱子（pick_up_box/put_down_box）需要显式传 params.service="robot_task_geely"，
        # 对照 programs/WRC/WRC.py 里 _send_component_service 用 ROBOT_TASK、
        # _send_box_service 用 ROBOT_TASK_GEELY 两个不同 service 的区分。
        service_alias = node.params.get("service", "robot_task")
        service_path = WRCFlowService.ROBOT_TASK_GEELY if service_alias == "robot_task_geely" else WRCFlowService.ROBOT_TASK

        if call_type == "service":
            result = robot.send_service_request_task(
                service_path, task=task, area=area,
                extra_params=extra_params, maxtime=timeout,
            )
        else:
            result = send_task_action(
                robot, task=task, area=area, extra_params=extra_params,
                spec=WRC_FLOW_TASK_ACTION_SPEC, timeout=timeout,
            )

        ctx.set("last_op_task", task)
        ctx.set("last_op_success", bool(result))
        if not result:
            err = getattr(result, "error_msg", "")
            logger.error(_LOG, f"[{robot_id}] 操作 {task} 失败: {err}")
            return False
        return True

    # ── plc_action（PLC/传送带动作）────────────────────────────────────────

    def handle_plc_action(node: FlowNode, ctx: FlowContext) -> bool:
        action = node.params.get("action", "forward")
        wait_done = node.params.get("wait_done", True)
        timeout = node.params.get("timeout", 60.0)

        if conveyor is None:
            logger.info(
                _LOG,
                f"plc_action 节点 {node.id}：未配置传送带 (conveyor=None)，"
                f"跳过动作 '{action}'，视为成功（先跑通流程图，后续再接硬件）",
            )
            return True

        try:
            if action == "forward":
                ok = conveyor.forward()
            elif action == "reverse":
                ok = conveyor.reverse()
            elif action == "stop":
                return bool(conveyor.stop())
            else:
                logger.error(_LOG, f"plc_action 节点 {node.id}：未知动作 '{action}'")
                return False

            if not ok:
                logger.error(_LOG, f"plc_action 节点 {node.id}：下发动作 '{action}' 失败")
                return False
            if wait_done:
                return bool(conveyor.wait_done(timeout=timeout))
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(_LOG, f"plc_action 节点 {node.id} 异常: {e}")
            return False

    # ── find_slot（按状态动态选槽位，对应 WRC.py 的 SlotTracker.find_slot_by_state）──

    def handle_find_slot(node: FlowNode, ctx: FlowContext) -> bool:
        slots = node.params.get("slots") or []
        want_state = ctx.render(node.params.get("want_state"))
        output_var = node.params.get("output_var", "found_slot")

        for slot in slots:
            if ctx.get(f"{slot}_state") == want_state:
                ctx.set(output_var, slot)
                logger.info(_LOG, f"find_slot 节点 {node.id}：在 {slots} 中找到状态='{want_state}' 的槽位 {slot}")
                return True

        ctx.set(output_var, None)
        logger.warning(_LOG, f"find_slot 节点 {node.id}：{slots} 中没有状态='{want_state}' 的槽位")
        return False

    # ── update_step ──────────────────────────────────────────────────────────

    def handle_update_step(node: FlowNode, ctx: FlowContext) -> bool:
        robot_id = _robot_id_of(ctx, node)
        step_label = ctx.render(node.params.get("step", node.label or node.id))
        message = ctx.render(node.params.get("message", ""))
        if task_state_machine is not None:
            task_state_machine.update_step(robot_id, step_label, message)
        return True

    return {
        "navigate": handle_navigate,
        "send_operation": handle_send_operation,
        "plc_action": handle_plc_action,
        "find_slot": handle_find_slot,
        "update_step": handle_update_step,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 节点参数表单 schema（供图形化编辑器动态生成参数面板，格式见 core/flow_engine.py
# 的 BUILTIN_NODE_TYPE_SCHEMAS 顶部说明：fields 是有序数组）。
# flow_api_server 会把内置 schema 与这里的 schema 合并后一起返回给前端。
# ──────────────────────────────────────────────────────────────────────────────

_ROBOT_ID_OPTIONS = ["robot_a", "robot_b", "robot_c"]

NODE_TYPE_SCHEMAS: Dict[str, Dict] = {
    "navigate": {
        "label": "导航到点位",
        "category": "机器人动作",
        "fields": [
            {"name": "pose", "type": "select", "label": "目标点位", "required": True,
             "options": ["home", "P1", "P2", "P3_1", "P4"]},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options": _ROBOT_ID_OPTIONS, "default": "robot_a"},
            {"name": "skip_if_at_pose", "type": "checkbox", "label": "已在该点位时跳过导航", "default": True},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 180},
        ],
        "outputs": ["success", "failure"],
    },
    "send_operation": {
        "label": "发送操作动作",
        "category": "机器人动作",
        "fields": [
            {"name": "call_type", "type": "select", "label": "调用方式", "required": True,
             "options": ["action", "service"], "default": "action"},
            {"name": "service", "type": "select", "label": "Service 通道（call_type=service 时生效）",
             "options": ["robot_task", "robot_task_geely"], "default": "robot_task"},
            {"name": "task", "type": "text", "label": "任务名(task)", "required": True},
            {"name": "area", "type": "text", "label": "区域(area)"},
            {"name": "extra_params", "type": "json", "label": "附加参数(extra_params)"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options": _ROBOT_ID_OPTIONS, "default": "robot_a"},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 1200},
        ],
        "outputs": ["success", "failure"],
    },
    "plc_action": {
        "label": "PLC/传送带动作",
        "category": "机器人动作",
        "fields": [
            {"name": "action", "type": "select", "label": "动作", "required": True,
             "options": ["forward", "reverse", "stop"], "default": "forward"},
            {"name": "wait_done", "type": "checkbox", "label": "等待动作完成", "default": True},
            {"name": "timeout", "type": "number", "label": "超时秒数", "default": 60},
        ],
        "outputs": ["success", "failure"],
    },
    "find_slot": {
        "label": "查找槽位（按状态）",
        "category": "流程辅助",
        "fields": [
            {"name": "slots", "type": "json", "label": '候选槽位名数组，如 ["P3_1","P3_2"]', "required": True},
            {"name": "want_state", "type": "text", "label": "要查找的状态值（支持 {{var}} 模板）", "required": True},
            {"name": "output_var", "type": "text", "label": "找到后写入的变量名", "required": True,
             "default": "found_slot"},
        ],
        "outputs": ["success", "failure"],
    },
    "update_step": {
        "label": "记录状态步骤",
        "category": "状态记录",
        "fields": [
            {"name": "step", "type": "text", "label": "步骤名称", "required": True},
            {"name": "message", "type": "text", "label": "描述信息"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options": _ROBOT_ID_OPTIONS, "default": "robot_a"},
        ],
        "outputs": ["default"],
    },
}