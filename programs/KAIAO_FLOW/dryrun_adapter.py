"""
KAIAO_FLOW 的"演练模式"适配器。

flow_api_server 本身不认识任何项目的业务细节，演练时需要知道：
    - 连哪个 mock 机器人（地址/端口）
    - 演练开始前要准备哪些流程变量
    - 图上 wait_for_command 的演练入参写在节点 ``dryrun_params`` 上，
      点演练时弹窗可改；复杂场景不用改本文件。
"""

import socket
import threading
from typing import Any, Dict, Tuple

from core.task_state_machine import TaskStateMachine
from hardware.robot_controller import RobotController
from infrastructure.error_logger import get_error_logger
from programs.KAIAO.KAIAO import KAIAOHandler
from programs.KAIAO_FLOW.node_handlers import build_handler_registry
from core.flow_store import load_flow as _store_load_flow
from programs.KAIAO_FLOW.KAIAO_FLOW import _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR

logger = get_error_logger()
_LOG = "KAIAO_FLOW"

# mock_rosbridge_server.py 默认端口映射（见 mock_rosbridge/mock_rosbridge_server.py）
MOCK_ROBOTS: Dict[str, Tuple[str, int]] = {
    "robot_a": ("127.0.0.1", 9090),
}

MOCK_HINT = "cd mock_rosbridge && python mock_rosbridge_server.py"

#: 演练用的示例命令参数，对应 test_commands/KAIAO_PICK_BOX_TO_SP_command.json。
#:
#: 注意演练测不到姿态校正：它挂在"去放箱位"那次导航的离架中间点上，而中间点本身
#: 要靠机器人实时里程计（ROBOT_MOTION_STATE 话题）才会生成，mock_rosbridge_server
#: 并不发布该话题——导航会走 no_change 直达分支，校正只在真机上才会真正触发。
DRYRUN_PICK_BOX_PARAMS: Dict[str, Any] = {
    "robot_id": "robot_a",
    "box_initial_area": {
        "shelf_num": [0, 1, 2],
        "shelf_type": "shelf",
    },
    "box_target_area": {
        "shelf_num": [0, 1, 0],
        "shelf_type": "agv_car",
    },
}

DRYRUN_PICK_COMPONENT_PARAMS: Dict[str, Any] = {
    "robot_id": "robot_a",
    "jobs": [
        {
            "box_initial_area": "component_car",
            "box_num": 0,
            "component_type": "black_screw",
            "target": [
                {"box_target_area": [0, 0, 0], "component_number": 1},
            ],
        }
    ],
}

DRYRUN_CONTEXT: Dict[str, Any] = {
    "robot_id": "robot_a",
    "task_id": "dryrun",
    "action_type": "PICK_BOX_TO_SP",
}

#: 每条可等待命令的演练默认入参。编辑器切换「等待的命令」时换成对应这一份。
DEFAULT_DRYRUN_SIGNALS: Dict[str, Dict[str, Any]] = {
    "PICK_BOX_TO_SP": DRYRUN_PICK_BOX_PARAMS,
    "PICK_COMPONENT_TO_SP": DRYRUN_PICK_COMPONENT_PARAMS,
    "PICK_UP_BOX": {
        "robot_id": "robot_a",
        "area": "shelf",
        "robot_area": "shelf",
        "shelf_level": 2,
    },
    "PUT_DOWN_BOX": {
        "robot_id": "robot_a",
        "area": "point1",
        "robot_area": "shelf",
        "shelf_level": 2,
    },
    "NAVIGATION": {
        "robot_id": "robot_a",
        "area": "point1",
    },
}


def _mock_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def connect_dryrun_robots() -> Dict[str, RobotController]:
    """连接一组指向 mock_rosbridge_server 的临时 RobotController。"""
    robots: Dict[str, RobotController] = {}
    for robot_id, (host, port) in MOCK_ROBOTS.items():
        # max_retry_attempts 必须显式给一个小数字：RobotController 默认无限重试，
        # mock 没启动时会让整个演练请求永远卡住。
        robot = RobotController(host=host, port=port, robot_type=robot_id,
                                max_retry_attempts=1, retry_interval=1)
        if not _mock_reachable(host, port):
            logger.error(
                _LOG,
                f"演练 mock 未监听 {host}:{port}（{robot_id}）。请先启动: {MOCK_HINT}",
            )
            robots[robot_id] = robot
            continue
        robot.connect()
        if not robot.is_connected():
            logger.error(_LOG, f"演练机器人 {robot_id} 连 {host}:{port} 失败")
        robots[robot_id] = robot
    return robots


def disconnect_dryrun_robots(robots: Dict[str, RobotController]):
    """
    演练结束后释放临时连接。

    注意断开用的是 close()——RobotController 没有 disconnect() 方法，
    写错名字会被下面的 except 静默吞掉，导致每演练一次就泄漏一条 websocket 连接。
    """
    for robot in robots.values():
        try:
            robot.stop_reconnect()   # 先停自动重连，否则 close() 后会被立刻拉起来
            robot.close()
        except Exception as e:  # noqa: BLE001 —— 清理失败不该影响演练结果返回
            logger.warning(_LOG, f"演练机器人断开失败: {e}")


def build_dryrun_engine_inputs(signal_bus, stop_event: threading.Event, signals=None):
    """
    组装一次演练所需的一切，返回 (handlers, robots, task_state_machine, auto_fire_thread)。

    命令入参由 flow_api_server 按图上 wait_for_command.dryrun_params / 演练弹窗
    注入 SignalBus，本线程只负责等到 stop，不再写死示例命令。
    """
    robots = connect_dryrun_robots()
    task_state_machine = TaskStateMachine()
    kaiao = KAIAOHandler(robots=robots, task_state_machine=task_state_machine)
    handlers = build_handler_registry(kaiao)

    def _auto_fire_loop():
        stop_event.wait()

    auto_fire_thread = threading.Thread(
        target=_auto_fire_loop, daemon=True, name="KAIAO_FLOW-dryrun-autofire",
    )
    return handlers, robots, task_state_machine, auto_fire_thread

def load_flow(flow_id):
    """
    演练时加载流程图。

    flow_api_server 演练建引擎时会取本模块的 load_flow 当 flow_loader；
    缺了它，图里一旦用「调用子流程」节点，演练就会以"未提供 flow_loader"失败，
    而真机跑得好好的——这种只在演练里出现的差异很难查，所以这里补齐。
    """
    return _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)
