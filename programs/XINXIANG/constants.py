"""
XINXIANG 项目专属常量。

导航点位加载优先级（高→低）：
    1. /config/robot_config.json
    2. programs/XINXIANG/robot_config.json
    3. infrastructure/robot_config.json
"""

from __future__ import annotations

import os

from hardware.action_utils import ActionSpec
from infrastructure.constants import PLCCoils, PLCHoldingRegisters, ROSTopic, ROSTopicMessageType
from infrastructure.pose_loader import get_active_project, load_nav_poses


_XINXIANG_POSE_DEFAULTS = {
    "home": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "detect": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
}


class NavigationPose:
    """XINXIANG 导航点位；模块加载时从 robot_config.json 覆盖。"""

    pass


_poses = load_nav_poses(
    project="XINXIANG",
    defaults=_XINXIANG_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "XINXIANG" else None,
    allow_extra_keys=True,
)
for _k, _v in _poses.items():
    setattr(NavigationPose, _k, _v)


class XinxiangNavTolerance:
    DISTANCE = 0.04
    HEADING = 0.04


class XinxiangService:
    ROBOT_TASK = "/robot_task"


XINXIANG_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class XinxiangTimeout:
    ROBOT_ACTION = 1200.0
    NAVIGATION = 180.0
    PLC = 120.0
    PLC_POLL = 0.2


#: 检测模块握手：本机可先写 DETECT_STATE，再置位线圈，等对方把寄存器改成 wait_done。
#: 放料位移：4003 初始 0，本机写成 1，对方再改成 2。不要空等对方把 0 改成 1。
DETECT_STEPS = {
    "dispense": {
        "label": "检测放料位移",
        "coil": PLCCoils.DETECT_DISPENSE,
        "register": PLCHoldingRegisters.DETECT_STATE,
        "set_state": 1,
        "wait_done": 2,
    },
    "start": {
        "label": "检测启动",
        "coil": PLCCoils.DETECT_START,
        "register": PLCHoldingRegisters.DETECT_STATE,
        "wait_done": 4,
    },
    "pick": {
        "label": "检测取料位移",
        "coil": PLCCoils.DETECT_PICK,
        "register": PLCHoldingRegisters.DETECT_STATE,
        "wait_done": 5,
    },
    "finish": {
        "label": "检测取料完成",
        "coil": PLCCoils.DETECT_FINISH,
        "register": PLCHoldingRegisters.DETECT_STATE,
        "wait_done": 1,
    },
}


# 通用 PLC 节点使用零基 Modbus 偏移，继续共享原服务端数据区。
PLC_POINTS = {
    "open_state": {"type": "INT", "address": PLCHoldingRegisters.OPEN_LID_STATE, "access": "readwrite"},
    "close_state": {"type": "INT", "address": PLCHoldingRegisters.CLOSE_LID_STATE, "access": "readwrite"},
    "detect_state": {"type": "INT", "address": PLCHoldingRegisters.DETECT_STATE, "access": "readwrite"},
    "clean_state": {"type": "INT", "address": PLCHoldingRegisters.CLEAN_STATE, "access": "readwrite"},
    "open_start": {"type": "BOOL", "address": PLCCoils.OPEN_START, "access": "readwrite"},
    "open_finish": {"type": "BOOL", "address": PLCCoils.OPEN_FINISH, "access": "readwrite"},
    "close_start": {"type": "BOOL", "address": PLCCoils.CLOSE_START, "access": "readwrite"},
    "close_finish": {"type": "BOOL", "address": PLCCoils.CLOSE_FINISH, "access": "readwrite"},
    "detect_dispense": {"type": "BOOL", "address": PLCCoils.DETECT_DISPENSE, "access": "readwrite"},
    "detect_start": {"type": "BOOL", "address": PLCCoils.DETECT_START, "access": "readwrite"},
    "detect_pick": {"type": "BOOL", "address": PLCCoils.DETECT_PICK, "access": "readwrite"},
    "detect_finish": {"type": "BOOL", "address": PLCCoils.DETECT_FINISH, "access": "readwrite"},
    "clean_start": {"type": "BOOL", "address": PLCCoils.CLEAN_START, "access": "readwrite"},
    "clean_finish": {"type": "BOOL", "address": PLCCoils.CLEAN_FINISH, "access": "readwrite"},
}
