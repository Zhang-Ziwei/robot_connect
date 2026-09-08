"""
CONST_FLOW 项目专属常量。

导航点位加载优先级（高→低）：
    1. /config/robot_config.json
    2. programs/CONST_FLOW/robot_config.json
    3. infrastructure/robot_config.json

工位点位按「类型_sequenceNumber」命名（如 station_1），工位变多时在编辑器里手动加。
"""

import os

from infrastructure.pose_loader import load_nav_poses, get_active_project
from infrastructure.constants import ROSTopic, ROSTopicMessageType
from hardware.action_utils import ActionSpec


class RobotLiveStatus:
    IDLE = "待机"
    CHARGE = "充电"
    MOVING = "行进"
    INSTALL = "装表"
    UNINSTALL = "拆表"
    WAIT_CALSYS = "等待检定系统回复"
    CALL_HUMAN = "呼叫人工"


class StationStatus:
    WAIT_INSTALL = "待装表"
    INSTALLING = "正在装表"
    INSTALLED = "已装表"
    LEAK_TEST = "检漏测试中"
    WAIT_UNINSTALL = "待拆表"
    UNINSTALLING = "正在拆表"


class PoseType:
    HOME = "home"
    INBOUND_SHELF = "inbound_shelf"
    OUTBOUND_SHELF = "outbound_shelf"
    BOX_WAIT = "box_wait"
    STATION = "station"

    @staticmethod
    def station(seq) -> str:
        return f"{PoseType.STATION}_{int(seq)}"


_CONST_POSE_DEFAULTS = {
    "home": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "inbound_shelf": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "outbound_shelf": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "box_wait": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "station_1": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "station_2": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "station_3": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
    "station_4": [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
}


class NavigationPose:
    """CONST_FLOW 导航点位；模块加载时从 robot_config.json 覆盖。"""
    pass


_poses = load_nav_poses(
    project="CONST_FLOW",
    defaults=_CONST_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "CONST_FLOW" else None,
    allow_extra_keys=True,
)
for _k, _v in _poses.items():
    setattr(NavigationPose, _k, _v)


class ConstNavTolerance:
    DISTANCE = 0.04
    HEADING = 0.04


class ConstService:
    ROBOT_TASK = "/robot_task"


CONST_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class ConstTask:
    """ROS service 的 task 字段。现场可在流程图节点上改，这里是默认名。"""
    PICK_UP_BOX = "pick_up_box"
    PUT_DOWN_BOX = "put_down_box"
    INSTALL_GAUGE = "install_gauge"
    UNINSTALL_GAUGE = "uninstall_gauge"


class ConstTimeout:
    MQTT_REPLY = 1.0
    MQTT_DUTINFO = 10.0
    MQTT_DISCOVER = 3.0
    MQTT_RETRIES = 2
    IDENTIFY_WAIT = 60.0
    GANTRY_WAIT = 3.0
    NAVIGATION = 180.0
    ROBOT_ACTION = 1200.0
    IDENTIFY_RETRIES = 2
    CALSYS_CODE1_RETRIES = 2
    BOX_POLL = 5.0
    PLC_POLL = 5.0


class HumanReason:
    GAUGE_DROPPED = "gauge_dropped"
    MQTT_TIMEOUT = "mqtt_timeout"
    CALSYS_ERROR = "calsys_error"
    IDENTIFY_FAILED = "identify_failed"
