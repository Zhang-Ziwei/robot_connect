"""
WRC_FLOW 项目的"演练模式"适配器。

flow_api_server.py 是通用的、不认识任何具体项目的业务细节，演练时需要知道：
    - 连哪个 mock 机器人（地址/端口）
    - 演练开始后要不要自动触发某些"人工信号"（否则 wait_for_command 节点会一直卡住，
      演练模式下没有真人会点 MANUAL_RESET_COMPLETED 按钮）

把这些项目特定信息放在这里，server 端只需要按 project 名字找到对应的适配器模块即可，
新增项目的演练支持时只需要新写一个这样的 adapter 文件，不需要改 flow_api_server.py。
"""

import threading
import time
from typing import Callable, Dict, Tuple

from core.task_state_machine import ParallelTaskStateMachine
from hardware.robot_controller import RobotController
from programs.WRC_FLOW.node_handlers import build_handler_registry

# mock_rosbridge_server.py 默认端口映射（见 mock_rosbridge/mock_rosbridge_server.py DEFAULT_ROBOTS）
MOCK_ROBOTS: Dict[str, Tuple[str, int]] = {
    "robot_a": ("127.0.0.1", 9090),
    "robot_b": ("127.0.0.1", 9091),
}

# 演练期间自动触发的信号：{事件名: 触发间隔秒数}
# manual_reset 对应真实场景里操作人员点击"人工复位完成"按钮；演练时没有真人操作，
# 所以每隔几秒自动触发一次，让 wait_for_command 节点不至于永远卡住，能看到完整的循环。
AUTO_FIRE_SIGNALS: Dict[str, float] = {
    "manual_reset": 3.0,
}


def connect_dryrun_robots() -> Dict[str, RobotController]:
    """
    连接一组指向 mock_rosbridge_server 的临时 RobotController 实例。
    调用方（flow_api_server）负责在演练结束后调用 disconnect() 清理。
    """
    robots: Dict[str, RobotController] = {}
    for robot_id, (host, port) in MOCK_ROBOTS.items():
        # max_retry_attempts 必须显式给一个小数字：RobotController 默认无限重试，
        # 如果 mock 没启动会导致这里永远卡住，拖垮整个演练请求。
        robot = RobotController(host=host, port=port, robot_type=robot_id,
                                 max_retry_attempts=2, retry_interval=1)
        robot.connect()
        robots[robot_id] = robot
    return robots


def disconnect_dryrun_robots(robots: Dict[str, RobotController]):
    for robot in robots.values():
        try:
            robot.disconnect()
        except Exception:
            pass


def build_dryrun_engine_inputs(signal_bus, stop_event: threading.Event):
    """
    组装一次演练所需的一切：handler 注册表 + 临时机器人 + 一个负责自动触发信号、
    在 stop_event 被 set 后自行退出的后台线程。

    返回 (handlers, robots, task_state_machine, auto_fire_thread)
    """
    robots = connect_dryrun_robots()
    task_state_machine = ParallelTaskStateMachine(list(robots.keys()))
    handlers = build_handler_registry(robots, task_state_machine, get_robot=robots.get)

    def _auto_fire_loop():
        while not stop_event.is_set():
            for name, interval in AUTO_FIRE_SIGNALS.items():
                signal_bus.fire(name, data={})
            stop_event.wait(timeout=min(AUTO_FIRE_SIGNALS.values()) if AUTO_FIRE_SIGNALS else 3.0)

    auto_fire_thread = threading.Thread(target=_auto_fire_loop, daemon=True, name="WRC_FLOW-dryrun-autofire")
    return handlers, robots, task_state_machine, auto_fire_thread
