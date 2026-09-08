"""
WRC_FLOW 项目专属常量

与 programs/WRC/constants.py 保持同一套点位/任务名定义（数值取自 WRC 项目现场配置的
拷贝），但作为独立项目单独维护，不 import programs.WRC 里的任何内容，避免两个项目
产生隐性耦合——以后无论哪个项目改点位/改任务名，都不会互相影响。

导航点位加载优先级（高→低），与其它项目一致：
    1. /config/robot_config.json                  — Docker 外部挂载配置
    2. programs/WRC_FLOW/robot_config.json         — 项目内置配置（本文件所在目录）
    3. infrastructure/robot_config.json           — 基础兜底配置
"""

import os
from enum import Enum

from infrastructure.pose_loader import load_nav_poses, get_active_project
from infrastructure.constants import ROSTopic, ROSTopicMessageType
from hardware.action_utils import ActionSpec


# ──────────────────────────────────────────────────────────────────────────────
# 箱子/槽位状态（demo 流程里用一个简化的字符串变量表示，不接完整 SlotTracker）
# ──────────────────────────────────────────────────────────────────────────────

class BoxSlotState(str, Enum):
    EMPTY  = "空箱子"
    HALF   = "放了一半"
    FULL   = "放满了"
    NO_BOX = "没箱子"


# ──────────────────────────────────────────────────────────────────────────────
# 导航目标点位
# ──────────────────────────────────────────────────────────────────────────────

class WRCFlowPose:
    """
    ROS service/action 的 area 字段值。

    导航走 robot_config 的点位 key（P1 / P3_1 / P4），发给机器人的 area 是 point_*。
    与 programs/WRC/constants.py 的 WRCPose 一致。
    """
    P0   = "point_0"
    P1   = "point_1"
    P2   = "point_2"
    P3_1 = "point_3_1"
    P3_2 = "point_3_2"
    P4   = "point_4"


_WRC_FLOW_POSE_DEFAULTS = {
    "home":  [(0.95, -0.25, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P1":    [(-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P2":    [(-1.02, -0.18, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P3_1":  [(0.88, 0.30, 0.0, 0.0, 0.0, 0.05, 1.00)],
    # P3 区域第二个槽位（对照 programs/WRC/constants.py 的 P3_2），
    # 用于 wrc_flow_main.json 里 find_slot 节点的动态选槽演示。
    "P3_2":  [(0.97, -0.64, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P4":    [(0.59, 2.01, 0.0, 0.0, 0.0, 0.73, 0.68)],
}


class NavigationPose:
    """WRC_FLOW 导航点位；模块加载时从 robot_config.json 覆盖。"""
    pass


# allow_extra_keys=True：本项目支持在图形编辑器里增删点位，配置文件里比上面 defaults
# 多出来的点位是正常操作而不是拼写错误，必须一并加载，否则新加的点位存进了配置却
# 永远不生效，navigate 时只会报"未知点位"。
_wrc_flow_poses = load_nav_poses(
    project="WRC_FLOW",
    defaults=_WRC_FLOW_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "WRC_FLOW" else None,
    allow_extra_keys=True,
)
for _pose_key, _pose_val in _wrc_flow_poses.items():
    setattr(NavigationPose, _pose_key, _pose_val)



# ──────────────────────────────────────────────────────────────────────────────
# 导航精度
# ──────────────────────────────────────────────────────────────────────────────

class WRCFlowNavTolerance:
    DISTANCE = 0.04
    HEADING = 0.04

# ──────────────────────────────────────────────────────────────────────────────
# ROS Service / Action
# ──────────────────────────────────────────────────────────────────────────────

class WRCFlowService:
    ROBOT_TASK = "/robot_task"
    # 搬箱子（pick_up_box / put_down_box）走这个独立 service，与零件抓放
    # （ROBOT_TASK）区分，对照 programs/WRC/constants.py 的 WRCService.ROBOT_TASK_GEELY
    ROBOT_TASK_GEELY = "/robot_task_geely"


WRC_FLOW_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class WRCFlowTask:
    """各步骤对应的 task 字段值（与 programs/WRC/constants.py 的 WRCTask 一致）"""
    # 搬零件（Service，ROBOT_TASK）
    PICK_UP_COMPONENT_A  = "pick_up_component_A"
    PICK_UP_COMPONENT_B  = "pick_up_component_B"
    PUT_DOWN_COMPONENT_A = "put_down_component_A"
    PUT_DOWN_COMPONENT_B = "put_down_component_B"

    # 搬箱子（Service，ROBOT_TASK_GEELY）
    PICK_UP_BOX  = "pick_up_box"
    PUT_DOWN_BOX = "put_down_box"

    # 拆垛 / 向 P1、P2 各放一箱（Service，ROBOT_TASK，robot_c；不传 area）
    PICK_BOX_TO_SP = "pick_box_to_sp"

    # 装配（Service，ROBOT_TASK，robot_b）
    ASSEMBLY             = "assembly"
    CONTINUE_ASSEMBLY    = "continue_assembly"


class WRCFlowTimeout:
    ROBOT_ACTION = 1200
    NAVIGATION   = 180
