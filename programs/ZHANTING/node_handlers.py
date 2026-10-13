"""展厅只组合通用节点，动作顺序和参数由流程图定义。"""

from __future__ import annotations

from core.http_nodes import build_http_handlers
from core.plc_nodes import build_plc_handlers
from core.robot_nodes import build_robot_handlers
from programs.ZHANTING.constants import NavigationPose


def build_handler_registry(robots, task_state_machine, get_robot=None, stop_event=None, plc_client=None):
    return {
        **build_robot_handlers(
            robots, task_state_machine, NavigationPose,
            get_robot=get_robot, stop_event=stop_event,
        ),
        **build_http_handlers(stop_event=stop_event),
        **build_plc_handlers(client=plc_client, stop_event=stop_event),
    }


NODE_TYPE_SCHEMAS = {}
COMMON_NODES = [
    "navigate", "send_operation", "update_step", "send_http",
    "plc_read", "plc_write", "plc_wait", "plc_handshake",
]
SERVICE_OPTIONS = ["robot_task"]
STEP_OPTIONS = ["人工上料", "开盖", "分液", "检测", "关盖", "倒废液", "完成"]
