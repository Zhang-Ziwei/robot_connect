"""
通用 ROS Actionlib Topic 协议客户端（**纯传输层，不含任何业务语义**）。

这个模块只负责把"发送 Goal → 订阅 Feedback/Result → 处理取消与重连"这件事
抽象成一个与具体 Action 无关的 `send_action()` 函数。**它完全不知道**
"这是导航还是操作"、"怎么判定业务成功"、"return_params 长什么样"——
这些知识全部归属于上层的领域模块：

    - 导航: ``hardware.navigation_utils`` (NAVIGATION_ACTION_SPEC + send_navigation_action)
    - 操作: ``hardware.task_utils``       (CHEM_PROJECT_ACTION_SPEC + send_task_action)

所有 Action 都遵循标准 rosbridge actionlib topic 协议：

    发 → <base>/goal      <pkg>/<Name>ActionGoal     {header, goal_id, goal}
    发 → <base>/cancel    actionlib_msgs/GoalID      {stamp, id}
    订 ← <base>/feedback  <pkg>/<Name>ActionFeedback {header, status, feedback}
    订 ← <base>/result    <pkg>/<Name>ActionResult   {header, status, result}
    订 ← <base>/status    actionlib_msgs/GoalStatusArray

只要把一个 Action 的 5 个 topic 名 + 3 个消息类型用 ``ActionSpec`` 描述清楚，
外加一个业务成功判定函数 ``succeeded_check(status_code, inner_result)``，
就可以复用 ``send_action()`` 完成一次完整的 Goal-Feedback-Result 循环。

> **什么时候直接用这里、什么时候用领域模块？**
> - 已有领域封装（navigation / task）→ 用领域模块，得到领域数据结构。
> - 新增一个**完全新的** Action（契约不同于上面两种）→ 直接 ``send_action()``
>   + 自定义 ``succeeded_check``。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Tuple, TYPE_CHECKING

from infrastructure.error_logger import get_error_logger

if TYPE_CHECKING:
    from hardware.robot_controller import RobotController

logger = get_error_logger()


# ==================== 数据结构 ====================

@dataclass
class ActionSpec:
    """
    描述一个 ROS Action 的全部通信终结点。

    使用 ``from_action_info("<base>", "<pkg>", "<Name>")`` 可按 rosbridge
    惯例自动派生；topic 名或消息类型名不规范时也可直接填写每个字段。
    """
    goal_topic: str
    feedback_topic: str
    result_topic: str
    cancel_topic: str
    status_topic: str
    goal_msg_type: str
    feedback_msg_type: str
    result_msg_type: str
    cancel_msg_type: str = "actionlib_msgs/GoalID"
    status_msg_type: str = "actionlib_msgs/GoalStatusArray"

    @classmethod
    def from_action_info(cls, base: str, pkg: str, name: str) -> "ActionSpec":
        """
        按 rosbridge 约定派生 topic 路径和消息类型。

        示例：
            ActionSpec.from_action_info("/robot_task", "navi_types", "RobotAction")
            → goal_topic=/robot_task/goal, goal_msg_type=navi_types/RobotActionActionGoal, ...
        """
        base = base.rstrip("/")
        return cls(
            goal_topic=f"{base}/goal",
            feedback_topic=f"{base}/feedback",
            result_topic=f"{base}/result",
            cancel_topic=f"{base}/cancel",
            status_topic=f"{base}/status",
            goal_msg_type=f"{pkg}/{name}ActionGoal",
            feedback_msg_type=f"{pkg}/{name}ActionFeedback",
            result_msg_type=f"{pkg}/{name}ActionResult",
        )


@dataclass
class ActionFeedback:
    """一次 action feedback。"""
    goal_id: str = ""
    status_code: int = -1                       # actionlib GoalStatus
    inner: Dict = field(default_factory=dict)   # 内层 feedback 字段（业务自定义）
    raw: Dict = field(default_factory=dict)     # 完整 <Name>ActionFeedback 消息
    stamp: Tuple[int, int] = (0, 0)             # (secs, nsecs)


@dataclass
class ActionResult:
    """一次 action 终态返回。"""
    succeeded: bool = False
    status_code: int = -1                       # actionlib GoalStatus (3=SUCCEEDED/4=ABORTED/...)
    error_msg: str = ""
    goal_id: str = ""
    inner: Dict = field(default_factory=dict)   # 内层 result 字段（业务自定义）
    raw: Dict = field(default_factory=dict)     # 完整 <Name>ActionResult 消息

    def __bool__(self) -> bool:
        """令 ``if not result:`` 直接表达"执行失败"。"""
        return self.succeeded


#: 成功判定函数签名：(actionlib_status, inner_result) → (succeeded, error_msg)
SuccessCheckFn = Callable[[int, Dict], Tuple[bool, str]]


# ==================== 内部工具 ====================

# actionlib_msgs/GoalStatus 常量
_ACTIONLIB_SUCCEEDED = 3


def _default_success_check(status_code: int, inner_result: Dict) -> Tuple[bool, str]:
    """默认成功判定：actionlib GoalStatus == 3 (SUCCEEDED)。"""
    if status_code == _ACTIONLIB_SUCCEEDED:
        return True, ""
    return False, f"actionlib status={status_code}"


def _generate_goal_id() -> str:
    """生成唯一 goal_id（格式与 ROS actionlib 客户端一致）。"""
    return f"robot_connect-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _match_goal_id(msg: Dict, goal_id: str) -> bool:
    """检查 actionlib 消息的 status.goal_id.id 是否等于指定 goal_id。"""
    if not msg:
        return False
    status = msg.get("status") or {}
    return (status.get("goal_id") or {}).get("id", "") == goal_id


def _extract_stamp(msg: Dict) -> Tuple[int, int]:
    """从 header.stamp 提取 (secs, nsecs) 作为去重 key。"""
    stamp = (msg.get("header") or {}).get("stamp") or {}
    return int(stamp.get("secs") or 0), int(stamp.get("nsecs") or 0)


def _build_action_goal_msg(goal_id: str, inner_goal: Dict, frame_id: str = "") -> Dict:
    """构造标准 <Name>ActionGoal 消息：{header, goal_id, goal}。"""
    now = time.time()
    secs = int(now)
    nsecs = int((now - secs) * 1e9)
    return {
        "header": {"seq": 0, "stamp": {"secs": secs, "nsecs": nsecs}, "frame_id": frame_id},
        "goal_id": {"stamp": {"secs": secs, "nsecs": nsecs}, "id": goal_id},
        "goal": inner_goal,
    }


# ==================== 核心 API ====================

def send_action(
    robot: "RobotController",
    spec: ActionSpec,
    goal: Dict,
    *,
    goal_id: Optional[str] = None,
    timeout: float = 300.0,
    feedback_callback: Optional[Callable[[ActionFeedback], None]] = None,
    succeeded_check: Optional[SuccessCheckFn] = None,
    retry_on_disconnect: bool = True,
    poll_interval: float = 0.2,
    frame_id: str = "",
    subscribe_settle: float = 0.3,
) -> ActionResult:
    """
    通用 ROS actionlib topic 客户端：发送一个 Goal 并阻塞等待 Result。

    处理的事情：
        1. 连接检查 / 断线自动重连 + 重新订阅
        2. 生成唯一 goal_id，通过它过滤 feedback/result，避免多 Action 串扰
        3. 先订阅再发布（清空老缓存 + 等 subscribe_settle 秒让订阅生效）
        4. 轮询 feedback，按 header.stamp 去重，调用业务回调
        5. 轮询 result，命中即返回
        6. 超时自动发 cancel 并返回失败

    参数:
        robot             : RobotController 实例
        spec              : ActionSpec
        goal              : 内层 Goal 字段（Action .msg 的 Goal 段内容）
        goal_id           : 自定义 goal_id（不传则自动生成）
        timeout           : 总超时秒数；超时会发 cancel 并返回 succeeded=False
        feedback_callback : 收到 feedback 时回调，签名 ``(ActionFeedback) -> None``
        succeeded_check   : 成功判定函数；不传时默认 actionlib status==3 视为成功
        retry_on_disconnect: 中途断连时是否重连 + 重新订阅 + 继续等结果
        poll_interval     : 轮询 topic_messages 的间隔（秒）
        frame_id          : Goal.header.frame_id
        subscribe_settle  : 订阅后等待 rosbridge 注册完成的秒数

    返回:
        ActionResult
            - succeeded        业务是否成功（默认看 actionlib status==3）
            - status_code      actionlib GoalStatus
            - error_msg        失败原因
            - goal_id          本次 goal_id
            - inner            内层 result 字段（业务载荷）
            - raw              完整 <Name>ActionResult 消息

    典型用法：

        spec = ActionSpec.from_action_info("/robot_task", "navi_types", "RobotAction")

        def on_fb(fb):
            print(fb.inner.get("status"), fb.inner.get("current_params"))

        def chem_success(status_code, inner):
            return bool(inner.get("success", False)), str(inner.get("error_msg", "") or "")

        res = send_action(
            robot, spec,
            goal={"robot_task_types": {"type": 0}, "task": "SCAN_TABLE",
                  "area": "", "extra_params": "{}"},
            feedback_callback=on_fb,
            succeeded_check=chem_success,
            timeout=120,
        )
        if not res:
            print("失败:", res.error_msg)
    """
    if succeeded_check is None:
        succeeded_check = _default_success_check

    # 1. 连接检查
    if not robot.is_connected():
        if retry_on_disconnect:
            logger.warning("action_utils", "机器人未连接，尝试重连...")
            print("⚠ 机器人未连接，尝试重连...")
            if not robot.connect():
                logger.error("action_utils", "重连失败")
                return ActionResult(succeeded=False, error_msg="连接失败")
        else:
            return ActionResult(succeeded=False, error_msg="未连接")

    gid = goal_id or _generate_goal_id()

    logger.info("action_utils", f"发送 Action: {spec.goal_topic} goal_id={gid}")
    print(f"🎬 发送 Action → {spec.goal_topic} (goal_id={gid})")

    # 2. 先订阅 feedback + result（防止消息先到）
    robot.subscribe_topic(
        topic_name=spec.feedback_topic,
        msg_type=spec.feedback_msg_type,
        throttle_rate=0,
        queue_length=10,
    )
    robot.subscribe_topic(
        topic_name=spec.result_topic,
        msg_type=spec.result_msg_type,
        throttle_rate=0,
        queue_length=10,
    )
    # 清掉老缓存，避免读到上一次调用的残留
    robot.topic_messages[spec.feedback_topic] = None
    robot.topic_messages[spec.result_topic] = None

    if subscribe_settle > 0:
        time.sleep(subscribe_settle)

    # 3. 发布 Goal
    goal_msg = _build_action_goal_msg(gid, goal, frame_id=frame_id)
    publish_ok = robot.publish_topic(
        topic_name=spec.goal_topic,
        msg_type=spec.goal_msg_type,
        msg_data=goal_msg,
    )
    if not publish_ok:
        logger.error("action_utils", f"发布 Goal 失败: {spec.goal_topic}")
        return ActionResult(succeeded=False, error_msg="发布 Goal 失败", goal_id=gid)
    print(f"📤 已发布 Goal → {spec.goal_topic}")

    # 4. 轮询 feedback / result
    start = time.time()
    last_feedback_stamp: Optional[Tuple[int, int]] = None

    while True:
        # 4a. 超时 → cancel + 返回失败
        if time.time() - start > timeout:
            logger.warning("action_utils", f"Action 超时 {timeout}s，发送 cancel: goal_id={gid}")
            print(f"⏱ Action 超时 ({timeout}s)，发送 cancel")
            cancel_action(robot, spec, goal_id=gid)
            return ActionResult(
                succeeded=False,
                error_msg=f"timeout after {timeout}s",
                goal_id=gid,
            )

        # 4b. 断线恢复
        if not robot.is_connected():
            if not retry_on_disconnect:
                return ActionResult(succeeded=False, error_msg="连接中断", goal_id=gid)
            logger.warning("action_utils", "Action 运行中连接断开，尝试重连并重新订阅...")
            print("⚠ Action 运行中连接断开，尝试重连...")
            if not robot.connect():
                return ActionResult(succeeded=False, error_msg="重连失败", goal_id=gid)
            robot.subscribe_topic(
                topic_name=spec.feedback_topic,
                msg_type=spec.feedback_msg_type,
            )
            robot.subscribe_topic(
                topic_name=spec.result_topic,
                msg_type=spec.result_msg_type,
            )

        # 4c. 读取 feedback（按 stamp 去重；按 goal_id 过滤）
        fb_msg = robot.topic_messages.get(spec.feedback_topic)
        if fb_msg and _match_goal_id(fb_msg, gid):
            stamp = _extract_stamp(fb_msg)
            if stamp != last_feedback_stamp:
                last_feedback_stamp = stamp
                fb = ActionFeedback(
                    goal_id=gid,
                    status_code=(fb_msg.get("status") or {}).get("status", -1),
                    inner=fb_msg.get("feedback") or {},
                    raw=fb_msg,
                    stamp=stamp,
                )
                if feedback_callback:
                    try:
                        feedback_callback(fb)
                    except Exception as e:
                        logger.warning("action_utils", f"feedback 回调异常: {e}")

        # 4d. 读取 result
        res_msg = robot.topic_messages.get(spec.result_topic)
        if res_msg and _match_goal_id(res_msg, gid):
            status_code = (res_msg.get("status") or {}).get("status", -1)
            inner = res_msg.get("result") or {}
            ok, err_msg = succeeded_check(status_code, inner)
            result = ActionResult(
                succeeded=ok,
                status_code=status_code,
                error_msg=err_msg,
                goal_id=gid,
                inner=inner,
                raw=res_msg,
            )
            if ok:
                logger.info("action_utils", f"Action 成功: {spec.result_topic} goal_id={gid}")
                print(f"✅ Action 成功 (goal_id={gid})")
            else:
                logger.warning(
                    "action_utils",
                    f"Action 失败: goal_id={gid}, status={status_code}, err={err_msg}",
                )
                print(f"❌ Action 失败: status={status_code}, err={err_msg}")
            return result

        time.sleep(poll_interval)


def cancel_action(
    robot: "RobotController",
    spec: ActionSpec,
    goal_id: str = "",
) -> bool:
    """
    向 ``<base>/cancel`` 发送 ``actionlib_msgs/GoalID``，取消一个或全部 Goal。

    参数:
        robot   : RobotController
        spec    : ActionSpec
        goal_id : 要取消的 goal_id；空字符串 → 取消该 action 的所有 goal
    """
    if not robot.is_connected():
        logger.error("action_utils", "未连接，无法取消 Action")
        return False

    now = time.time()
    secs = int(now)
    nsecs = int((now - secs) * 1e9)
    ok = robot.publish_topic(
        topic_name=spec.cancel_topic,
        msg_type=spec.cancel_msg_type,
        msg_data={"stamp": {"secs": secs, "nsecs": nsecs}, "id": goal_id},
    )
    if ok:
        logger.info("action_utils", f"已发送 cancel: {spec.cancel_topic} id={goal_id or '(all)'}")
        print(f"🛑 已发送 cancel: {spec.cancel_topic} id={goal_id or '(all)'}")
    return ok


__all__ = [
    "ActionSpec",
    "ActionFeedback",
    "ActionResult",
    "SuccessCheckFn",
    "send_action",
    "cancel_action",
]
