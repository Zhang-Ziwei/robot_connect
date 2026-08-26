"""
WAIC 任务处理器

职责：
    - PICK_BOX_TO_SP  ：异步大任务，把箱子从 box_initial_area 搬到 box_target_area
    - PICK_UP_BOX     ：异步任务，导航到 area 后执行抓取（HTTP 立即返回）
    - PUT_DOWN_BOX    ：异步任务，导航到 area 后执行放置（HTTP 立即返回）
    - NAVIGATION      ：异步任务，从当前位置导航到 area（HTTP 立即返回）

每个异步任务结束后（成功或失败），都会通过 CallbackSender 向外部系统发送 HTTP 回调。

导入层级易变）：
    标准库（threading / time / uuid）          — 最稳定
    infrastructure.*（constants / logger）     — 稳定
    hardware.*（robot_controller / nav）       — 较稳定
    core.task_state_machine                    — 较稳定
    handlers.callback_sender                    — 稳定
    programs.WAIC.constants（本项目专属）      — 自己维护
"""

import threading
import time
import uuid
from typing import Dict, Any, Optional

from infrastructure.constants import (
    ErrorCode,
    NavigationState,
    make_error_response,
    make_success_response,
)
from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    send_navigation_action,
    build_navigation_goal,
    is_robot_at_pose,
)
from hardware.robot_controller import RobotController
from core.task_state_machine import TaskStateMachine
from handlers.callback_sender import CallbackSender, get_callback_sender

from programs.WAIC.constants import (
    NavigationPose,
    WAICService,
    WAICTask,
    WAICStep,
    WAICTimeout,
    WAICNavTolerance,
    get_nav_pose,
)

logger = get_error_logger()


def _make_action_response(
    success: bool,
    action_type: str,
    description: str = "",
    message: str = "",
    code: int = 0,
) -> Dict:
    """构造 WAIC 标准动作响应体。"""
    if not message:
        message = (
            f"动作 {action_type} 执行成功"
            if success
            else f"动作 {action_type} 执行失败"
        )
    return {
        "success":     success,
        "code":        code if success else (code or ErrorCode.INTERNAL_ERROR),
        "action_type": action_type,
        "description": description,
        "message":     message,
    }


class WAICHandler:
    """
    WAIC 任务处理器。

    由 programs/TJSH/cmd_handler.py 实例化后挂载到命令分发表：
        self._waic = WAICHandler(robots=self.robots)
        self._handler_map.update({
            "PICK_BOX_TO_SP": self._waic.handle_pick_box_to_sp,
            "PICK_UP_BOX":    self._waic.handle_pick_up_box,
            "PUT_DOWN_BOX":   self._waic.handle_put_down_box,
            "NAVIGATION":     self._waic.handle_navigation,
        })
    """

    def __init__(
        self,
        robots: Dict[str, RobotController] = None,
        task_state_machine: TaskStateMachine = None,
        callback_sender: CallbackSender = None,
    ):
        self.robots = robots or {}
        # 单机器人任务状态机（WAIC 每次仅一台机器人执行）
        # 允许外部传入，便于与 dispatcher 共享同一个状态机实例
        if task_state_machine is not None:
            self.task_state_machine = task_state_machine
        else:
            self.task_state_machine = TaskStateMachine()
        # 任务完成回调发送器；为 None 时从全局配置读取单例
        self.callback_sender: CallbackSender = (
            callback_sender if callback_sender is not None else get_callback_sender()
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────────────────

    def _get_robot(self, cmd_data: Dict) -> tuple:
        """
        从 cmd_data.params.robot_id 或默认取 robot_a 获取机器人实例。

        返回:
            (robot_id: str, robot: RobotController | None, error_resp: dict | None)
        """
        params    = cmd_data.get("params", {}) or {}
        robot_id  = params.get("robot_id") or "robot_a"
        robot     = self.robots.get(robot_id)
        if robot is None:
            resp = make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {robot_id} 不存在",
            )
            return robot_id, None, resp
        return robot_id, robot, None

    def _busy_response(self, action_type: str, robot_id: str) -> Dict:
        state = self.task_state_machine.get_state()
        busy_task = state.get("cmd_id") or state.get("task_id") or "未知"
        return _make_action_response(
            False,
            action_type,
            message=f"机器人 {robot_id} 正忙（当前任务: {busy_task}）",
            code=ErrorCode.ROBOT_BUSY,
        )

    def _resolve_pose(self, area_name: str, label: str, robot_id: str) -> tuple:
        """
        将 area_name 解析为导航点位坐标列表。

        返回:
            (waypoints: list | None, error_msg: str | None)
        """
        pose = get_nav_pose(area_name)
        if pose is None:
            msg = f"[{robot_id}] {label}: 未找到点位 '{area_name}'，请在 robot_config.json 的 navigation_poses.WAIC 中配置"
            logger.error("WAIC", msg)
            return None, msg
        if isinstance(pose, list) and pose and isinstance(pose[0], (list, tuple)):
            return pose, None
        elif isinstance(pose, (list, tuple)) and len(pose) == 7 and isinstance(pose[0], (int, float)):
            return [pose], None
        else:
            return [pose], None

    def _navigate(
        self,
        robot: RobotController,
        area_name: str,
        label: str,
        robot_id: str,
        task_id: str = "",
    ) -> bool:
        """
        导航到 area_name 对应的点位。失败时记录状态机错误。
        FAILED/ABORTED 状态最多重试 3 次。

        返回:
            True 表示导航到达，False 表示失败
        """
        waypoints, err = self._resolve_pose(area_name, label, robot_id)
        if err:
            if task_id:
                self.task_state_machine.set_error(err)
            return False

        goal = build_navigation_goal(
            waypoints,
            distance_tolerance=WAICNavTolerance.DISTANCE,
            heading_tolerance=WAICNavTolerance.HEADING,
            translation_enable=True,
            translation_heading=WAICNavTolerance.TRANSLATION_HEADING,
        )

        def _on_feedback(fb):
            print(f"  [WAIC 导航][{robot_id}] {label}: {fb.state.name}")

        _RETRYABLE = {NavigationState.FAILED, NavigationState.ABORTED}
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            result = send_navigation_action(
                robot,
                goal,
                feedback_callback=_on_feedback,
                timeout=WAICTimeout.NAVIGATION,
            )
            if result is None:
                err = f"[{robot_id}] 导航到 {label} 超时或无响应"
                logger.error("WAIC", err)
                if task_id:
                    self.task_state_machine.set_error(err)
                return False

            if result.succeeded:
                logger.info("WAIC", f"[{robot_id}] 导航到 {label} 成功（state={result.state.name}）")
                return True

            if result.state in _RETRYABLE and attempt < max_retries:
                logger.warning(
                    "WAIC",
                    f"[{robot_id}] 导航到 {label} 暂态失败(state={result.state.name})，"
                    f"第 {attempt} 次重试...",
                )
                time.sleep(1)
                continue

            err = f"[{robot_id}] 导航到 {label} 失败: state={result.state.name}"
            if result.causes:
                err += f" causes={result.causes}"
            logger.error("WAIC", err)
            if task_id:
                self.task_state_machine.set_error(err)
            return False

        return False


    # ──────────────────────────────────────────────────────────────────────────
    # 公开命令处理器
    # ──────────────────────────────────────────────────────────────────────────

    def handle_pick_box_to_sp(self, cmd_data: Dict) -> Dict:
        """
        PICK_BOX_TO_SP —— 异步大任务：将箱子从初始区搬到目标区。

        params 字段：
            robot_id        : 执行任务的机器人 ID（可选，默认 "robot_a"）
            box_initial_area: 取箱点位名称（对应 navigation_poses.WAIC 中的 key）
            box_target_area : 放箱点位名称

        流程：
            1. 导航到 box_initial_area
            2. 执行 pick_up_box
            3. 导航到 box_target_area
            4. 执行 put_down_box
        """
        action_type = "PICK_BOX_TO_SP"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        initial_area = params.get("box_initial_area", "")
        target_area  = params.get("box_target_area", "")

        if not initial_area or not target_area:
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: box_initial_area 和 box_target_area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        task_id = cmd_id or str(uuid.uuid4())

        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                # 1. 导航到初始区
                # 1.1 检测机器人是否在初始区
                if not is_robot_at_pose(
                    robot, get_nav_pose(initial_area),
                    WAICNavTolerance.DISTANCE, WAICNavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        WAICStep.NAVIGATING, f"导航到 {initial_area}"
                    )
                    if not self._navigate(robot, initial_area, initial_area, robot_id, task_id):
                        return
                else:
                    logger.info("WAIC", f"[{robot_id}] 机器人已在 {initial_area} 点位，跳过导航")

                # 2. 抓取箱子
                self.task_state_machine.update_step_label(
                    WAICStep.PICKING_UP, f"抓取箱子（{initial_area}）"
                )
                result = robot.send_service_request_task(
                    WAICService.ROBOT_TASK, task=WAICTask.PICK_UP_BOX,
                    area=initial_area, maxtime=WAICTimeout.ROBOT_ACTION,
                )
                if not result:
                    err = f"[{robot_id}] pick_up_box 失败: {result.error_msg}"
                    logger.error("WAIC", err)
                    self.task_state_machine.set_error(err)
                    return
                logger.info("WAIC", f"[{robot_id}] pick_up_box 成功")

                # 3. 导航到目标区
                self.task_state_machine.update_step_label(
                    WAICStep.NAVIGATING_TARGET, f"导航到 {target_area}"
                )
                if not self._navigate(robot, target_area, target_area, robot_id, task_id):
                    return

                # 4. 放置箱子
                self.task_state_machine.update_step_label(
                    WAICStep.PUTTING_DOWN, f"放置箱子（{target_area}）"
                )
                result = robot.send_service_request_task(
                    WAICService.ROBOT_TASK, task=WAICTask.PUT_DOWN_BOX,
                    area=target_area, maxtime=WAICTimeout.ROBOT_ACTION,
                )
                if not result:
                    err = f"[{robot_id}] put_down_box 失败: {result.error_msg}"
                    logger.error("WAIC", err)
                    self.task_state_machine.set_error(err)
                    return
                logger.info("WAIC", f"[{robot_id}] put_down_box 成功")

                self.task_state_machine.complete_task(True, f"{initial_area} → {target_area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PICK_BOX_TO_SP', success=True,
                    message=f"搬运完成: {initial_area} → {target_area}",
                )
                logger.info("WAIC", f"[{robot_id}] PICK_BOX_TO_SP 完成: {initial_area} → {target_area}")

            except Exception as exc:
                err = f"[{robot_id}] PICK_BOX_TO_SP 异常: {exc}"
                logger.exception_occurred("WAIC", "PICK_BOX_TO_SP", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PICK_BOX_TO_SP', success=False,
                    message=err,
                )

        t = threading.Thread(target=_run, daemon=True, name=f"waic-pick-box-{robot_id}")
        t.start()

        return _make_action_response(
            True, action_type,
            message=f"动作 {action_type} 执行成功",
        )

    def handle_pick_up_box(self, cmd_data: Dict) -> Dict:
        """
        PICK_UP_BOX —— 异步任务：导航到 area 后执行抓取（HTTP 立即返回）。

        params 字段：
            robot_id : 执行任务的机器人 ID（可选，默认 "robot_a"）
            area     : 目标点位名称（对应 navigation_poses.WAIC 中的 key）
        """
        action_type = "PICK_UP_BOX"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                '''if not is_robot_at_pose(
                    robot, get_nav_pose(area),
                    WAICNavTolerance.DISTANCE, WAICNavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        WAICStep.NAVIGATING, f"导航到 {area}"
                    )
                    if not self._navigate(robot, area, area, robot_id, task_id):
                        return
                else:
                    logger.info("WAIC", f"[{robot_id}] 机器人已在 {area} 点位，跳过导航")'''

                self.task_state_machine.update_step_label(
                    WAICStep.PICKING_UP, f"抓取箱子（{area}）"
                )
                result = robot.send_service_request_task(
                    WAICService.ROBOT_TASK, task=WAICTask.PICK_UP_BOX,
                    area=area, maxtime=WAICTimeout.ROBOT_ACTION,
                )
                if not result:
                    err = f"[{robot_id}] pick_up_box 失败: {result.error_msg}"
                    logger.error("WAIC", err)
                    self.task_state_machine.set_error(err)
                    return
                logger.info("WAIC", f"[{robot_id}] pick_up_box 成功")
                self.task_state_machine.complete_task(True, f"pick_up_box @ {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PICK_UP_BOX', success=True,
                    message=f"抓取完成: {area}",
                )
                logger.info("WAIC", f"[{robot_id}] PICK_UP_BOX 完成: {area}")
            except Exception as exc:
                err = f"[{robot_id}] PICK_UP_BOX 异常: {exc}"
                logger.exception_occurred("WAIC", "PICK_UP_BOX", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PICK_UP_BOX', success=False,
                    message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"waic-pick-up-{robot_id}"
        ).start()

        return _make_action_response(
            True, action_type,
            message=f"动作 {action_type} 已受理，后台执行中",
        )

    def handle_put_down_box(self, cmd_data: Dict) -> Dict:
        """
        PUT_DOWN_BOX —— 异步任务：导航到 area 后执行放置（HTTP 立即返回）。

        params 字段：
            robot_id : 执行任务的机器人 ID（可选，默认 "robot_a"）
            area     : 目标点位名称（对应 navigation_poses.WAIC 中的 key）
        """
        action_type = "PUT_DOWN_BOX"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                '''if not is_robot_at_pose(
                    robot, get_nav_pose(area),
                    WAICNavTolerance.DISTANCE, WAICNavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        WAICStep.NAVIGATING, f"导航到 {area}"
                    )
                    if not self._navigate(robot, area, area, robot_id, task_id):
                        return
                else:
                    logger.info("WAIC", f"[{robot_id}] 机器人已在 {area} 点位，跳过导航")'''

                self.task_state_machine.update_step_label(
                    WAICStep.PUTTING_DOWN, f"放置箱子（{area}）"
                )
                result = robot.send_service_request_task(
                    WAICService.ROBOT_TASK, task=WAICTask.PUT_DOWN_BOX,
                    area=area, maxtime=WAICTimeout.ROBOT_ACTION,
                )
                if not result:
                    err = f"[{robot_id}] put_down_box 失败: {result.error_msg}"
                    logger.error("WAIC", err)
                    self.task_state_machine.set_error(err)
                    return
                logger.info("WAIC", f"[{robot_id}] put_down_box 成功")
                self.task_state_machine.complete_task(True, f"put_down_box @ {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PUT_DOWN_BOX', success=True,
                    message=f"放置完成: {area}",
                )
                logger.info("WAIC", f"[{robot_id}] PUT_DOWN_BOX 完成: {area}")
            except Exception as exc:
                err = f"[{robot_id}] PUT_DOWN_BOX 异常: {exc}"
                logger.exception_occurred("WAIC", "PUT_DOWN_BOX", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='PUT_DOWN_BOX', success=False,
                    message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"waic-put-down-{robot_id}"
        ).start()

        return _make_action_response(
            True, action_type,
            message=f"动作 {action_type} 已受理，后台执行中",
        )

    def handle_navigation(self, cmd_data: Dict) -> Dict:
        """
        NAVIGATION —— 异步任务：从当前位置导航到目标 area（HTTP 立即返回）。

        params 字段：
            robot_id : 执行任务的机器人 ID（可选，默认 "robot_a"）
            area     : 目标点位名称（对应 navigation_poses.WAIC 中的 key）
        """
        action_type = "NAVIGATION"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                if not is_robot_at_pose(
                    robot, get_nav_pose(area),
                    WAICNavTolerance.DISTANCE, WAICNavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        WAICStep.NAVIGATING, f"导航到 {area}"
                    )
                    if not self._navigate(robot, area, area, robot_id, task_id):
                        return
                else:
                    logger.info("WAIC", f"[{robot_id}] 机器人已在 {area} 点位，跳过导航")

                self.task_state_machine.complete_task(True, f"导航到 {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='NAVIGATION', success=True,
                    message=f"导航完成: {area}",
                )
                logger.info("WAIC", f"[{robot_id}] NAVIGATION 完成: {area}")
            except Exception as exc:
                err = f"[{robot_id}] NAVIGATION 异常: {exc}"
                logger.exception_occurred("WAIC", "NAVIGATION", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type='NAVIGATION', success=False,
                    message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"waic-navigation-{robot_id}"
        ).start()

        return _make_action_response(
            True, action_type,
            message=f"动作 {action_type} 已受理，后台执行中",
        )
