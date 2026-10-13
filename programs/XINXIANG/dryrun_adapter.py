"""XINXIANG 演练适配器：机器人走 mock rosbridge，检测模块握手在演练里跳过。"""

import socket
import threading
from typing import Dict, Tuple

from core.flow_store import load_flow as _store_load_flow
from core.task_state_machine import ParallelTaskStateMachine
from hardware.robot_controller import RobotController
from infrastructure.error_logger import get_error_logger
from programs.XINXIANG.node_handlers import build_handler_registry
from programs.XINXIANG.XINXIANG import _EXTERNAL_FLOWS_DIR, _LOCAL_FLOWS_DIR

logger = get_error_logger()
_LOG = "XINXIANG"

MOCK_ROBOTS: Dict[str, Tuple[str, int]] = {
    "robot_a": ("127.0.0.1", 9090),
}
MOCK_HINT = "cd mock_rosbridge && python mock_rosbridge_server.py"
DEFAULT_DRYRUN_SIGNALS: Dict[str, Dict] = {}
DRYRUN_CONTEXT = {"robot_id": "robot_a"}


def _mock_reachable(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def connect_dryrun_robots() -> Dict[str, RobotController]:
    robots: Dict[str, RobotController] = {}
    for robot_id, (host, port) in MOCK_ROBOTS.items():
        robot = RobotController(
            host=host, port=port, robot_type=robot_id,
            max_retry_attempts=1, retry_interval=1,
        )
        if not _mock_reachable(host, port):
            logger.error(_LOG, f"演练 mock 未监听 {host}:{port}。请先启动: {MOCK_HINT}")
            robots[robot_id] = robot
            continue
        robot.connect()
        robots[robot_id] = robot
    return robots


def disconnect_dryrun_robots(robots: Dict[str, RobotController]):
    for robot in robots.values():
        try:
            robot.stop_reconnect()
            robot.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(_LOG, f"演练机器人断开失败: {e}")


def build_dryrun_engine_inputs(signal_bus, stop_event: threading.Event, signals=None):
    robots = connect_dryrun_robots()
    task_state_machine = ParallelTaskStateMachine(list(robots.keys()) or ["robot_a"])
    handlers = build_handler_registry(
        robots, task_state_machine, get_robot=robots.get, stop_event=stop_event,
    )

    def _auto_fire_loop():
        stop_event.wait()

    auto_fire_thread = threading.Thread(
        target=_auto_fire_loop, daemon=True, name="XINXIANG-dryrun-autofire",
    )
    return handlers, robots, task_state_machine, auto_fire_thread


def load_flow(flow_id):
    return _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)
