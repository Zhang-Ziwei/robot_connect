"""
机器人导航工具模块

封装与导航相关的 ROS Bridge 交互功能，包括：
- wait_for_topic_message       : 等待并获取 topic 消息（带断线重连）
- wait_for_navigation_finished : 等待导航完成
- navigate_to_home_before_task : 充电中接到任务时先导航回 home 点
- send_navigation_action       : 发送导航 Action 目标（复用 robot.send_action）
- cancel_navigation_action     : 取消导航 Action
- get_robot_odom               : 读取机器人当前里程计位姿（Odom_Info 字典）
- is_robot_at_pose             : 判断机器人是否在目标点位附近（可跳过重复导航）

导航 Action 定义 (Navigation.action):
    Goal:
        - header: std_msgs/Header
        - task_type: TaskType (任务类型)
        - waypoints: Waypoint[] (路径点列表，最后一个为目标点)
        - translation: Translation (平移要求)
    
    Result:
        - header: std_msgs/Header
        - duration: 用时
        - distance_deviation: 到达距离偏差(m)
        - heading_deviation: 到达航向偏差(rad)
        - state: NavigationState (Succeeded/Failed/Cancelled/Aborted)
        - causes: ErrorInfo[] (未成功原因)
    
    Feedback:
        - header: std_msgs/Header
        - state: NavigationState (算法状态)
        - faults: ErrorInfo[] (故障信息)
"""

import time
import json
import asyncio
import threading
from typing import Dict, Optional, List, Tuple, Callable, Any, TYPE_CHECKING
from dataclasses import dataclass, field

from infrastructure.constants import (
    ROSTopic,
    ROSService,
    ROSTopicMessageType,
    ROSServiceMessageType,
    NavigationState,
    NavigationTaskType as TaskType,
    NAV_STATE_SUCCEEDED,
    NAV_STATE_FAILED,
    ACTIONLIB_GOAL_STATUS_ACTIVE,
)
from infrastructure.error_logger import get_error_logger
from hardware.action_utils import ActionSpec

if TYPE_CHECKING:
    from hardware.robot_controller import RobotController

logger = get_error_logger()


#: 导航 Action 的 ROS actionlib topic 终结点定义。
#: 话题名与消息类型集中在 ``infrastructure.constants``，这里只做聚合绑定。
NAVIGATION_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.NAVIGATION_ACTION_GOAL,
    feedback_topic=ROSTopic.NAVIGATION_ACTION_FEEDBACK,
    result_topic=ROSTopic.NAVIGATION_ACTION_RESULT,
    cancel_topic=ROSTopic.NAVIGATION_ACTION_CANCEL,
    status_topic=ROSTopic.NAVIGATION_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.NAVIGATION_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.NAVIGATION_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.NAVIGATION_ACTION_RESULT,
)


@dataclass
class Waypoint:
    """导航路点"""
    x: float
    y: float
    z: float = 0.0
    orientation_x: float = 0.0
    orientation_y: float = 0.0
    orientation_z: float = 0.0
    orientation_w: float = 1.0
    distance_tolerance: float = 0.0
    heading_tolerance: float = 0.0
    
    def to_dict(self) -> Dict:
        """转换为 ROS 消息格式"""
        return {
            "pose": {
                "position": {"x": self.x, "y": self.y, "z": self.z},
                "orientation": {
                    "x": self.orientation_x,
                    "y": self.orientation_y,
                    "z": self.orientation_z,
                    "w": self.orientation_w
                }
            },
            "distance_tolerance": self.distance_tolerance,
            "heading_tolerance": self.heading_tolerance
        }


@dataclass
class NavigationGoal:
    """导航目标"""
    waypoints: List[Waypoint]
    task_type: TaskType = TaskType.ROUTINE
    frame_id: str = "map"
    translation_enable: bool = False
    translation_heading: float = 0.0
    
    def to_inner_goal_dict(self) -> Dict:
        """
        转换为 Navigation.msg 的 Goal 内层字段。
        
        对应 ROS 定义:
            std_msgs/Header header
            TaskType task_type
            Waypoint[] waypoints
            Translation translation
        """
        current_time = time.time()
        secs = int(current_time)
        nsecs = int((current_time - secs) * 1e9)
        
        return {
            "header": {
                "seq": 0,
                "stamp": {"secs": secs, "nsecs": nsecs},
                "frame_id": self.frame_id
            },
            "task_type": {"value": int(self.task_type)},
            "translation": {
                "enable": self.translation_enable,
                "heading": self.translation_heading
            },
            "waypoints": [wp.to_dict() for wp in self.waypoints]
        }
    
    def to_action_goal_dict(self, goal_id: str) -> Dict:
        """
        转换为 actionlib 协议的 NavigationActionGoal 消息。
        
        对应 ROS actionlib 协议:
            std_msgs/Header header
            actionlib_msgs/GoalID goal_id
            NavigationGoal goal
        """
        current_time = time.time()
        secs = int(current_time)
        nsecs = int((current_time - secs) * 1e9)
        
        return {
            "header": {
                "seq": 0,
                "stamp": {"secs": secs, "nsecs": nsecs},
                "frame_id": self.frame_id
            },
            "goal_id": {
                "stamp": {"secs": secs, "nsecs": nsecs},
                "id": goal_id
            },
            "goal": self.to_inner_goal_dict()
        }


@dataclass
class NavigationResult:
    """导航结果"""
    state: NavigationState = NavigationState.NONE
    duration_secs: float = 0.0
    distance_deviation: float = 0.0
    heading_deviation: float = 0.0
    causes: List[Dict] = field(default_factory=list)
    raw_result: Dict = field(default_factory=dict)
    
    @property
    def succeeded(self) -> bool:
        return self.state in NAV_STATE_SUCCEEDED
    
    @property
    def failed(self) -> bool:
        """除 succeeded 外都视为失败（含 cancelled / aborted / error ...）"""
        return self.state in NAV_STATE_FAILED
    
    def __bool__(self) -> bool:
        """
        让 `if result:` / `if not result:` 直接反映成败。当这个类的实例（result）出现在 布尔判断（if/not/and/or）中时，自动调用 __bool__，返回 True/False
        等价于 result.succeeded。
        """
        return self.succeeded


@dataclass
class NavigationFeedback:
    """导航反馈"""
    state: NavigationState = NavigationState.NONE
    faults: List[Dict] = field(default_factory=list)
    raw_feedback: Dict = field(default_factory=dict)


# ==================== 原有的 Topic 等待函数 ====================

def wait_for_topic_message(
    robot: "RobotController",
    topic_name: str,
    msg_type: str = ROSTopicMessageType.NAVIGATION_STATUS,
    timeout: float = 60.0,
    retry_on_disconnect: bool = True,
    sleep_time: float = 2.0,
) -> Optional[Dict]:
    """
    等待并获取 topic 消息，支持连接断开后自动重连。

    参数:
        robot             : RobotController 实例
        topic_name        : 要获取的 topic 名称
        msg_type          : 消息类型
        timeout           : 总超时时间（秒）
        retry_on_disconnect: 断线时是否等待重连后继续
        sleep_time        : 每次轮询的等待间隔（秒）

    返回:
        dict: topic 消息内容；超时或失败返回 None
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        if not robot.is_connected():
            if not retry_on_disconnect:
                logger.error("navigation_utils", "连接断开，放弃等待 topic 消息")
                return None

            logger.warning("navigation_utils", "检测到连接断开，开始重连...")
            print("⚠️  机器人连接已断开，开始重连...")

            reconnect_success = robot.connect()

            if not reconnect_success:
                logger.error("navigation_utils", "重连失败")
                print("✗ 机器人重连失败")
                return None

            logger.info("navigation_utils", "重连成功")
            print("✓ 机器人重连成功")
            time.sleep(sleep_time)

            logger.info("navigation_utils", f"重新订阅 topic: {topic_name}")
            print(f"[DEBUG] 开始重新订阅 topic: {topic_name}")

            subscribe_success = robot.subscribe_topic(
                topic_name=topic_name,
                msg_type=msg_type,
                throttle_rate=0,
                queue_length=1,
            )

            if not subscribe_success:
                logger.error("navigation_utils", "重新订阅失败")
                print("✗ Topic 重新订阅失败")
                return None

            logger.info("navigation_utils", "重新订阅成功")
            print("✓ Topic 重新订阅成功")
            print(f"[DEBUG] 等待 {sleep_time}s 让 topic 消息开始传输...")
            time.sleep(sleep_time // 2)

        msg = robot.get_topic_message(topic_name, msg_type, sleep_time)
        if msg:
            return msg

        time.sleep(sleep_time)

    logger.warning("navigation_utils", f"等待 topic 消息超时: {topic_name}")
    return None


def wait_for_navigation_finished(
    robot: "RobotController",
    task_state_machine=None,
) -> bool:
    """
    轮询导航状态 topic，直到导航成功/失败为止。

    参数:
        robot              : RobotController 实例
        task_state_machine : 可选，失败时调用 set_error() 更新任务状态

    返回:
        True  : 导航成功（taskstate==SUCCESS 且 state==FINISHED/CHARGE_FINISHED）
        False : 导航失败或连接异常过多
    """
    status_names = {
        0: "NONE",
        1: "STANDBY，导航待机中",
        2: "PLANNING，导航规划中",
        3: "RUNNING，导航运行中",
        4: "STOPPING，导航停止中",
        5: "FINISHED，导航完成",
        6: "FAILURE，导航失败",
        7: "CHARGE, 前往充电",
        8: "CHARGE_FINISHED, 到达充电点位",
        9: "PHASE_FINISHED, 阶段完成",
    }
    taskstate_names = {
        0: "NONE",
        1: "RUNNING，运行中",
        2: "SUCCESS，运行成功",
        3: "FAILED，运行失败",
    }

    status_code_temp = 0
    error_count = 0
    connection_error_count = 0
    max_connection_errors = 30  # 约 5 分钟

    while True:
        nav_status = wait_for_topic_message(
            robot=robot,
            topic_name=ROSTopic.NAVIGATION_STATUS,
            msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
            timeout=10,
            retry_on_disconnect=True,
        )

        if nav_status is None:
            connection_error_count += 1
            if connection_error_count >= max_connection_errors:
                msg = f"导航状态获取失败次数过多 ({connection_error_count} 次)，放弃等待"
                logger.error("navigation_utils", msg)
                if task_state_machine:
                    task_state_machine.set_error("连接异常，无法获取导航状态")
                return False

            logger.warning(
                "navigation_utils",
                f"获取导航状态失败 ({connection_error_count}/{max_connection_errors})，继续等待...",
            )
            print(f"⚠️  获取导航状态失败 ({connection_error_count}/{max_connection_errors})，继续等待...")
            time.sleep(1)
            continue

        connection_error_count = 0
        status_code = nav_status.get("state", 0).get("value", 0)
        status_name = status_names.get(status_code, f"UNKNOWN({status_code})")
        taskstate_code = nav_status.get("taskstate", 0).get("value", 0)
        taskstate_name = taskstate_names.get(taskstate_code, f"UNKNOWN({taskstate_code})")

        if status_code_temp != taskstate_code:
            if error_count < 10:
                logger.info("navigation_utils", f"导航状态已更新: {taskstate_code} ({taskstate_name})")
                print(f"✓ 导航状态已更新: {taskstate_code} - {taskstate_name}")
                logger.info("navigation_utils", f"state 导航状态: {status_code} ({status_name})")
                print(f"✓ state 导航状态: {status_code} - {status_name}")
            status_code_temp = taskstate_code

            if taskstate_code == 2 and status_code in (5, 8):
                return True
            elif taskstate_code == 3:
                error_count += 1
                if error_count > 1000:
                    logger.error("navigation_utils", "导航失败")
                    if task_state_machine:
                        task_state_machine.set_error("导航失败")
                    print(f"导航失败 {error_count} 次")
                    return False


def navigate_to_home_before_task(
    robot: "RobotController",
    robot_id: str,
    home_pose: str,
    task_state_machine=None,
) -> bool:
    """
    机器人充电中接收到任务时，先导航回 home 点位再执行任务。

    参数:
        robot              : RobotController 实例
        robot_id           : 机器人 ID（用于日志和电池状态更新）
        home_pose          : home 点位名称
        task_state_machine : 可选，用于导航失败时更新状态

    返回:
        True  : 已到达 home 点位
        False : 导航失败
    """
    from infrastructure.constants import ROSTopic, ROSTopicMessageType

    try:
        logger.info("navigation_utils", f"{robot_id} 充电中接收到任务，先导航到 home 点位: {home_pose}")
        print(f"🏠 {robot_id} 充电中接收到任务，先返回 home 点位: {home_pose}")

        robot.send_service_request(
            robot.get_robot_service(),
            "navigation_prepare",
            extra_params={"area": "home"},
        )

        robot.publish_topic(
            topic_name=ROSTopic.NAVIGATION_CONTROL,
            msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
            msg_data={"data": home_pose},
        )

        nav_result = wait_for_navigation_finished(robot, task_state_machine)
        if not nav_result:
            logger.error("navigation_utils", f"{robot_id} 导航到 home 点位失败")
            print(f"❌ {robot_id} 导航到 home 点位失败")
            return False

        time.sleep(1.5)
        logger.info("navigation_utils", f"{robot_id} 已到达 home 点位: {home_pose}")
        print(f"✅ {robot_id} 已到达 home 点位: {home_pose}")

        # 更新电池监控器状态为 NORMAL
        from hardware.battery_monitor import get_battery_monitor, RobotBatteryState
        battery_monitor = get_battery_monitor()
        if battery_monitor:
            state = battery_monitor.battery_states.get(robot_id)
            if state:
                state.state = RobotBatteryState.NORMAL
                logger.info("navigation_utils", f"{robot_id} 电池状态已更新为 NORMAL")

        return True

    except Exception as e:
        logger.exception_occurred("navigation_utils", f"{robot_id} 导航到 home 点位", e)
        return False


# ==================== 导航 Action 调用函数 ====================

def build_navigation_goal(
    waypoints: List[Tuple[float, float, float]],
    task_type: TaskType = TaskType.ROUTINE,
    frame_id: str = "map",
    distance_tolerance: float = 0.0,
    heading_tolerance: float = 0.0,
    translation_enable: bool = False,
    translation_heading: float = 0.0,
) -> NavigationGoal:
    """
    构建导航目标。

    参数:
        waypoints           : 路点列表，每项为 (x,y,z) 或 (x,y,z,qx,qy,qz,qw)；多点路径用 list 定义
        task_type           : 任务类型
        frame_id            : 坐标系
        distance_tolerance  : 位置容差（米）
        heading_tolerance   : 航向容差（弧度）
        translation_enable  : 是否启用平移
        translation_heading : 平移航向

    返回:
        NavigationGoal 对象
    """
    wp_list = []
    for wp in waypoints:
        if len(wp) == 3:
            x, y, z = wp
            wp_obj = Waypoint(
                x=x, y=y, z=z,
                distance_tolerance=distance_tolerance,
                heading_tolerance=heading_tolerance
            )
        elif len(wp) >= 7:
            x, y, z, qx, qy, qz, qw = wp[:7]
            wp_obj = Waypoint(
                x=x, y=y, z=z,
                orientation_x=qx, orientation_y=qy, orientation_z=qz, orientation_w=qw,
                distance_tolerance=distance_tolerance,
                heading_tolerance=heading_tolerance
            )
        else:
            raise ValueError(f"Invalid waypoint format: {wp}")
        wp_list.append(wp_obj)

    return NavigationGoal(
        waypoints=wp_list,
        task_type=task_type,
        frame_id=frame_id,
        translation_enable=translation_enable,
        translation_heading=translation_heading
    )


def send_navigation_action(
    robot: "RobotController",
    goal: NavigationGoal,
    timeout: float = 300.0,
    feedback_callback: Optional[Callable[[NavigationFeedback], None]] = None,
    *,
    feedback_on_state_change_only: bool = False,
    feedback_min_interval: float = 0.0,
    retry_on_disconnect: bool = True,
    poll_interval: float = 0.2,
    max_attempts: int = 5,
    retry_error_code: int = 10009,
    retry_delay: float = 1.0,
) -> NavigationResult:
    """
    通过 ROS actionlib Topic 协议发送导航目标并等待结果。

    这是对通用 ``hardware.action_utils.send_action`` 的导航特化封装：
        - Action 终结点来自 ``NAVIGATION_ACTION_SPEC``
        - Result 里的 ``inner.state.value`` 映射为 ``NavigationState`` 来判定成败
        - 把通用 ``ActionFeedback`` 转成 ``NavigationFeedback`` 交给调用方

    使用标准 actionlib topics（rosbridge 全版本兼容）：
    - 发布 → /zj_humanoid/navigation/navigation/goal      (NavigationActionGoal)
    - 订阅 ← /zj_humanoid/navigation/navigation/feedback  (NavigationActionFeedback)
    - 订阅 ← /zj_humanoid/navigation/navigation/result    (NavigationActionResult)
    - 发布 → /zj_humanoid/navigation/navigation/cancel    (actionlib_msgs/GoalID)

    通过 goal_id 过滤 feedback/result，避免多 Action 互相干扰。

    参数:
        robot             : RobotController 实例
        goal              : NavigationGoal 导航目标
        timeout           : 等待结果超时时间（秒）
        feedback_callback : 反馈回调函数，接收 NavigationFeedback 对象
        feedback_on_state_change_only
                          : 为 True 时仅在 ``NavigationState`` 变化时调用
                            ``feedback_callback``（机器人高频发 feedback 时可显著减少日志）
        feedback_min_interval
                          : 大于 0 时，至少隔该秒数才可能再次调用 ``feedback_callback``；
                            若与 ``feedback_on_state_change_only`` 同时为 True，则**任一**条件满足
                            即调用（状态变化立即回调，或按间隔采样同状态的高频反馈）
        retry_on_disconnect: 断线时是否重连后继续等待当前 goal 的结果
        poll_interval     : 轮询 topic 消息的间隔（秒）
        max_attempts      : 导航业务失败且命中 retry_error_code 时最多重新发送 goal 的次数
        retry_error_code  : 可重试的导航业务错误码，默认 10009（目标容差不满足）
        retry_delay       : 两次重新导航之间的等待时间（秒）

    返回:
        NavigationResult 对象，包含执行结果

    使用示例:
        >>> goal = build_navigation_goal([(5.0, 3.0, 0.0)])
        >>> def on_feedback(fb):
        ...     print(f"状态: {fb.state.name}")
        >>> result = send_navigation_action(robot, goal, feedback_callback=on_feedback,
        ...     feedback_on_state_change_only=True)
        >>> if result.succeeded:
        ...     print("导航成功")
    """
    # 延迟 import，避免循环依赖
    from hardware.action_utils import (
        ActionFeedback as _AF,
        send_action as _send_action,
    )

    inner_goal = goal.to_inner_goal_dict()

    def _nav_success_check(status_code: int, inner: Dict) -> Tuple[bool, str]:
        state = _extract_nav_state(inner, status_code)
        if state in NAV_STATE_SUCCEEDED:
            return True, ""
        return False, f"state={state.name}"

    wrapped_cb: Optional[Callable[[_AF], None]] = None
    if feedback_callback is not None:
        last_logged_state = {"value": None}
        # 用户回调节流：与内置 print 解耦（print 仍仅在状态变化时打一条）
        cb_gate = {"last_state": None, "last_mono": 0.0}

        def _cb(af: _AF) -> None:
            state_val = (af.inner.get("state") or {}).get("value", None)
            if state_val is not None and state_val in NavigationState._value2member_map_:
                state = NavigationState(state_val)
            else:
                state = NavigationState.NONE
            if state != last_logged_state["value"]:
                last_logged_state["value"] = state
                logger.info("navigation_utils", f"导航反馈: state={state.name}")
                print(f"📶 导航反馈: state={state.name}")
            invoke_user = True
            now_m = time.monotonic()
            if feedback_on_state_change_only or feedback_min_interval > 0:
                state_changed = state != cb_gate["last_state"]
                interval_ok = (
                    feedback_min_interval <= 0
                    or cb_gate["last_mono"] <= 0
                    or (now_m - cb_gate["last_mono"]) >= feedback_min_interval
                )
                if feedback_on_state_change_only and feedback_min_interval > 0:
                    if not (state_changed or interval_ok):
                        invoke_user = False
                elif feedback_on_state_change_only:
                    if not state_changed:
                        invoke_user = False
                else:
                    if not interval_ok:
                        invoke_user = False
            if not invoke_user:
                return
            cb_gate["last_state"] = state
            cb_gate["last_mono"] = now_m
            try:
                feedback_callback(NavigationFeedback(
                    state=state,
                    faults=af.inner.get("faults", []) or [],
                    raw_feedback=af.inner,
                ))
            except Exception as e:
                logger.warning("navigation_utils", f"反馈回调异常: {e}")

        wrapped_cb = _cb

    attempts = max(1, int(max_attempts or 1))
    last_result: Optional[NavigationResult] = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            logger.warning(
                "navigation_utils",
                f"导航失败命中 code={retry_error_code}，第 {attempt}/{attempts} 次重新发送导航 goal",
            )
            print(f"⚠ 导航失败 code={retry_error_code}，重新导航 {attempt}/{attempts}")
            if retry_delay > 0:
                time.sleep(retry_delay)

        action_result = _send_action(
            robot=robot,
            spec=NAVIGATION_ACTION_SPEC,
            goal=inner_goal,
            timeout=timeout,
            feedback_callback=wrapped_cb,
            succeeded_check=_nav_success_check,
            retry_on_disconnect=retry_on_disconnect,
            poll_interval=poll_interval,
            frame_id=goal.frame_id,
        )
        last_result = _to_navigation_result(action_result)
        # 注意：NavigationResult.__bool__ == succeeded，失败结果也是有效对象，
        # 不能用 `if last_result:` / `last_result or ...`，否则会丢掉 causes。
        if last_result.succeeded:
            if attempt > 1:
                logger.info("navigation_utils", f"导航重试后成功: attempt={attempt}")
            return last_result

        codes = _extract_navigation_error_codes({
            "causes": last_result.causes,
            "raw_result": last_result.raw_result,
        })
        if retry_error_code not in codes:
            logger.warning(
                "navigation_utils",
                f"导航失败但未命中可重试错误码: state={last_result.state.name}, codes={sorted(codes)}",
            )
            return last_result

    logger.error(
        "navigation_utils",
        f"导航失败 code={retry_error_code} 已重试 {attempts} 次仍未成功",
    )
    return last_result if last_result is not None else NavigationResult(state=NavigationState.ABORTED)


def _extract_navigation_error_codes(value: Any) -> set:
    """
    从导航 Action 返回结构里递归提取错误码。

    真机导航失败时，错误码可能出现在 result.causes、raw_result 或嵌套的
    code / error_code / errorCode 字段中。这里不绑定具体结构，便于兼容
    固件响应字段微调。
    """
    codes = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("code", "error_code", "errorCode"):
                try:
                    codes.add(int(item))
                except (TypeError, ValueError):
                    pass
            codes.update(_extract_navigation_error_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.update(_extract_navigation_error_codes(item))
    return codes


def _extract_nav_state(inner: Dict, status_code: int) -> NavigationState:
    """
    从 Navigation.action Result 的内层字段提取业务 state。
    优先用 Result.state.value，退化到 actionlib GoalStatus。
    """
    inner = inner or {}
    inner_state_val = (inner.get("state") or {}).get("value", None)
    if inner_state_val is not None and inner_state_val in NavigationState._value2member_map_:
        return NavigationState(inner_state_val)
    if status_code in NavigationState._value2member_map_:
        return NavigationState(status_code)
    return NavigationState.ABORTED


def _to_navigation_result(action_result) -> NavigationResult:
    """通用 ActionResult → 领域 NavigationResult。"""
    inner = action_result.inner or {}
    state = _extract_nav_state(inner, action_result.status_code)

    # 若通用层已判成功（e.g. 状态在 NAV_STATE_SUCCEEDED 集合里）但 inner 没给 state，
    # 则退化为 SUCCEEDED；反之若彻底没信息则给 ABORTED。
    if action_result.succeeded and state not in NAV_STATE_SUCCEEDED:
        state = NavigationState.SUCCEEDED

    duration = inner.get("duration") or {}
    duration_secs = 0.0
    if isinstance(duration, dict):
        duration_secs = (duration.get("secs") or 0) + (duration.get("nsecs") or 0) / 1e9

    result = NavigationResult(
        state=state,
        duration_secs=duration_secs,
        distance_deviation=inner.get("distance_deviation", 0.0),
        heading_deviation=inner.get("heading_deviation", 0.0),
        causes=inner.get("causes", []) or [],
        raw_result=inner,
    )

    # 终态日志（保留原先行为）
    if result.succeeded:
        logger.info("navigation_utils", f"导航完成: state={state.name}")
        print(f"✅ 导航成功")
    else:
        logger.warning("navigation_utils", f"导航失败: state={state.name}")
        print(f"❌ 导航失败: state={state.name}")
        if result.causes:
            print(f"   原因: {result.causes}")
    return result


def cancel_navigation_action(
    robot: "RobotController",
    goal_id: str = "",
) -> bool:
    """
    取消正在执行的导航 Action。

    薄封装：委托到 ``hardware.action_utils.cancel_action(NAVIGATION_ACTION_SPEC, ...)``。
    通过发布到 /zj_humanoid/navigation/navigation/cancel (actionlib_msgs/GoalID)。

    参数:
        robot   : RobotController 实例
        goal_id : 要取消的 goal_id。空字符串表示取消所有 goal。

    使用示例:
        >>> cancel_navigation_action(robot)                   # 取消所有
        >>> cancel_navigation_action(robot, goal_id="xxx-yy") # 取消指定
    """
    from hardware.action_utils import cancel_action as _cancel
    return _cancel(robot, NAVIGATION_ACTION_SPEC, goal_id=goal_id)


def navigate_to_waypoints(
    robot: "RobotController",
    waypoints: List[Tuple[float, float, float]],
    task_type: TaskType = TaskType.ROUTINE,
    timeout: float = 600.0,
    feedback_callback: Optional[Callable[[NavigationFeedback], None]] = None,
    *,
    feedback_on_state_change_only: bool = False,
    feedback_min_interval: float = 0.0,
    retry_on_disconnect: bool = True,
) -> bool:
    """
    导航到指定路点的便捷函数（支持断线重连）。

    参数:
        robot             : RobotController 实例
        waypoints         : 路点列表 [(x1, y1, z1), ...]
        task_type         : 任务类型
        timeout           : 超时时间（秒）
        feedback_callback : 反馈回调
        feedback_on_state_change_only / feedback_min_interval
                          : 透传给 ``send_navigation_action``，用于降低回调频率
        retry_on_disconnect: 断线时是否重连

    返回:
        True: 导航成功
        False: 导航失败

    使用示例:
        >>> success = navigate_to_waypoints(robot, [(5.0, 3.0, 0.0)])
        >>> success = navigate_to_waypoints(robot, [
        ...     (1.0, 1.0, 0.0),
        ...     (5.0, 3.0, 0.0),
        ... ], timeout=180)
    """
    goal = build_navigation_goal(waypoints, task_type=task_type)
    result = send_navigation_action(
        robot=robot,
        goal=goal,
        timeout=timeout,
        feedback_callback=feedback_callback,
        feedback_on_state_change_only=feedback_on_state_change_only,
        feedback_min_interval=feedback_min_interval,
        retry_on_disconnect=retry_on_disconnect,
    )
    return result.succeeded


# ==================== 导航状态检测 & 地图下发 ====================

def is_robot_navigating(
    robot: "RobotController",
    timeout: float = 5.0,
) -> bool:
    """
    检查机器人当前是否在导航运动中。
    
    通过订阅 NAVIGATION_ACTION_STATUS（actionlib_msgs/GoalStatusArray）读取
    status_list，只要存在任意一个 status ∈ {PENDING, ACTIVE, PREEMPTING,
    RECALLING}（即 ACTIONLIB_GOAL_STATUS_ACTIVE）即认为正在导航。
    
    注意：机器人在导航完成后，上一个终态（如 status=3 SUCCEEDED）可能
    仍会在 status_list 中残留一段时间，因此只看"是否存在 active 状态"，
    而不是"是否全部为终态"。
    
    参数:
        robot   : RobotController 实例
        timeout : 等待 status 消息的超时时间（秒）
    
    返回:
        True  : 正在导航中
        False : 空闲（或超时未收到状态消息）
    """
    if not robot.is_connected():
        logger.warning("navigation_utils", "机器人未连接，无法检查导航状态")
        return False
    
    # 确保订阅了状态 topic（idempotent）
    robot.subscribe_topic(
        topic_name=ROSTopic.NAVIGATION_ACTION_STATUS,
        msg_type=ROSTopicMessageType.NAVIGATION_ACTION_STATUS,
        throttle_rate=0,
        queue_length=1,
    )
    
    # 清空旧缓存，确保读到的是最新的
    robot.topic_messages[ROSTopic.NAVIGATION_ACTION_STATUS] = None
    
    # 等待一条新消息
    start = time.time()
    msg = None
    while time.time() - start < timeout:
        cached = robot.topic_messages.get(ROSTopic.NAVIGATION_ACTION_STATUS)
        if cached:
            msg = cached
            break
        time.sleep(0.2)
    
    if not msg:
        logger.info(
            "navigation_utils",
            f"未收到 {ROSTopic.NAVIGATION_ACTION_STATUS} 消息（{timeout}s）→ 视为空闲",
        )
        return False
    
    status_list = msg.get("status_list", []) or []
    for entry in status_list:
        code = entry.get("status")
        if code in ACTIONLIB_GOAL_STATUS_ACTIVE:
            goal_id = entry.get("goal_id", {}).get("id", "?")
            logger.info(
                "navigation_utils",
                f"检测到导航进行中: goal_id={goal_id}, status={code}",
            )
            return True
    
    return False


def wait_for_navigation_idle(
    robot: "RobotController",
    timeout: float = 60.0,
    poll_interval: float = 1.0,
) -> bool:
    """
    等待机器人导航进入空闲状态（没有 active goal）。
    
    参数:
        robot         : RobotController 实例
        timeout       : 最长等待时间（秒）
        poll_interval : 轮询间隔（秒）
    
    返回:
        True  : 已进入空闲
        False : 超时
    """
    start = time.time()
    while time.time() - start < timeout:
        if not is_robot_navigating(robot, timeout=3.0):
            return True
        logger.info("navigation_utils", "导航运行中，等待空闲...")
        time.sleep(poll_interval)
    return False


def get_current_navigation_map(
    robot: "RobotController",
    service_timeout: float = 10.0,
) -> Optional[str]:
    """
    通过 ROSService.NAVIGATION_GET_MAP_INFO 查询机器人当前加载的导航地图名称。
    
    服务响应示例:
        {
            "code": 0,
            "message": "操作成功",
            "map_info": {"map_name": "test1", "map_metadata": {...}}
        }
    
    参数:
        robot           : RobotController 实例
        service_timeout : 服务调用超时（秒）
    
    返回:
        str  : 当前地图名称（成功查到）
        None : 查询失败或未加载地图
    """
    if not robot.is_connected():
        logger.warning("navigation_utils", "机器人未连接，无法查询当前地图")
        return None
    
    response = robot.call_service(
        service_name=ROSService.NAVIGATION_GET_MAP_INFO,
        args={},
        timeout=service_timeout,
    )
    
    if response is None:
        return None
    
    values = response.get("values", {})
    if not isinstance(values, dict):
        logger.warning("navigation_utils", f"get_cur_map_info 返回异常: {values}")
        return None
    
    code = values.get("code", -1)
    if code != 0:
        logger.info(
            "navigation_utils",
            f"get_cur_map_info code={code}, message={values.get('message', '')}",
        )
        return None
    
    map_info = values.get("map_info", {}) or {}
    return map_info.get("map_name") or None


def set_navigation_map(
    robot: "RobotController",
    map_name: str,
    wait_idle_timeout: float = 60.0,
    service_timeout: float = 30.0,
    skip_if_same: bool = True,
) -> Tuple[bool, str]:
    """
    通过 ROSService.NAVIGATION_MAP_CONFIG 设置导航地图。
    
    流程:
        1. (可选) 查询当前地图，若已是目标地图则跳过
        2. 检测导航是否在运行，若在运行则等待空闲（最长 wait_idle_timeout）
        3. 调用 set_map 服务下发地图
    
    服务请求:
        args = {"map_name": map_name}
    
    服务响应:
        {"map_name": str, "code": int, "message": str, "result": bool}
    
    参数:
        robot             : RobotController 实例
        map_name          : 地图/点位名称，如 "test1"
        wait_idle_timeout : 等待导航空闲最长时间（秒）
        service_timeout   : 服务调用超时（秒）
        skip_if_same      : 若当前地图与目标相同则跳过设置
    
    返回:
        (success, message) 元组
          success : True 表示地图设置成功（或已是目标地图）
          message : 机器人返回的 message 字段或错误描述
    """
    if not map_name:
        return False, "map_name 为空"
    
    if not robot.is_connected():
        return False, "机器人未连接"
    
    logger.info("navigation_utils", f"准备设置导航地图: {map_name}")
    print(f"🗺 准备设置导航地图: {map_name}")
    
    # 0. 先查询当前地图，若相同则跳过
    if skip_if_same:
        try:
            current_map = get_current_navigation_map(robot, service_timeout=10.0)
        except Exception as e:
            logger.warning("navigation_utils", f"查询当前地图异常: {e}")
            current_map = None
        
        if current_map is not None:
            print(f"ℹ 当前地图: {current_map}")
            if current_map == map_name:
                msg = f"当前已是目标地图 '{map_name}'，跳过设置"
                logger.info("navigation_utils", msg)
                print(f"✅ {msg}")
                return True, msg
    
    # 1. 等导航空闲
    if is_robot_navigating(robot, timeout=3.0):
        logger.info("navigation_utils", "导航运行中，等待空闲...")
        print("⏳ 导航运行中，等待空闲后再设置地图...")
        if not wait_for_navigation_idle(robot, timeout=wait_idle_timeout):
            msg = f"导航 {wait_idle_timeout}s 内未进入空闲，跳过地图设置"
            logger.warning("navigation_utils", msg)
            print(f"⚠ {msg}")
            return False, msg
    
    # 2. 调用 set_map 服务
    response = robot.call_service(
        service_name=ROSService.NAVIGATION_MAP_CONFIG,
        args={"map_name": map_name},
        timeout=service_timeout,
    )
    
    if response is None:
        msg = "调用 set_map 服务无响应"
        logger.error("navigation_utils", msg)
        print(f"❌ {msg}")
        return False, msg
    
    # rosbridge service_response 响应格式:
    #   {"op": "service_response", "service": "...", "values": {...}, "result": bool}
    result_ok = bool(response.get("result", False))
    values = response.get("values", {})
    
    # values 在失败时可能是字符串（rosbridge 错误信息）
    if not isinstance(values, dict):
        msg = str(values) if values else "未知错误"
        logger.error("navigation_utils", f"设置地图失败: {msg}")
        print(f"❌ 设置地图失败: {msg}")
        return False, msg
    
    ret_code = values.get("code", -1)
    ret_msg = values.get("message", "")
    ret_map_name = values.get("map_name", map_name)
    
    # 成功判定：rosbridge 的 result=True 且业务 code=0
    success = result_ok and (ret_code == 0)
    if success:
        logger.info(
            "navigation_utils",
            f"设置导航地图成功: map={ret_map_name}, code={ret_code}, message={ret_msg}",
        )
        print(f"✅ 设置导航地图成功: {ret_map_name} (code={ret_code})")
    else:
        logger.warning(
            "navigation_utils",
            f"设置导航地图失败: map={ret_map_name}, code={ret_code}, message={ret_msg}",
        )
        print(f"❌ 设置导航地图失败: code={ret_code}, message={ret_msg}")
    
    return success, ret_msg or f"code={ret_code}"


def set_navigation_localization(
    robot: "RobotController",
    map_name: str,
    method: str = "auto",
    map_path: str = "",
    x_pos: float = 0.0,
    y_pos: float = 0.0,
    z_pos: float = 0.0,
    x_ori: float = 0.0,
    y_ori: float = 0.0,
    z_ori: float = 0.0,
    w_ori: float = 0.0,
    check_timeout: float = 30.0,
    retry_interval: float = 10.0,
    max_retries: int = 5,
) -> Tuple[bool, str]:
    """
    调用导航重定位服务，并轮询定位状态直到成功或超时后重试。

    流程：
        1. 调用 ROSService.RELOC 服务（naviai_localization_msgs/Lio 参数格式）
        2. 订阅 ROSTopic.LOCATION_CODE，等待 status == 2
        3. 若超时未成功，等 retry_interval 秒后重试，最多 max_retries 次
           （max_retries=-1 表示无限重试）

    参数:
        robot          : RobotController 实例
        map_name       : 导航地图名（map_path 为空时作为 map_path 使用）
        method         : 重定位方法，固定为 "auto"
        map_path       : 地图文件路径/名称，留空则使用 map_name
        x_pos..w_ori   : 初始位置和姿态（全为 0 时执行全局重定位）
        check_timeout  : 每次等待定位成功的超时（秒）
        retry_interval : 失败后的重试间隔（秒）
        max_retries    : 最大重试次数（-1=无限重试）

    返回:
        (success: bool, message: str)
    """
    if not robot.is_connected():
        return False, "机器人未连接"

    effective_map_path = map_path.strip() if map_path else map_name

    service_args = {
        "method":   method,
        "map_path": effective_map_path,
        "x_pos":    x_pos,
        "y_pos":    y_pos,
        "z_pos":    z_pos,
        "x_ori":    x_ori,
        "y_ori":    y_ori,
        "z_ori":    z_ori,
        "w_ori":    w_ori,
    }

    attempt = 0
    while True:
        attempt += 1
        prefix = f"（第{attempt}次）" if attempt > 1 else ""
        logger.info(
            "navigation_utils",
            f"调用导航定位服务{prefix}: map_path={effective_map_path} method={method}",
        )
        print(f"📍 调用导航定位服务{prefix}: map_path={effective_map_path}")

        # 1. 调用重定位 service
        from infrastructure.constants import ROSService
        resp = robot.call_service(
            service_name=ROSService.RELOC,
            args=service_args,
            timeout=check_timeout,
        )
        if resp is None:
            msg = f"调用重定位服务超时或失败{prefix}"
            logger.error("navigation_utils", msg)
            if max_retries != -1 and attempt > max_retries:
                return False, msg
            print(f"⏳ {retry_interval}s 后重试...")
            time.sleep(retry_interval)
            continue

        # 2. 从服务响应中读取定位状态（1=初始化中，2=正常，3=异常）
        values = resp.get("values", {}) if resp else {}
        status = values.get("status")
        if status == 2:
            msg = f"导航定位成功{prefix}: map_path={effective_map_path}"
            logger.info("navigation_utils", msg)
            print(f"✅ 导航定位成功: map_path={effective_map_path}")
            return True, msg
        if status == 3:
            fail_msg = f"导航定位异常{prefix}: loc_status=3"
            logger.warning("navigation_utils", fail_msg)
            print(f"⚠ {fail_msg}")
            if max_retries != -1 and attempt > max_retries:
                return False, fail_msg
            print(f"⏳ {retry_interval}s 后重试...")
            time.sleep(retry_interval)
            continue

        # 服务调用成功但定位尚未就绪：轮询 LOCATION_CODE topic
        print(f"⏳ 定位服务已调用，等待定位完成（最长 {check_timeout}s）...")
        deadline = time.time() + check_timeout
        loc_status = None
        while time.time() < deadline:
            raw = wait_for_topic_message(
                robot,
                ROSTopic.LOCATION_CODE,
                msg_type=ROSTopicMessageType.LOCATION_CODE,
                timeout=5.0,
                retry_on_disconnect=False,
                sleep_time=1.0,
            )
            loc_status = raw.get("status") if raw else None
            if loc_status == 2:
                break
            if loc_status == 3:
                break  # 异常，退出轮询后由外层重发

        if loc_status == 2:
            msg = f"导航定位成功{prefix}: map_path={effective_map_path}"
            logger.info("navigation_utils", msg)
            print(f"✅ 导航定位成功: map_path={effective_map_path}")
            return True, msg

        fail_msg = f"导航定位未完成{prefix}: loc_status={loc_status!r}"
        logger.warning("navigation_utils", fail_msg)
        print(f"⚠ {fail_msg}")

        if max_retries != -1 and attempt > max_retries:
            return False, fail_msg

        print(f"⏳ {retry_interval}s 后重试...")
        time.sleep(retry_interval)


# ── 里程计查询 ────────────────────────────────────────────────────────────────

def get_robot_odom(
    robot: "RobotController",
    timeout: float = 5.0,
) -> Optional[Dict]:
    """
    获取机器人当前里程计位姿与速度（订阅 ``ROSTopic.ROBOT_MOTION_STATE``）。

    本函数是对 ``wait_for_topic_message`` + ROS nav_msgs/Odometry 解析的公共封装，
    供任意模块调用，无需通过 ``CmdHandler``，避免循环导入。

    参数:
        robot   : RobotController 实例
        timeout : 等待 topic 消息的超时时间（秒），默认 5 s

    返回:
        成功时返回 ``Odom_Info`` 字典::

            {
                "Position_Point_X_In": float | None,   # x (m)
                "Position_Point_Y_In": float | None,   # y (m)
                "Position_Point_Z_In": float | None,   # z (m)
                "Orientation_X_In":    float | None,   # 四元数 x
                "Orientation_Y_In":    float | None,   # 四元数 y
                "Orientation_Z_In":    float | None,   # 四元数 z
                "Orientation_W_In":    float | None,   # 四元数 w
                "Vector_Linear_X_In":  float | None,   # 线速度 x (m/s)
                "Vector_Linear_Y_In":  float | None,
                "Vector_Linear_Z_In":  float | None,
                "Vector_Angular_X_In": float | None,   # 角速度 x (rad/s)
                "Vector_Angular_Y_In": float | None,
                "Vector_Angular_Z_In": float | None,
            }

        失败（超时 / 连接断开 / 解析异常）时返回 ``None``。
    """
    try:
        raw = wait_for_topic_message(
            robot,
            ROSTopic.ROBOT_MOTION_STATE,
            msg_type=ROSTopicMessageType.ROBOT_MOTION_STATE,
            timeout=timeout,
            retry_on_disconnect=False,
            sleep_time=0,
        )
        if not raw:
            return None

        pose_outer  = raw.get("pose") or {}
        pose        = pose_outer.get("pose") or {}
        position    = pose.get("position") or {}
        orientation = pose.get("orientation") or {}

        twist_outer = raw.get("twist") or {}
        twist       = twist_outer.get("twist") or {}
        linear      = twist.get("linear") or {}
        angular     = twist.get("angular") or {}

        return {
            "Position_Point_X_In":  position.get("x"),
            "Position_Point_Y_In":  position.get("y"),
            "Position_Point_Z_In":  position.get("z"),
            "Orientation_X_In":     orientation.get("x"),
            "Orientation_Y_In":     orientation.get("y"),
            "Orientation_Z_In":     orientation.get("z"),
            "Orientation_W_In":     orientation.get("w"),
            "Vector_Linear_X_In":   linear.get("x"),
            "Vector_Linear_Y_In":   linear.get("y"),
            "Vector_Linear_Z_In":   linear.get("z"),
            "Vector_Angular_X_In":  angular.get("x"),
            "Vector_Angular_Y_In":  angular.get("y"),
            "Vector_Angular_Z_In":  angular.get("z"),
        }
    except Exception as e:
        logger.warning("navigation_utils", f"get_robot_odom 异常: {e}")
        return None


def get_robot_battery(
    robot: "RobotController",
    timeout: float = 5.0,
) -> Optional[Dict]:
    """
    获取机器人当前电池电量信息（订阅 ``ROSTopic.BATTERY_STATE``）。

    查找策略（优先使用已有缓存，避免重复订阅）：
        1. 若 ``BatteryMonitor`` 正在运行且已缓存电量，直接读取（零延迟）。
        2. 否则主动订阅 ``BATTERY_STATE`` topic，等待一次消息后返回。

    参数:
        robot   : RobotController 实例
        timeout : 等待 topic 消息的超时时间（秒），仅在直接订阅时有效，默认 5 s

    返回:
        成功时返回电池信息字典::

            {
                "percentage":         float,   # 电量百分比（0.0~1.0），如 0.85
                "percentage_display": str,     # 格式化显示，如 "85.0%"
                "voltage":            float,   # 电压（V），如 24.5
                "current":            float,   # 电流（A），负值=放电，正值=充电
                "power_supply_status": int,    # 0=未知 1=充电 2=放电 3=未充电 4=满电
                "source":             str,     # "monitor"（缓存）或 "topic"（直接订阅）
            }

        失败（超时 / 连接断开 / 解析异常）时返回 ``None``。
    """
    # ── 1. 优先从 BatteryMonitor 缓存读取 ────────────────────────────────────
    try:
        from hardware.battery_monitor import get_battery_monitor
        monitor = get_battery_monitor()
        if monitor:
            robot_id = getattr(robot, "robot_id", None)
            if robot_id:
                state = monitor.battery_states.get(robot_id)
                if state and state.percentage is not None:
                    pct = float(state.percentage)
                    return {
                        "percentage":          pct,
                        "percentage_display":  f"{pct * 100:.1f}%",
                        "voltage":             None,
                        "current":             None,
                        "power_supply_status": None,
                        "source":              "monitor",
                    }
    except Exception:
        pass

    # ── 2. 直接订阅 topic 读取 ────────────────────────────────────────────────
    try:
        raw = wait_for_topic_message(
            robot,
            ROSTopic.BATTERY_STATE,
            msg_type=ROSTopicMessageType.BATTERY_STATE,
            timeout=timeout,
            retry_on_disconnect=False,
            sleep_time=0,
        )
        if not raw:
            return None

        pct = float(raw.get("percentage", 0.0))
        return {
            "percentage":          pct,
            "percentage_display":  f"{pct * 100:.1f}%",
            "voltage":             raw.get("voltage"),
            "current":             raw.get("current"),
            "power_supply_status": raw.get("power_supply_status"),
            "source":              "topic",
        }
    except Exception as e:
        logger.warning("navigation_utils", f"get_robot_battery 异常: {e}")
        return None


def is_robot_at_pose(
    robot: "RobotController",
    target_pose,
    xy_tol:  float = 0.08,
    yaw_tol: float = 0.08,
    z_tol:   Optional[float] = None,
    timeout: float = 5.0,
) -> bool:
    """
    判断机器人当前位置是否在目标点位附近（用于跳过重复导航）。

    参数:
        robot       : RobotController 实例
        target_pose : 支持以下两种格式（与各项目 NavigationPose 定义保持一致）：

                      * 7 元素元组/列表 ``(x, y, z, qx, qy, qz, qw)`` — 单段终点
                      * list-of-tuples ``[(x,...), (x,...)]`` — 多段路径，自动取最后
                        一个路点（目标终点）做比较

        xy_tol      : XY 平面位置容差（米），默认 0.05 m
        yaw_tol     : 四元数 z/w 分量容差，默认 0.05
        z_tol       : Z 轴位置容差（米）；传 ``None``（默认）则不检查 Z 轴
        timeout     : 传给 get_robot_odom 的超时，默认 5 s

    返回:
        ``True``  — 机器人已在目标点位容差范围内，可安全跳过导航
        ``False`` — 不在点位，或 Odom 获取失败（保守决策，仍执行导航）

    使用示例::

        # 单段点位
        if is_robot_at_pose(robot, NavigationPose.P2):
            logger.info("nav", "已在 P2，跳过导航")

        # 多段路径（取终点比较）
        if is_robot_at_pose(robot, NavigationPose.P1_mid):
            logger.info("nav", "已在 P1_mid 终点，跳过导航")
    """
    if not target_pose:
        return False

    # 多段路径（list-of-tuples）：取最后一个路点作为目标终点
    if isinstance(target_pose, (list, tuple)) and isinstance(target_pose[0], (list, tuple)):
        target_pose = target_pose[-1]

    odom = get_robot_odom(robot, timeout=timeout)
    if odom is None:
        return False

    try:
        x_ok  = abs(float(odom.get("Position_Point_X_In") or 0) - target_pose[0]) < xy_tol
        y_ok  = abs(float(odom.get("Position_Point_Y_In") or 0) - target_pose[1]) < xy_tol
        qz_ok = abs(float(odom.get("Orientation_Z_In")    or 0) - target_pose[5]) < yaw_tol
        qw_ok = abs(float(odom.get("Orientation_W_In")    or 1) - target_pose[6]) < yaw_tol

        if z_tol is not None:
            z_ok = abs(float(odom.get("Position_Point_Z_In") or 0) - target_pose[2]) < z_tol
        else:
            z_ok = True

        return x_ok and y_ok and z_ok and qz_ok and qw_ok
    except (TypeError, ValueError) as e:
        logger.warning("navigation_utils", f"is_robot_at_pose 解析异常: {e}")
        return False
