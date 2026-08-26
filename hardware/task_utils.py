"""
机器人任务/操作 Action 工具层（与 `navigation_utils` 并列）。

这一层只做一件事：把"任务/操作"类 Action 的业务契约（Goal/Feedback/Result
的字段命名、成功判定规则、return_params 的 JSON 解析等）从通用传输层
(`hardware.action_utils.send_action`) 里剥出来，让：

    - 通用传输层 `action_utils` 永远不知道"什么是一个任务"
    - 业务代码调用 `send_task_action(robot, task="SCAN_TABLE", ...)` 即可

当前涵盖的 Action 契约（与 ``mock_chem_project_action_server.py``、
``/robot_task/*`` 一致）::

    # Goal
    navi_types/RobotTaskTypes robot_task_types    -> {"type": uint8}
    string task
    string area
    string extra_params                           -> 传入时可为 str / dict / list
    ---
    # Result
    bool   success
    string error_msg
    string return_params                          -> 通常是 JSON 字符串
    ---
    # Feedback
    string status
    string current_params

如果以后又有**同契约**的其他 Action（比如换前缀 `/zj_humanoid/chem_project/...`
或别的任务服务）：只需构造一个新的 ``ActionSpec`` 传进 ``send_task_action(..., spec=xxx)``
就能复用全部业务解析逻辑。

如果以后的 Action **不是**这个契约：直接调用通用的
``hardware.action_utils.send_action`` + 自定义 ``succeeded_check``，
和这层无关。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Union, TYPE_CHECKING

from infrastructure.constants import ROSTopic, ROSTopicMessageType
from infrastructure.error_logger import get_error_logger
from hardware.action_utils import (
    ActionSpec,
    ActionFeedback,
    ActionResult,
    send_action,
    cancel_action,
)

if TYPE_CHECKING:
    from hardware.robot_controller import RobotController

logger = get_error_logger()


# ==================== 预置 Spec ====================

#: 机器人任务 Action（/robot_task/*，navi_types/RobotActionAction*）
#: topic 与消息类型集中维护在 ``infrastructure.constants``。
CHEM_PROJECT_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


# ==================== 领域数据结构 ====================

@dataclass
class TaskFeedback:
    """
    任务 Action 的一次反馈（已经从通用 ``ActionFeedback.inner`` 抽出业务字段）。
    """
    status: str = ""                   # 业务状态字符串（如 "step_1_of_3" / "FINISHED" / "FAILED"）
    current_params: str = ""           # 业务附加参数（通常是 JSON 字符串）
    goal_id: str = ""
    raw: Dict = field(default_factory=dict)   # 原始 inner feedback 字段


@dataclass
class TaskResult:
    """
    任务 Action 的终态返回（已从通用 ``ActionResult`` 映射到业务字段）。

    - ``succeeded``    : 业务是否成功（== inner.success）
    - ``error_msg``    : 失败原因（== inner.error_msg，或通用层合成的描述）
    - ``return_params``: 业务返回载荷（通常是 JSON 字符串，可用
                         ``parse_return_params()`` 解析）
    - ``status_code``  : actionlib GoalStatus，仅作参考信息
    - ``raw``          : 完整的 <Name>ActionResult 消息

    对象本身支持 ``if not result: ...`` 语义（__bool__ 返回 succeeded）。
    """
    succeeded: bool = False
    error_msg: str = ""
    return_params: str = ""
    goal_id: str = ""
    status_code: int = -1
    raw: Dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.succeeded

    def parse_return_params(self, default: Any = None) -> Any:
        """
        把 ``return_params`` 解析为 JSON。

        - 若为空串 → 返回 ``default``
        - 若非 JSON → 返回原字符串
        - 否则返回解析出的对象（dict / list / 基本类型）
        """
        if not self.return_params:
            return default
        try:
            return json.loads(self.return_params)
        except (TypeError, ValueError):
            return self.return_params


# ==================== 成功判定 ====================

def _task_success_check(status_code: int, inner: Dict) -> Tuple[bool, str]:
    """
    任务 Action 的业务成功判定：

        Result { bool success, string error_msg, string return_params }

    只看 ``inner.success``；actionlib ``status_code`` 仅在没有 ``error_msg``
    时作为失败原因的兜底说明。
    """
    ok = bool(inner.get("success", False))
    err = str(inner.get("error_msg", "") or "")
    if ok:
        return True, ""
    if not err:
        err = f"(no error_msg, actionlib status={status_code})"
    return False, err


def _to_task_feedback(af: ActionFeedback) -> TaskFeedback:
    """通用 ActionFeedback → 领域 TaskFeedback。"""
    inner = af.inner or {}
    return TaskFeedback(
        status=str(inner.get("status", "") or ""),
        current_params=str(inner.get("current_params", "") or ""),
        goal_id=af.goal_id,
        raw=inner,
    )


def _to_task_result(ar: ActionResult) -> TaskResult:
    """通用 ActionResult → 领域 TaskResult。"""
    inner = ar.inner or {}
    return TaskResult(
        succeeded=ar.succeeded,
        error_msg=ar.error_msg,
        return_params=str(inner.get("return_params", "") or ""),
        goal_id=ar.goal_id,
        status_code=ar.status_code,
        raw=ar.raw,
    )


# ==================== 核心 API ====================

def send_task_action(
    robot: "RobotController",
    task: str,
    area: str = "",
    extra_params: Union[str, Dict, list] = "",
    robot_task_type: int = 0,
    *,
    spec: ActionSpec = CHEM_PROJECT_ACTION_SPEC,
    timeout: float = 600.0,
    feedback_callback: Optional[Callable[[TaskFeedback], None]] = None,
    retry_on_disconnect: bool = True,
    poll_interval: float = 0.2,
) -> TaskResult:
    """
    调用一个"任务/操作"类 Action，一次性完成 Goal 发送 + 等待 Result。

    参数:
        robot           : RobotController
        task            : 任务名（如 ``"SCAN_TABLE"`` / ``"WAITING_SPLIT_AREA_TRANSFER"``）
        area            : 区域标识，缺省空串
        extra_params    : 额外参数。可传:
                          - ``str``  : 直接作为 Goal.extra_params
                          - ``dict`` / ``list``: 自动 ``json.dumps(..., ensure_ascii=False)``
        robot_task_type : ``navi_types/RobotTaskTypes.type``（uint8），默认 0
        spec            : Action 终结点；默认 :data:`CHEM_PROJECT_ACTION_SPEC`；
                          真机若前缀不同，可传入自定义 ActionSpec
        timeout         : 总超时秒数
        feedback_callback: 反馈回调 ``(TaskFeedback) -> None``，已经解析好 status/current_params
        retry_on_disconnect: 中途断连时是否重连 + 重新订阅并继续等结果
        poll_interval   : 轮询间隔

    返回:
        TaskResult（可直接 ``if not result:`` 判断；``result.parse_return_params()``
        可自动 JSON 解码 ``return_params``）

    使用示例::

        def on_fb(fb: TaskFeedback):
            print(fb.status, "→", fb.current_params)

        res = send_task_action(robot, task="SCAN_TABLE", feedback_callback=on_fb)
        if not res:
            logger.error("任务", f"SCAN_TABLE 失败: {res.error_msg}")
            return

        payload = res.parse_return_params() or {}  # dict / None
    """
    if isinstance(extra_params, (dict, list)):
        extra_params_str = json.dumps(extra_params, ensure_ascii=False)
    else:
        extra_params_str = str(extra_params or "")

    goal = {
        "robot_task_types": {"type": int(robot_task_type)},
        "task": str(task or ""),
        "area": str(area or ""),
        "extra_params": extra_params_str,
    }

    wrapped_cb: Optional[Callable[[ActionFeedback], None]] = None
    if feedback_callback is not None:
        def _cb(af: ActionFeedback) -> None:
            try:
                feedback_callback(_to_task_feedback(af))
            except Exception as e:
                logger.warning("task_utils", f"feedback 回调异常: {e}")
        wrapped_cb = _cb

    action_result = send_action(
        robot=robot,
        spec=spec,
        goal=goal,
        timeout=timeout,
        feedback_callback=wrapped_cb,
        succeeded_check=_task_success_check,
        retry_on_disconnect=retry_on_disconnect,
        poll_interval=poll_interval,
    )

    return _to_task_result(action_result)


def cancel_task_action(
    robot: "RobotController",
    *,
    spec: ActionSpec = CHEM_PROJECT_ACTION_SPEC,
    goal_id: str = "",
) -> bool:
    """
    取消任务 Action。默认取消 chem_project 的指定 goal（空 id → 取消全部）。
    """
    return cancel_action(robot, spec, goal_id=goal_id)


# ==================== 向后兼容别名 ====================

# 旧名字保留，避免已经有代码 import 的地方炸掉。
# 新代码应优先使用 send_task_action / TaskResult / TaskFeedback。
send_chem_project_action = send_task_action


__all__ = [
    "CHEM_PROJECT_ACTION_SPEC",
    "TaskFeedback",
    "TaskResult",
    "send_task_action",
    "cancel_task_action",
    "send_chem_project_action",  # 兼容别名
]
