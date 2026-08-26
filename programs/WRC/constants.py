"""
WRC（演示装配）任务专属常量

导航点位加载优先级（高→低）：
    1. /config/robot_config.json            — Docker 外部挂载配置
    2. programs/WRC/robot_config.json       — 项目内置配置（本文件所在目录）
    3. infrastructure/robot_config.json    — 基础兜底配置
"""

import os
from enum import Enum
from infrastructure.pose_loader import load_nav_poses, get_active_project
from infrastructure.constants import ROSTopic, ROSTopicMessageType
from hardware.action_utils import ActionSpec


# ──────────────────────────────────────────────────────────────────────────────
# 槽位状态枚举
# ──────────────────────────────────────────────────────────────────────────────

class BoxSlotState(str, Enum):
    """
    箱子槽位的物理状态。

    EMPTY  = 有空箱，可放零件
    HALF   = 已放部分零件
    FULL   = 已满，等待搬走 / 装配
    NO_BOX = 无箱子
    """
    EMPTY  = "空箱子"
    HALF   = "放了一半"
    FULL   = "放满了"
    NO_BOX = "没箱子"


# ──────────────────────────────────────────────────────────────────────────────
# 导航目标点位
# ──────────────────────────────────────────────────────────────────────────────

class WRCPose:
    """WRC 任务各导航点位名称（与 ROS area 字段一致）"""
    P0   = "point_0"
    P1   = "point_1"      # 零件A取件 / 料箱放置点
    P2   = "point_2"      # 零件B取件 / 料箱放置点
    P3_1 = "point_3_1"    # P3 槽位 1
    P3_2 = "point_3_2"    # P3 槽位 2
    P4   = "point_4"      # 装配交接 / 满箱放置点


_WRC_POSE_DEFAULTS = {
    "home":     [(0.95, -0.25, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P1":       [(-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P1_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P2":       [(-1.02, -0.18, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P2_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (-1.02, -0.18, 0.0, 0.0, 0.0, 1.00, -0.05)],
    "P3_1":     [(0.88, 0.30, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P3_1_mid": [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (0.88, 0.30, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P3_2":     [(0.97, -0.64, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P3_2_mid": [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (0.97, -0.64, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P4":       [(0.59, 2.01, 0.0, 0.0, 0.0, 0.73, 0.68)],
    "P4_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73)],
}


class NavigationPose:
    """WRC 导航点位；模块加载时从 robot_config.json 覆盖。"""
    pass


_wrc_poses = load_nav_poses(
    project="WRC",
    defaults=_WRC_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "WRC" else None,
)
for _pose_key, _pose_val in _wrc_poses.items():
    setattr(NavigationPose, _pose_key, _pose_val)


# ──────────────────────────────────────────────────────────────────────────────
# 导航精度
# ──────────────────────────────────────────────────────────────────────────────

class WRCNavTolerance:
    DISTANCE = 0.04
    HEADING  = 0.04
    TRANSLATION_HEADING = 0.0


class WRCAtPoseTolerance:
    """
    “是否已到点”用的 odom 容差（独立于导航 goal 的容差）。

    - is_robot_at_pose：用来决定是否需要跳过导航/是否已经停在取放点附近
    - 与 build_navigation_goal 里的 distance_tolerance/heading_tolerance 无关
    """
    DISTANCE = 0.12
    HEADING = 0.12
    # 获取 odom 的等待时间（秒），避免偶发取不到 odom 导致 at_pose 一直 False
    TIMEOUT = 6.0


# ──────────────────────────────────────────────────────────────────────────────
# ROS Service / Action / Task
# ──────────────────────────────────────────────────────────────────────────────

class WRCService:
    """WRC 使用的 ROS Service 名"""
    ROBOT_TASK = "/robot_task"
    ROBOT_TASK_GEELY = "/robot_task_geely"


# 零件抓放走 Action（/robot_task/*）
WRC_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class WRCTask:
    """WRC 各步骤对应的 task 字段值"""
    # 搬零件（Action）
    PICK_UP_COMPONENT_A  = "pick_up_component_A"
    PICK_UP_COMPONENT_B  = "pick_up_component_B"
    PUT_DOWN_COMPONENT_A = "put_down_component_A"
    PUT_DOWN_COMPONENT_B = "put_down_component_B"

    # 搬箱子（Service）
    PICK_UP_BOX  = "pick_up_box"
    PUT_DOWN_BOX = "put_down_box"

    # 拆垛 / 向 P1、P2 各放一箱（Service，robot_c；无 area）
    PICK_BOX_TO_SP = "pick_box_to_sp"

    # 装配（Service，WA1 / robot_b）
    ASSEMBLY          = "assembly"
    CONTINUE_ASSEMBLY = "continue_assembly"


# ──────────────────────────────────────────────────────────────────────────────
# 任务步骤枚举
# ──────────────────────────────────────────────────────────────────────────────

class WRCStep(Enum):
    IDLE                           = "空闲"

    WAITING_MANUAL_RESET           = "等待人工复位完成信号"
    PLACING_BOX_AT_P1              = "向P1放置料箱"
    PLACING_BOX_AT_P2              = "向P2放置料箱"
    PLACING_BOX_AT_P1_P2           = "向P1与P2放置料箱"  # robot_c 拆垛
    NAVIGATING_TO_P1               = "导航到P1取件点"
    PICK_UP_COMPONENT_A_AT_P1      = "抓取零件A在P1点位"
    NAVIGATING_TO_P2               = "导航到P2取件点"
    PICK_UP_COMPONENT_B_AT_P2      = "抓取零件B在P2点位"
    NAVIGATING_TO_HOME             = "导航到home点位"

    NAVIGATING_TO_P3               = "导航到P3槽位"
    PUT_DOWN_COMPONENT_A_AT_P3     = "放下零件A到P3槽位"
    PUT_DOWN_COMPONENT_B_AT_P3     = "放下零件B到P3槽位"
    PUT_DOWN_COMPONENT_A_AND_B_AT_P3 = "放下零件A和B到P3槽位"

    NAVIGATING_TO_P4               = "导航到P4放置点"
    ACTION_PUT_BOX_AT_P3           = "把空箱子从P4搬到P3空槽"
    ACTION_PUT_BOX_AT_P4           = "把满箱子从P3搬到P4"
    WAITING_NEXT_STEP              = "等待NEXT_STEP信号"

    ASSEMBLY                       = "装配中"
    COMPLETED                      = "任务完成"
    ERROR                          = "任务异常"


# ──────────────────────────────────────────────────────────────────────────────
# 超时配置（秒）
# ──────────────────────────────────────────────────────────────────────────────

class WRCTimeout:
    ROBOT_ACTION = 1200
    NAVIGATION   = 180
    HUMAN_WAIT   = 600
    NEXT_STEP    = 600
    P1_WAIT      = 1800
