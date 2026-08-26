"""
KAIAO 项目专属常量
"""

import os
from infrastructure.pose_loader import load_nav_poses, get_active_project
from infrastructure.constants import ROSTopic, ROSTopicMessageType
from hardware.action_utils import ActionSpec


class KAIAOService:
    ROBOT_TASK = "/robot_task"


# ── KAIAO 机器人任务 Action Spec ─────────────────────────────────────────────
# topic 与消息类型统一见 infrastructure.constants（ROSTopic / ROSTopicMessageType）
KAIAO_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class KAIAOArea:
    """ROS 任务 area 参数：区分 AGV 小车与播种墙/货架。"""
    AGV_CAR = "agv_car"
    AGV_CAR0_0 = "agv_car0_0"
    SHELF = "shelf"
    SHELF0_0 = "shelf0_0"
    SHELF0_1 = "shelf0_1"
    SHELF0_2 = "shelf0_2"
    SHELF1_0 = "shelf1_0"
    SHELF1_1 = "shelf1_1"
    SHELF1_2 = "shelf1_2"
    COMPONENT_CAR = "component_car"


class KAIAOTask:
    PICK_UP_BOX        = "pick_up_box"
    PUT_DOWN_BOX       = "put_down_box"
    PICK_UP_COMPONENT  = "pick_up_component"
    PUT_DOWN_COMPONENT = "put_down_component"
    MODIFY_Z           = "modify_z"     # 调整机械臂至目标层高度


class KAIAOStep:
    IDLE               = "IDLE"
    NAVIGATING         = "NAVIGATING"
    PICKING_UP         = "PICKING_UP"
    PICKING_COMPONENT  = "PICKING_COMPONENT"
    NAVIGATING_TARGET  = "NAVIGATING_TARGET"
    PUTTING_DOWN       = "PUTTING_DOWN"
    PUTTING_COMPONENT  = "PUTTING_COMPONENT"
    DONE               = "DONE"


class KAIAOTimeout:
    ROBOT_ACTION = 800
    NAVIGATION   = 180


class KAIAONavTolerance:
    DISTANCE            = 0.08
    HEADING             = 0.08
    TRANSLATION_HEADING = 0.08


# 取箱前默认导航点位（货架侧）
DEFAULT_SHELF_NAV_AREA = "shelf"

# 零件类型（ROS extra_params.type）
COMPONENT_TYPES = (
    "black_screw",
    "black_tube",
    "black_square",
    "black_joystick",
    "black_fan",
)

_KAIAO_POSE_DEFAULTS: dict = {
    "home":    [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "agv_car0_0": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_0":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_1":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_2":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_3":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_0":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_1":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_2":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_3":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "component_car": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point1":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point2":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point3":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point4":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point5":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
}


class NavigationPose:
    pass


_project_config_dir = (
    os.path.dirname(__file__)
    if get_active_project() not in ("ALL",)
    else None
)

_poses = load_nav_poses("KAIAO", _KAIAO_POSE_DEFAULTS, project_config_dir=_project_config_dir)
for _k, _v in _poses.items():
    setattr(NavigationPose, _k, _v)


def get_nav_pose(area_name: str):
    return getattr(NavigationPose, area_name, None)
