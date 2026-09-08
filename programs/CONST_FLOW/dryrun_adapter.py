"""CONST_FLOW 演练适配器。MQTT 连配置里的 Broker（开发默认 127.0.0.1:8870），机器人走 mock rosbridge。"""

import socket
import threading
from typing import Dict, Tuple

from core.task_state_machine import ParallelTaskStateMachine
from hardware.robot_controller import RobotController
from hardware.navigation_utils import get_robot_battery
from infrastructure.error_logger import get_error_logger
from programs.CONST_FLOW.node_handlers import build_handler_registry
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter
from programs.CONST_FLOW.CONST_FLOW import load_flow

logger = get_error_logger()
_LOG = "CONST_FLOW"

MOCK_ROBOTS: Dict[str, Tuple[str, int]] = {
    "robot_a": ("127.0.0.1", 9090),
}
MOCK_HINT = "cd mock_rosbridge && python mock_rosbridge_server.py"
AUTO_FIRE_SIGNALS: Dict[str, float] = {}
DEFAULT_DRYRUN_SIGNALS: Dict[str, Dict] = {
    "CONST_HUMAN_HANDLED": {},
}
DRYRUN_CONTEXT = {"robot_id": "robot_a"}

_dryrun_mqtt = None


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
    global _dryrun_mqtt
    if _dryrun_mqtt is not None:
        try:
            _dryrun_mqtt.disconnect()
        except Exception:
            pass
        _dryrun_mqtt = None
    for robot in robots.values():
        try:
            robot.stop_reconnect()
            robot.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(_LOG, f"演练机器人断开失败: {e}")


def build_dryrun_engine_inputs(signal_bus, stop_event: threading.Event, signals=None):
    global _dryrun_mqtt
    robots = connect_dryrun_robots()
    task_state_machine = ParallelTaskStateMachine(list(robots.keys()) or ["robot_a"])

    def _battery():
        robot = robots.get("robot_a")
        if robot is None:
            return 100
        info = get_robot_battery(robot, timeout=0.2)
        if not info:
            return 100
        pct = float(info.get("percentage") or 1.0)
        if pct <= 1.0:
            pct *= 100.0
        return int(pct)

    mqtt = ConSTMqttAdapter(get_battery=_battery)
    if not mqtt.connect():
        logger.warning(_LOG, "演练未连上 MQTT Broker（请确认上位机/MQServer 在 127.0.0.1:8870）")
    else:
        mqtt.start_heartbeat()
    _dryrun_mqtt = mqtt
    handlers = build_handler_registry(
        robots, task_state_machine, mqtt=mqtt,
        get_robot=robots.get, stop_event=stop_event,
    )

    def _auto_fire_loop():
        stop_event.wait()

    auto_fire_thread = threading.Thread(
        target=_auto_fire_loop, daemon=True, name="CONST_FLOW-dryrun-autofire",
    )
    return handlers, robots, task_state_machine, auto_fire_thread
