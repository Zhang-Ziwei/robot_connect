"""
BaseCmdDispatcher — 通用命令分发器基类

职责：
  - 持有机器人字典、任务状态机、启动/重置事件等公共状态
  - 提供与具体项目无关的通用方法：
      机器人实例获取、电量检查、任务繁忙检查、home 点导航
  - 提供通用命令处理器：
      START_WORKING / RESET_SYSTEM / GET_TASK_STATE /
      ROBOT_ACTION / 5 个硬件传感器状态查询
  - handle_command 含休眠门卫 + handler_map 分发
  - handler_map 在 __init__ 时一次性构建并缓存，子类通过
    self._handler_map.update({...}) 注册自己的命令

设计原则：
  - 不导入任何业务项目（programs.*）
  - 不依赖 TJSH 特有资源（bottle_manager / task_optimizer 等）
  - 子类继承后通过 __init__ 扩展 handler_map，覆盖
    handle_reset_system 清理自身事件
"""

import threading
from typing import Any, Dict, Optional

from hardware.robot_controller import RobotController
from hardware.battery_monitor import get_battery_monitor, is_robot_available_for_task
from hardware.navigation_utils import (
    navigate_to_home_before_task,
    wait_for_navigation_finished,
    wait_for_topic_message,
    get_robot_odom,
    get_robot_battery,
)
from core.robot_actions import (
    handle_robot_action_command,
    is_robot_actions_enabled,
    get_available_actions,
)
from core.task_state_machine import get_task_state_machine, TaskStatus
from infrastructure.constants import (
    ErrorCode,
    ROSTopic,
    ROSTopicMessageType,
    get_main_ros_service,
    make_error_response,
    make_success_response,
)
from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import reload_config, display_config_info
from network.websocket_client import get_websocket_client

logger = get_error_logger()

# 系统生命周期：START_WORKING 已收到但尚未完成连接/地图下发时为 True
_system_activating = threading.Event()


def set_system_activating(active: bool) -> None:
    """标记系统是否处于激活流程中（供 main.py 与门卫协同）。"""
    if active:
        _system_activating.set()
    else:
        _system_activating.clear()


def is_system_activating() -> bool:
    return _system_activating.is_set()


class BaseCmdDispatcher:
    """
    通用命令分发器基类。

    子类使用方式：

        class MyCmdHandler(BaseCmdDispatcher):
            def __init__(self, robots=None):
                super().__init__(robots)
                # 初始化子类特有资源
                self._my_handler = MyHandler(robots=self.robots)
                # 注册子类命令（在 super().__init__ 之后）
                self._handler_map.update({
                    "MY_COMMAND": self._my_handler.handle_my_command,
                })

    休眠门卫：
        _PRE_STARTUP_ALLOWED 集合内的命令在 START_WORKING 前也允许通过。
        子类可覆盖该类属性以扩展白名单。
    """

    # 休眠门卫白名单：这些命令在 START_WORKING 前也允许通过
    _PRE_STARTUP_ALLOWED: frozenset = frozenset({
        "START_WORKING",
        "RESET_SYSTEM",
        "GET_TASK_STATE",
    })

    def __init__(self, robots: Dict[str, RobotController] = None):
        self.robots = robots or {}
        # 向后兼容引用
        self.robot_a = self.robots.get("robot_a")
        self.robot_b = self.robots.get("robot_b")
        # 状态机
        self.task_state_machine = get_task_state_machine()
        # 系统生命周期事件
        self.start_working_event = threading.Event()
        self.reset_system_event = threading.Event()
        # 构建并缓存命令分发表（子类在此之后扩展）
        self._handler_map: Dict = self._build_handler_map()

    # ──────────────────────────────────────────────────────────────────────────
    # 命令分发
    # ──────────────────────────────────────────────────────────────────────────

    def _build_handler_map(self) -> Dict:
        """
        构建通用命令分发表。
        子类不应覆盖此方法，而应在 __init__ 里调用
        self._handler_map.update({...}) 注册额外命令。
        """
        return {
            "START_WORKING":               self.handle_start_working,
            "RESET_SYSTEM":                self.handle_reset_system,
            "GET_TASK_STATE":              self.handle_get_task_state,
            "ROBOT_ACTION":                self.handle_robot_b_action,
            "GET_UPPER_LIMB_JOINT_STATES": self.handle_get_upper_limb_joint_states,
            "GET_HAND_JOINT_STATES":       self.handle_get_hand_joint_states,
            "GET_FINGER_PRESSURES":        self.handle_get_finger_pressures,
            "GET_FULL_BODY_SENSOR_STATES": self.handle_get_full_body_sensor_states,
            "GET_ROBOT_MOTION_STATE":      self.handle_get_robot_motion_state,
            "GET_BATTERY_STATE":           self.handle_get_battery_state,
        }

    def handle_command(self, cmd_data: Dict) -> Dict:
        """
        处理命令入口。

        流程：
          1. 休眠门卫：未收到 START_WORKING 前，只放行白名单命令
          2. 从 _handler_map 查找对应 handler
          3. 调用 handler，填充 cmd_id 和 code 字段后返回
        """
        cmd_type = cmd_data.get("cmd_type")
        cmd_id = cmd_data.get("cmd_id")

        if cmd_type != "GET_TASK_STATE":
            logger.info("命令处理器", f"收到命令: {cmd_type} (ID: {cmd_id})")

        # ── 休眠门卫 ─────────────────────────────────────────────────────────
        if cmd_type not in self._PRE_STARTUP_ALLOWED and not self.start_working_event.is_set():
            if is_system_activating():
                msg = "系统激活中，请稍后再试"
                logger.warning("命令处理器", f"[激活拦截] {cmd_type} - {msg}")
                print(f"\n⏳ [激活拦截] 收到 {cmd_type}，{msg}\n")
                return make_error_response(
                    ErrorCode.SYSTEM_ACTIVATING,
                    msg,
                    cmd_id=cmd_id,
                    cmd_type=cmd_type,
                )
            msg = "系统休眠中，请先发送 START_WORKING 命令启动系统"
            logger.warning("命令处理器", f"[休眠拦截] {cmd_type} - {msg}")
            print(f"\n⏸️  [休眠拦截] 收到 {cmd_type}，但系统尚未激活，{msg}\n")
            return make_error_response(
                ErrorCode.SYSTEM_SLEEPING,
                msg,
                cmd_id=cmd_id,
                cmd_type=cmd_type,
            )

        handler = self._handler_map.get(cmd_type)
        if not handler:
            error_msg = f"未知的命令类型: {cmd_type}"
            logger.error("命令处理器", error_msg)
            return make_error_response(ErrorCode.UNKNOWN_CMD_TYPE, error_msg, cmd_id=cmd_id)

        try:
            result = handler(cmd_data)
            result["cmd_id"] = cmd_id
            if "code" not in result:
                result["code"] = ErrorCode.SUCCESS if result.get("success") else ErrorCode.INTERNAL_ERROR
            return result
        except Exception as e:
            logger.exception_occurred("命令处理器", f"处理命令{cmd_type}", e)
            return make_error_response(
                ErrorCode.CMD_EXECUTION_ERROR,
                f"命令执行异常: {str(e)}",
                cmd_id=cmd_id,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # 机器人管理工具
    # ──────────────────────────────────────────────────────────────────────────

    def get_robot(self, robot_id: str) -> Optional[RobotController]:
        """
        根据 robot_id 获取机器人实例。

        参数:
            robot_id: 机器人 ID（如 "robot_a", "robot_b"）

        返回:
            RobotController 实例，不存在则返回 None
        """
        return self.robots.get(robot_id)

    def get_ros_service(self, robot_id: str) -> str:
        """
        根据机器人 ID 获取对应的主 ROS 服务名称。

        返回:
            对应的 ROS 服务名称
            - robot_a → STRAWBERRY_SERVICE
            - robot_b → CHEM_PROJECT_SERVICE
        """
        return get_main_ros_service(robot_id)

    def is_robot_busy(self, robot_id: str = None) -> bool:
        """
        检查机器人是否正在执行任务。

        参数:
            robot_id: 机器人 ID；为 None 则检查全局任务状态

        返回:
            True: 机器人正忙（RUNNING / WAITING）
            False: 机器人空闲
        """
        state = self.task_state_machine.get_state()
        current_status = state.get("status")
        current_robot = state.get("robot_id")

        if robot_id and current_robot and current_robot != robot_id:
            return False

        return current_status in [TaskStatus.RUNNING.value, TaskStatus.WAITING.value]

    def check_battery_availability(self, robot_id: str, cmd_type: str = None) -> dict:
        """
        检查机器人电量是否允许执行任务。

        参数:
            robot_id: 机器人 ID
            cmd_type: 命令类型（用于判断是否为免检命令）

        返回:
            {
                "error_response": 错误响应或 None,
                "need_go_home":   是否需要先回 home,
                "home_pose":      home 点位名称或 None
            }
        """
        EXEMPT_COMMANDS = ["GET_TASK_STATE", "GET_STATION_COUNTER"]

        if cmd_type in EXEMPT_COMMANDS:
            return {"error_response": None, "need_go_home": False, "home_pose": None}

        available, reason = is_robot_available_for_task(robot_id)
        battery_monitor = get_battery_monitor()
        battery_status = battery_monitor.get_battery_status(robot_id) if battery_monitor else None

        if available and reason is None:
            return {"error_response": None, "need_go_home": False, "home_pose": None}

        if available and reason == "charging_need_go_home":
            home_pose = battery_monitor.get_robot_home_pose(robot_id) if battery_monitor else "home"
            percentage = battery_status.get("percentage", 0) if battery_status else 0
            threshold = battery_monitor.get_charging_accept_task_threshold() if battery_monitor else 0.5
            logger.info(
                "命令处理器",
                f"{robot_id} 充电中 ({percentage*100:.1f}% >= {threshold*100:.0f}%)，"
                f"接受任务前需先返回home点位: {home_pose}",
            )
            return {"error_response": None, "need_go_home": True, "home_pose": home_pose}

        if reason == "battery_info_pending":
            return {
                "error_response": make_error_response(
                    ErrorCode.ROBOT_BATTERY_INFO_PENDING,
                    f"机器人 {robot_id} 电量信息未获取，请等待系统初始化完成",
                    robot_id=robot_id,
                    battery_status=battery_status,
                ),
                "need_go_home": False,
                "home_pose": None,
            }
        elif reason == "low_battery":
            percentage = battery_status.get("percentage", 0) if battery_status else 0
            return {
                "error_response": make_error_response(
                    ErrorCode.ROBOT_LOW_BATTERY,
                    f"机器人 {robot_id} 电量低 ({percentage*100:.1f}%)，等待充电完成",
                    robot_id=robot_id,
                    battery_status=battery_status,
                ),
                "need_go_home": False,
                "home_pose": None,
            }
        elif reason == "charging_below_threshold":
            percentage = battery_status.get("percentage", 0) if battery_status else 0
            threshold = battery_monitor.get_charging_accept_task_threshold() if battery_monitor else 0.5
            return {
                "error_response": make_error_response(
                    ErrorCode.ROBOT_CHARGING_REJECT_TASK,
                    f"机器人 {robot_id} 正在充电 ({percentage*100:.1f}%)，"
                    f"需等待电量达到 {threshold*100:.0f}% 才能接收新任务",
                    robot_id=robot_id,
                    battery_status=battery_status,
                ),
                "need_go_home": False,
                "home_pose": None,
            }

        return {"error_response": None, "need_go_home": False, "home_pose": None}

    def _navigate_to_home_before_task(self, robot_id: str, home_pose: str) -> bool:
        """在执行任务前导航到 home 点位（委托给 hardware.navigation_utils）"""
        robot = self.get_robot(robot_id)
        if not robot:
            logger.error("命令处理器", f"机器人 {robot_id} 不存在，无法导航到home")
            return False
        return navigate_to_home_before_task(robot, robot_id, home_pose, self.task_state_machine)

    # ──────────────────────────────────────────────────────────────────────────
    # 通用命令 handlers
    # ──────────────────────────────────────────────────────────────────────────

    def handle_start_working(self, cmd_data: Dict) -> Dict:
        """处理 START_WORKING 命令：激活程序，开始正常工作。"""
        logger.info("命令处理器", "收到START_WORKING命令，程序激活")
        self.start_working_event.set()
        return {"success": True, "message": "已接收START_WORKING命令，程序开始工作"}

    def handle_reset_system(self, cmd_data: Dict) -> Dict:
        """
        处理 RESET_SYSTEM 命令：重置系统到初始休眠状态。

        子类覆盖时请先清理项目特有事件，再调用 super().handle_reset_system(cmd_data)。

        通用步骤：
          1. 触发重置事件（通知 main.py）
          2. 停止所有机器人的重连并断开连接
          3. 清除 start_working_event
          4. 重置任务状态机
          5. 清空机器人字典
          6. 重新加载配置文件（config_loader / battery_monitor / websocket_client）
        """
        logger.info("命令处理器", "收到RESET_SYSTEM命令，开始重置系统...")
        try:
            # 1. 触发重置事件
            self.reset_system_event.set()

            # 2. 停止所有机器人
            for robot_id, robot in self.robots.items():
                if robot:
                    robot.stop_reconnect()
                    try:
                        if robot.is_connected():
                            robot.close()
                    except Exception:
                        pass
                    logger.info("命令处理器", f"已停止 {robot_id} 的重连")

            # 清除 start_working_event
            self.start_working_event.clear()
            set_system_activating(False)

            # 4. 重置状态机
            self.task_state_machine.reset()

            # 5. 清空机器人字典
            self.robots.clear()
            self.robot_a = None
            self.robot_b = None

            # 6. 重新加载配置
            logger.info("命令处理器", "正在重新加载配置文件...")
            reload_config()
            display_config_info()

            battery_monitor = get_battery_monitor()
            if battery_monitor:
                battery_monitor.reload_config()
                logger.info("命令处理器", "电池监控器配置已重新加载")

            websocket_client = get_websocket_client()
            if websocket_client:
                websocket_client.reload_config()
                logger.info("命令处理器", "WebSocket客户端配置已重新加载")

            logger.info("命令处理器", "系统重置完成，已进入休眠状态，等待START_WORKING命令激活")
            return {
                "success": True,
                "message": "系统已重置到休眠状态，配置文件已重新加载，发送START_WORKING命令可重新激活",
            }
        except Exception as e:
            logger.exception_occurred("命令处理器", "重置系统", e)
            return make_error_response(ErrorCode.SYSTEM_RESET_FAILED, f"系统重置失败: {str(e)}")

    def handle_get_task_state(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_TASK_STATE 命令：查询任务执行状态。

        params.target_cmd_id 存在时查询指定任务，否则查询当前任务。
        """
        params = cmd_data.get("params", {})
        target_cmd_id = params.get("target_cmd_id")

        if target_cmd_id:
            state = self.task_state_machine.get_state(target_cmd_id)
        else:
            state = self.task_state_machine.get_state()

        if state.get("status") == "未找到":
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                state.get("message"),
                current_task_id=state.get("current_task_id"),
            )

        return make_success_response("状态查询成功", data=state)

    def handle_robot_b_action(self, cmd_data: Dict) -> Dict:
        """
        处理 ROBOT_ACTION 命令：执行机器人单独动作（委托 core.robot_actions）。

        请求参数:
            action_type: 动作类型，如 "B_STEP_1"
            robot_id:    机器人 ID（默认 "robot_b"）
        """
        params = cmd_data.get("params", {})
        action_type = params.get("action_type")
        robot_id = params.get("robot_id", "robot_b")
        timeout = params.get("timeout", 600)

        logger.info(
            "命令处理器",
            f"ROBOT_ACTION - action_type: {action_type}, robot_id: {robot_id}, timeout: {timeout}",
        )

        if not is_robot_actions_enabled():
            return make_error_response(
                ErrorCode.ROBOT_ACTION_DISABLED,
                "机器人动作接口未启用，请在constants.py中设置ENABLE_ROBOT_B_ACTIONS=True",
                available_actions=get_available_actions(),
            )

        return handle_robot_action_command(
            {"action_type": action_type, "robot_id": robot_id, "timeout": timeout},
            self.robots,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 硬件传感器状态查询
    # ──────────────────────────────────────────────────────────────────────────

    def handle_get_upper_limb_joint_states(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_UPPER_LIMB_JOINT_STATES 命令 — 查询上肢关节状态。

        返回左臂、右臂、颈部、躯干的关节角度（键名为 *_IN；缺关节值为 null）:
        - 颈部: Neck_Z_In, Neck_Y_In
        - 躯干: Pitch_Y_B_In, Pitch_Y_M_In, Waist_Z_In, Waist_Y_In
        - 左臂: Shoulder_Z_L_In … Wrist_X_L_In（8 关节）
        - 右臂: Shoulder_Z_R_In … Wrist_X_R_In（8 关节）
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")

        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(ErrorCode.ROBOT_NOT_CONNECTED, f"机器人 {robot_id} 未连接")

        try:
            joint_states = self._wait_for_topic_message(
                ROSTopic.UPPER_LIMB_JOINT_STATES,
                ROSTopicMessageType.UPPER_LIMB_JOINT_STATES,
                timeout=5.0,
                retry_on_disconnect=False,
                robot=robot,
                sleep_time=0,
            )

            if not joint_states:
                return make_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"无法获取 {robot_id} 的上肢关节状态（超时5秒）",
                )

            names = joint_states.get("name", [])
            positions = joint_states.get("position", [])
            joint_data = {
                name: (positions[i] if i < len(positions) else None)
                for i, name in enumerate(names)
            }

            neck_joints      = ["Neck_Z",     "Neck_Y"]
            torso_joints     = ["Pitch_Y_B",  "Pitch_Y_M",  "Waist_Z",  "Waist_Y"]
            left_arm_joints  = ["Shoulder_Z_L","Shoulder_Y_L","Shoulder_X_L","Elbow_Z_L",
                                 "Elbow_Y_L",  "Wrist_Z_L",  "Wrist_Y_L", "Wrist_X_L"]
            right_arm_joints = ["Shoulder_Z_R","Shoulder_Y_R","Shoulder_X_R","Elbow_Z_R",
                                 "Elbow_Y_R",  "Wrist_Z_R",  "Wrist_Y_R", "Wrist_X_R"]

            neck_in      = ["Neck_Z_In",     "Neck_Y_In"]
            torso_in     = ["Pitch_Y_B_In",  "Pitch_Y_M_In",  "Waist_Z_In",  "Waist_Y_In"]
            left_arm_in  = ["Shoulder_Z_L_In","Shoulder_Y_L_In","Shoulder_X_L_In","Elbow_Z_L_In",
                             "Elbow_Y_L_In",  "Wrist_Z_L_In",  "Wrist_Y_L_In", "Wrist_X_L_In"]
            right_arm_in = ["Shoulder_Z_R_In","Shoulder_Y_R_In","Shoulder_X_R_In","Elbow_Z_R_In",
                             "Elbow_Y_R_In",  "Wrist_Z_R_In",  "Wrist_Y_R_In", "Wrist_X_R_In"]

            return {
                "success": True,
                "robot_id": robot_id,
                "data": {
                    "Head_Neck": {k: joint_data.get(o) for o, k in zip(neck_joints,      neck_in)},
                    "Torso":     {k: joint_data.get(o) for o, k in zip(torso_joints,     torso_in)},
                    "Left_Arm":  {k: joint_data.get(o) for o, k in zip(left_arm_joints,  left_arm_in)},
                    "Right_Arm": {k: joint_data.get(o) for o, k in zip(right_arm_joints, right_arm_in)},
                },
            }

        except Exception as e:
            logger.exception_occurred("命令处理器", "获取上肢关节状态", e)
            return make_error_response(ErrorCode.INTERNAL_ERROR, f"获取上肢关节状态失败: {str(e)}")

    def handle_get_hand_joint_states(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_HAND_JOINT_STATES 命令 — 查询手部关节状态。

        返回左手、右手的关节角度（键名 *_IN；缺关节值为 null）:
        - 左手: THUMB_MP_L_In … LITTLE_MCP_L_In（6 关节）
        - 右手: THUMB_MP_R_In … LITTLE_MCP_R_In（6 关节）
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")

        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(ErrorCode.ROBOT_NOT_CONNECTED, f"机器人 {robot_id} 未连接")

        try:
            joint_states = self._wait_for_topic_message(
                ROSTopic.HAND_JOINT_STATES,
                ROSTopicMessageType.HAND_JOINT_STATES,
                timeout=5.0,
                retry_on_disconnect=False,
                robot=robot,
                sleep_time=0,
            )

            joint_data = {}
            if joint_states:
                names = joint_states.get("name", [])
                positions = joint_states.get("position", [])
                joint_data = {
                    name: (positions[i] if i < len(positions) else None)
                    for i, name in enumerate(names)
                }

            left_joints  = ["THUMB_MP_LEFT",  "THUMB_CMC_LEFT",  "INDEX_MCP_LEFT",
                             "MIDDLE_MCP_LEFT","RING_MCP_LEFT",   "LITTLE_MCP_LEFT"]
            right_joints = ["THUMB_MP_RIGHT", "THUMB_CMC_RIGHT", "INDEX_MCP_RIGHT",
                             "MIDDLE_MCP_RIGHT","RING_MCP_RIGHT", "LITTLE_MCP_RIGHT"]
            left_in  = ["THUMB_MP_L_In",  "THUMB_CMC_L_In",  "INDEX_MCP_L_In",
                         "MIDDLE_MCP_L_In","RING_MCP_L_In",   "LITTLE_MCP_L_In"]
            right_in = ["THUMB_MP_R_In",  "THUMB_CMC_R_In",  "INDEX_MCP_R_In",
                         "MIDDLE_MCP_R_In","RING_MCP_R_In",   "LITTLE_MCP_R_In"]

            return {
                "success": True,
                "robot_id": robot_id,
                "data": {
                    "Left_Hand":  {k: joint_data.get(o) for o, k in zip(left_joints,  left_in)},
                    "Right_Hand": {k: joint_data.get(o) for o, k in zip(right_joints, right_in)},
                },
            }

        except Exception as e:
            logger.exception_occurred("命令处理器", "获取手部关节状态", e)
            return make_error_response(ErrorCode.INTERNAL_ERROR, f"获取手部关节状态失败: {str(e)}")

    def handle_get_finger_pressures(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_FINGER_PRESSURES 命令 — 查询手指压力传感器。

        返回左手、右手手指压力（键名 *_IN；缺数据值为 null）:
        - 左手: Finger_Pressures_Thumb_L_In … Finger_Pressures_Little_L_In（5 路）
        - 右手: Finger_Pressures_Thumb_R_In … Finger_Pressures_Little_R_In（5 路）

        请求参数:
            robot_id: 机器人 ID（默认 robot_a）
            hand:     "left" / "right" / 不传（两只手都查）
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")
        hand = params.get("hand")

        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(ErrorCode.ROBOT_NOT_CONNECTED, f"机器人 {robot_id} 未连接")

        try:
            result_data = {}

            left_names_in = [
                "Finger_Pressures_Thumb_L_In",  "Finger_Pressures_Index_L_In",
                "Finger_Pressures_Middle_L_In",  "Finger_Pressures_Ring_L_In",
                "Finger_Pressures_Little_L_In",
            ]
            right_names_in = [
                "Finger_Pressures_Thumb_R_In",  "Finger_Pressures_Index_R_In",
                "Finger_Pressures_Middle_R_In",  "Finger_Pressures_Ring_R_In",
                "Finger_Pressures_Little_R_In",
            ]

            if hand is None or hand == "left":
                raw = self._wait_for_topic_message(
                    ROSTopic.FINGER_PRESSURES_LEFT,
                    ROSTopicMessageType.FINGER_PRESSURES_LEFT,
                    timeout=5.0, retry_on_disconnect=False, robot=robot, sleep_time=0,
                )
                pv = raw.get("pressure", []) if raw else []
                result_data["Left_Hand_Finger_Pressures"] = {
                    k: (pv[i] if i < len(pv) else None) for i, k in enumerate(left_names_in)
                }

            if hand is None or hand == "right":
                raw = self._wait_for_topic_message(
                    ROSTopic.FINGER_PRESSURES_RIGHT,
                    ROSTopicMessageType.FINGER_PRESSURES_RIGHT,
                    timeout=5.0, retry_on_disconnect=False, robot=robot, sleep_time=0,
                )
                pv = raw.get("pressure", []) if raw else []
                result_data["Right_Hand_Finger_Pressures"] = {
                    k: (pv[i] if i < len(pv) else None) for i, k in enumerate(right_names_in)
                }

            if not result_data:
                return make_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"无法获取 {robot_id} 的手指压力传感器数据",
                )

            return {"success": True, "robot_id": robot_id, "data": result_data}

        except Exception as e:
            logger.exception_occurred("命令处理器", "获取手指压力传感器", e)
            return make_error_response(ErrorCode.INTERNAL_ERROR, f"获取手指压力传感器失败: {str(e)}")

    def handle_get_full_body_sensor_states(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_FULL_BODY_SENSOR_STATES 命令 — 聚合上肢关节、手部关节、手指压力。

        等价于依次调用 GET_UPPER_LIMB_JOINT_STATES / GET_HAND_JOINT_STATES /
        GET_FINGER_PRESSURES，将三者 data 合并后返回。

        任一部分失败时 success 为 False，errors 给出对应说明。
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")

        r_upper  = self.handle_get_upper_limb_joint_states(cmd_data)
        r_hand   = self.handle_get_hand_joint_states(cmd_data)
        r_finger = self.handle_get_finger_pressures(cmd_data)

        ok = r_upper.get("success") and r_hand.get("success") and r_finger.get("success")
        data = {
            "upper_limb_joint_states": r_upper.get("data"),
            "hand_joint_states":       r_hand.get("data"),
            "finger_pressures":        r_finger.get("data"),
        }

        errors: Dict = {}
        if not r_upper.get("success"):
            errors["upper_limb_joint_states"] = r_upper.get("message", "查询失败")
        if not r_hand.get("success"):
            errors["hand_joint_states"] = r_hand.get("message", "查询失败")
        if not r_finger.get("success"):
            errors["finger_pressures"] = r_finger.get("message", "查询失败")

        first_fail_code = ErrorCode.INTERNAL_ERROR
        for r in (r_upper, r_hand, r_finger):
            if not r.get("success") and r.get("code") is not None:
                first_fail_code = r["code"]
                break

        result: Dict[str, Any] = {
            "success": ok,
            "code":    ErrorCode.SUCCESS if ok else first_fail_code,
            "robot_id": robot_id,
            "data":    data,
        }
        if not ok:
            result["message"] = "部分或全部传感器查询失败"
            result["errors"]  = errors

        logger.info("命令处理器", f"GET_FULL_BODY_SENSOR_STATES robot_id={robot_id} success={ok}")
        return result

    def handle_get_robot_motion_state(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_ROBOT_MOTION_STATE 命令 — 查询机器人运动状态（里程计）。

        返回位置、姿态、线速度、角速度（键名 *_In；超时时值为 null）。
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")

        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(ErrorCode.ROBOT_NOT_CONNECTED, f"机器人 {robot_id} 未连接")

        try:
            odom = get_robot_odom(robot, timeout=5.0)
            if odom is None:
                return make_error_response(ErrorCode.INTERNAL_ERROR, "获取里程计超时或连接断开")
            return {"success": True, "robot_id": robot_id, "data": {"Odom_Info": odom}}
        except Exception as e:
            logger.exception_occurred("命令处理器", "获取机器人运动状态", e)
            return make_error_response(ErrorCode.INTERNAL_ERROR, f"获取机器人运动状态失败: {str(e)}")

    def handle_get_battery_state(self, cmd_data: Dict) -> Dict:
        """
        处理 GET_BATTERY_STATE 命令 — 查询机器人电池电量。

        查找策略（优先缓存，降低延迟）：
          1. 若 BatteryMonitor 正在运行且已缓存，直接返回（零延迟）。
          2. 否则主动订阅 BATTERY_STATE topic，等待一次消息后返回。

        返回::

            {
                "success": true,
                "robot_id": "robot_a",
                "data": {
                    "percentage":         0.85,
                    "percentage_display": "85.0%",
                    "voltage":            24.5,
                    "current":           -2.3,
                    "power_supply_status": 2,
                    "source":             "monitor"
                }
            }
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")

        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(ErrorCode.ROBOT_NOT_CONNECTED, f"机器人 {robot_id} 未连接")

        try:
            battery = get_robot_battery(robot, timeout=5.0)
            if battery is None:
                return make_error_response(ErrorCode.INTERNAL_ERROR, "获取电量信息超时或连接断开")
            return {"success": True, "robot_id": robot_id, "data": battery}
        except Exception as e:
            logger.exception_occurred("命令处理器", "获取机器人电池电量", e)
            return make_error_response(ErrorCode.INTERNAL_ERROR, f"获取电池电量失败: {str(e)}")

    # ──────────────────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────────────────

    def _wait_for_navigation_finished(self, robot: RobotController = None) -> bool:
        """等待导航完成（委托给 hardware.navigation_utils）"""
        if robot is None:
            robot = self.robot_a
        return wait_for_navigation_finished(robot, self.task_state_machine)

    def _wait_for_topic_message(
        self,
        topic_name: str,
        msg_type: str = ROSTopicMessageType.NAVIGATION_STATUS,
        timeout: float = 60.0,
        retry_on_disconnect: bool = True,
        robot: RobotController = None,
        sleep_time: float = 2.0,
    ) -> Optional[Dict]:
        """等待并获取 topic 消息（委托给 hardware.navigation_utils）"""
        if robot is None:
            robot = self.robot_a
        return wait_for_topic_message(robot, topic_name, msg_type, timeout, retry_on_disconnect, sleep_time)
