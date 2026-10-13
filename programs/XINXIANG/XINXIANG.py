"""
XINXIANG 任务处理器。

START_WORKING 只连机器人。PROCESS_BEGINS 启动当前已激活的图，跑完即停。
是否再跑一轮由图上的连线决定：最后一步连回「等待外部命令」才会再等下一次 PROCESS_BEGINS。
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from core.flow_engine import FlowEngine, FlowResult, NodeRecord, SignalBus
from core.flow_store import load_flow as _store_load_flow, require_one_enabled, start_wait_event
from core.task_state_machine import ParallelTaskStateMachine
from infrastructure.config_loader import get_require_process_begins
from infrastructure.constants import ErrorCode, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger

from programs.XINXIANG.node_handlers import build_handler_registry
from programs.XINXIANG.plc_client import XinxiangPlcClient, client_from_config

logger = get_error_logger()
_LOG = "XINXIANG"

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


class XinxiangHandler:
    def __init__(self, robots: dict):
        self.robots = robots or {}
        self.task_state_machine = ParallelTaskStateMachine([_FLOW_ROBOT_ID])
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._signal_bus = SignalBus()
        self._engine: Optional[FlowEngine] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._process_active = False
        self._plc: Optional[XinxiangPlcClient] = client_from_config()
        self._handlers = build_handler_registry(
            self.robots, self.task_state_machine,
            get_robot=self._get_robot, stop_event=self._stop_event,
            plc_client=self._plc,
        )
        # 默认等 PROCESS_BEGINS。仅当配置 require_process_begins=false 时连上就开跑一次。
        if self._get_robot(_FLOW_ROBOT_ID) is not None and not get_require_process_begins():
            self._launch({})

    def _get_robot(self, robot_id: str):
        return self.robots.get(robot_id)

    def _on_node_event(self, node_id: str, status: str, record: NodeRecord):
        if record.type in ("delay", "condition", "noop"):
            return
        if status == "running":
            logger.info(_LOG, f"▶ 节点 {node_id}({record.type}) [{record.label}] 开始执行")
        else:
            mark = "✓" if status == "success" else "✗"
            logger.info(_LOG, f"{mark} 节点 {node_id}({record.type}) 结果={status} {record.message}")

    def _run_engine(self, engine: FlowEngine):
        try:
            result: FlowResult = engine.run(
                extra={
                    "stop_event": self._stop_event,
                    "robots": self.robots,
                    "plc_client": self._plc,
                },
            )
            logger.info(_LOG, f"流程执行结束: status={result.status} message={result.message}")
            failed = [
                f"{rec.node_id}({rec.type})"
                for rec in (result.trace or [])
                if getattr(rec, "status", "") == "failure"
            ]
            if failed or (not result.success and result.status not in ("stopped",)):
                self.task_state_machine.set_error(result.message or "流程节点失败")
                logger.error(_LOG, f"本轮失败节点: {', '.join(failed) or result.status}")
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred(_LOG, "流程执行异常", e)
            self.task_state_machine.set_error(f"流程执行异常: {e}")
        finally:
            self.task_state_machine.mark_robot_done(_FLOW_ROBOT_ID)
            self._process_active = False

    def _launch(self, params: Optional[Dict] = None):
        """加载已激活的图并跑一遍。图走到没有出边就停，不再由 Python 重开。"""
        params = params or {}
        flow_id, graph, err = require_one_enabled(_LOCAL_FLOWS_DIR)
        if err:
            logger.error(_LOG, err)
            return err
        self._stop_event.clear()
        self._pause_event.set()
        self._process_active = True
        self.task_state_machine.start_task(params.get("cmd_id") or "XINXIANG")
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
            self._signal_bus.fire("PROCESS_BEGINS", data=params)
        self._engine_thread = threading.Thread(
            target=self._run_engine, args=(engine,), daemon=True, name="XINXIANG-engine",
        )
        self._engine_thread.start()
        logger.info(_LOG, f"已启动流程 '{flow_id}'（跑完即停，除非图上自己连回等待节点）")
        return None

    def shutdown(self):
        self._stop_event.set()
        self._pause_event.set()
        # 不关 Modbus 服务器。RESET 会新建 Handler，但 Slave 数据区要留着，否则对方写进旧区、新握手读到 0。
        logger.info(_LOG, "XINXIANG 已关闭（PLC 服务器保持）")

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}
        if self._process_active and self._engine_thread and self._engine_thread.is_alive():
            if self._engine and self._engine.is_waiting_for("PROCESS_BEGINS"):
                self._signal_bus.fire("PROCESS_BEGINS", data=params)
                logger.info(_LOG, "流程已在运行，PROCESS_BEGINS 已唤醒等待节点")
                return make_success_response("已唤醒等待 PROCESS_BEGINS 的流程节点", cmd_id=cmd_id)
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "XINXIANG 流程已在运行。请先发送 PROCESS_ENDED，再发 PROCESS_BEGINS。",
                cmd_id=cmd_id,
            )
        if self._get_robot(_FLOW_ROBOT_ID) is None:
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {_FLOW_ROBOT_ID} 不存在。请先发送 START_WORKING 连接机器人。",
                cmd_id=cmd_id,
            )
        err = self._launch(dict(params, cmd_id=cmd_id))
        if err:
            self._process_active = False
            return make_error_response(ErrorCode.CMD_EXECUTION_ERROR, err, cmd_id=cmd_id)
        return make_success_response("已接收 PROCESS_BEGINS，流程开始工作", cmd_id=cmd_id)

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
        logger.info(_LOG, "收到 PROCESS_ENDED")
        return make_success_response("流程结束请求已接收", cmd_id=cmd_id)

    def build_command_map(self) -> Dict:
        return {
            "PROCESS_BEGINS": self.handle_process_begins,
            "PROCESS_PAUSED": self.handle_process_paused,
            "PROCESS_RESUMED": self.handle_process_resumed,
            "PROCESS_ENDED": self.handle_process_ended,
        }
