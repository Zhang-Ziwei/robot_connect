"""
CONST_FLOW 任务处理器。

PROCESS_BEGINS / PAUSED / RESUMED / ENDED 控制常驻流程图。
默认必须先 PROCESS_BEGINS 才开跑；上位机项目在 robot_config.flow_control
里把 require_process_begins 设为 false 后，START_WORKING 连上机器人会自动开跑。
RESET_SYSTEM 会停流程并断开 MQTT。
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from infrastructure.constants import ErrorCode, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import get_require_process_begins
from core.task_state_machine import ParallelTaskStateMachine
from core.flow_engine import FlowEngine, FlowResult, NodeRecord, SignalBus
from hardware.navigation_utils import cancel_navigation_action, get_robot_battery

from core.flow_store import load_flow as _store_load_flow, require_one_enabled, start_wait_event
from programs.CONST_FLOW.node_handlers import build_handler_registry
from programs.CONST_FLOW.mqtt_adapter import ConSTMqttAdapter

logger = get_error_logger()
_LOG = "CONST_FLOW"

_EXTERNAL_FLOWS_DIR = "/config/flows"
_LOCAL_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")
_FLOW_ROBOT_ID = "robot_a"


def load_flow(flow_id: str) -> Optional[Dict]:
    graph = _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)
    if graph is not None:
        logger.info(_LOG, f"加载流程 '{flow_id}'")
        return graph
    logger.error(_LOG, f"流程 '{flow_id}' 未找到")
    return None


class CONSTFlowHandler:
    def __init__(self, robots: dict):
        self.robots = robots or {}
        self.task_state_machine = ParallelTaskStateMachine([_FLOW_ROBOT_ID])
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._signal_bus = SignalBus()
        self._engine: Optional[FlowEngine] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._process_active = False
        self._shutdown = threading.Event()

        self.mqtt = ConSTMqttAdapter(get_battery=self._read_battery)
        self._handlers = build_handler_registry(
            self.robots, self.task_state_machine,
            mqtt=self.mqtt, get_robot=self._get_robot,
            stop_event=self._stop_event,
        )
        if self._get_robot(_FLOW_ROBOT_ID) is not None and not get_require_process_begins():
            self._start_auto_loop()

    def _get_robot(self, robot_id: str):
        return self.robots.get(robot_id)

    def _read_battery(self) -> int:
        robot = self._get_robot(_FLOW_ROBOT_ID)
        if robot is None:
            return 100
        info = get_robot_battery(robot, timeout=0.5)
        if not info:
            return 100
        pct = info.get("percentage")
        try:
            value = float(pct)
        except (TypeError, ValueError):
            return 100
        if value <= 1.0:
            value *= 100.0
        return max(0, min(100, int(round(value))))

    def _on_node_event(self, node_id: str, status: str, record: NodeRecord):
        if status == "running":
            logger.info(_LOG, f"▶ 节点 {node_id}({record.type}) [{record.label}] 开始执行")
        else:
            mark = "✓" if status == "success" else "✗"
            logger.info(_LOG, f"{mark} 节点 {node_id}({record.type}) 结果={status} {record.message}")

    def _start_auto_loop(self):
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._shutdown.clear()
        self._loop_thread = threading.Thread(
            target=self._auto_loop, daemon=True, name="CONST_FLOW-auto",
        )
        self._loop_thread.start()
        logger.info(_LOG, "已启动自动流程线程")

    def _auto_loop(self):
        robot = self._get_robot(_FLOW_ROBOT_ID)
        if robot is not None and hasattr(robot, "wait_for_navigation_map_ready"):
            try:
                robot.wait_for_navigation_map_ready(timeout=120.0)
            except Exception as e:  # noqa: BLE001
                logger.warning(_LOG, f"等待导航地图: {e}")
        while not self._shutdown.is_set() and not self._stop_event.is_set():
            if self._get_robot(_FLOW_ROBOT_ID) is None:
                self._shutdown.wait(timeout=1.0)
                continue
            self._run_once()
            if self._shutdown.is_set() or self._stop_event.is_set():
                break
            logger.info(_LOG, "主流程结束，2 秒后重新进入")
            self._shutdown.wait(timeout=2.0)
            self._stop_event.clear()
            self._pause_event.set()

    def _run_once(self):
        flow_id, graph, err = require_one_enabled(_LOCAL_FLOWS_DIR)
        if err:
            logger.error(_LOG, err)
            self._shutdown.wait(timeout=10.0)
            return
        logger.info(_LOG, f"自动循环加载已激活流程 '{flow_id}'")
        self._stop_event.clear()
        self._pause_event.set()
        self._process_active = True
        self.task_state_machine.start_task("CONST_AUTO")
        engine = FlowEngine(
            graph,
            handlers=self._handlers,
            pause_event=self._pause_event,
            stop_event=self._stop_event,
            on_event=self._on_node_event,
            signal_bus=self._signal_bus,
            flow_loader=load_flow,
        )
        self._engine = engine
        if start_wait_event(graph) == "PROCESS_BEGINS":
            self._signal_bus.fire("PROCESS_BEGINS", data={})
        try:
            result: FlowResult = engine.run(
                extra={
                    "const_mqtt": self.mqtt,
                    "stop_event": self._stop_event,
                    "robots": self.robots,
                },
            )
            logger.info(_LOG, f"流程执行结束: status={result.status} message={result.message}")
            if not result.success and result.status not in ("stopped",):
                self.task_state_machine.set_error(result.message)
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "流程执行异常", e)
            self.task_state_machine.set_error(f"流程执行异常: {e}")
        finally:
            self.task_state_machine.mark_robot_done(_FLOW_ROBOT_ID)
            self._process_active = False
            self._engine = None

    def shutdown(self):
        self._shutdown.set()
        self._stop_event.set()
        self._pause_event.set()
        self.mqtt.human_handled()
        self.mqtt.disconnect()
        logger.info(_LOG, "CONST_FLOW 已关闭")

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}
        if self._loop_thread and self._loop_thread.is_alive():
            if self._engine and self._engine.is_waiting_for("PROCESS_BEGINS"):
                self._signal_bus.fire("PROCESS_BEGINS", data=params)
                return make_success_response("已唤醒等待 PROCESS_BEGINS 的流程节点", cmd_id=cmd_id)
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "CONST_FLOW 检定流程已在运行（本项目 START_WORKING 后会自动开跑）。"
                "请先发送 PROCESS_ENDED。人工复位不属于本项目，请把 active_project 改为 WRC_FLOW 后 RESET_SYSTEM",
                cmd_id=cmd_id,
            )
        self._shutdown.clear()
        self._stop_event.clear()
        self._pause_event.set()
        self._start_auto_loop()
        return make_success_response("已接收 PROCESS_BEGINS，检定流程开始工作", cmd_id=cmd_id)

    def handle_process_paused(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有正在运行的流程", cmd_id=cmd_id)
        self._pause_event.clear()
        logger.info(_LOG, "收到 PROCESS_PAUSED")
        return make_success_response("流程已暂停", cmd_id=cmd_id)

    def handle_process_resumed(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有正在运行的流程", cmd_id=cmd_id)
        self._pause_event.set()
        logger.info(_LOG, "收到 PROCESS_RESUMED")
        return make_success_response("流程已恢复", cmd_id=cmd_id)

    def handle_process_ended(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        self._stop_event.set()
        self._pause_event.set()
        self._shutdown.set()
        logger.info(_LOG, "收到 PROCESS_ENDED，自动循环将停止")
        return make_success_response("流程结束请求已接收", cmd_id=cmd_id)

    def handle_human_handled(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        self.mqtt.human_handled()
        self._signal_bus.fire("CONST_HUMAN_HANDLED", data=cmd_data.get("params") or {})
        return make_success_response("已清除呼叫人工标志", cmd_id=cmd_id)

    def handle_cancel_navigation(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") or {}
        robot_id = params.get("robot_id") or _FLOW_ROBOT_ID
        robot = self._get_robot(robot_id)
        if robot is None:
            return make_error_response(ErrorCode.ROBOT_NOT_FOUND, f"机器人 {robot_id} 不存在", cmd_id=cmd_id)
        goal_id = params.get("goal_id") or ""
        ok = cancel_navigation_action(robot, goal_id=goal_id)
        if ok:
            return make_success_response(
                "已发送导航取消" + (f"（goal_id={goal_id}）" if goal_id else "（全部在途 goal）"),
                cmd_id=cmd_id,
            )
        return make_error_response(ErrorCode.CMD_EXECUTION_ERROR, "导航取消发送失败", cmd_id=cmd_id)

    def handle_manual_reset_completed(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        return make_error_response(
            ErrorCode.UNKNOWN_CMD_TYPE,
            "当前运行的是 CONST_FLOW，不处理 MANUAL_RESET_COMPLETED。"
            "请把 infrastructure/robot_config.json 的 active_project 改为 WRC_FLOW，"
            "发送 RESET_SYSTEM 再 START_WORKING",
            cmd_id=cmd_id,
        )

    def build_command_map(self) -> Dict:
        return {
            "PROCESS_BEGINS": self.handle_process_begins,
            "PROCESS_PAUSED": self.handle_process_paused,
            "PROCESS_RESUMED": self.handle_process_resumed,
            "PROCESS_ENDED": self.handle_process_ended,
            "CONST_HUMAN_HANDLED": self.handle_human_handled,
            "CANCEL_NAVIGATION": self.handle_cancel_navigation,
            "MANUAL_RESET_COMPLETED": self.handle_manual_reset_completed,
        }
