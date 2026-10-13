"""展厅演练：机器人连接本地 mock，HTTP / PLC 由通用节点模拟。"""

from __future__ import annotations

import threading

from core.flow_store import load_flow as _store_load_flow
from core.task_state_machine import ParallelTaskStateMachine
from programs.XINXIANG.dryrun_adapter import (
    MOCK_HINT,
    MOCK_ROBOTS,
    connect_dryrun_robots,
    disconnect_dryrun_robots,
)
from programs.ZHANTING.constants import LOADED_EVENT
from programs.ZHANTING.node_handlers import build_handler_registry
from programs.ZHANTING.ZHANTING import _EXTERNAL_FLOWS_DIR, _LOCAL_FLOWS_DIR


DEFAULT_DRYRUN_SIGNALS = {"PROCESS_BEGINS": {}, LOADED_EVENT: {}}
DRYRUN_CONTEXT = {"robot_id": "robot_a"}


def build_dryrun_engine_inputs(signal_bus, stop_event: threading.Event, signals=None):
    robots = connect_dryrun_robots()
    task_state_machine = ParallelTaskStateMachine(list(robots) or ["robot_a"])
    # 不构造 PLC 客户端。运行引擎的 extra.dryrun=True 使 HTTP/PLC 节点只模拟结果。
    handlers = build_handler_registry(
        robots, task_state_machine, get_robot=robots.get, stop_event=stop_event,
    )
    auto_fire_thread = threading.Thread(
        target=stop_event.wait, daemon=True, name="ZHANTING-dryrun",
    )
    return handlers, robots, task_state_machine, auto_fire_thread


def load_flow(flow_id):
    return _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)
