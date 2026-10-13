"""展厅流程生命周期：单次启动、人工确认、暂停、恢复和可安全重启的停止。"""

from __future__ import annotations

import copy
import os
import threading
from urllib.parse import urlsplit

from core.flow_engine import FlowEngine, SignalBus
from core.flow_store import load_flow as _store_load_flow, require_one_enabled, start_wait_event
from core.task_state_machine import ParallelTaskStateMachine
from infrastructure.config_loader import get_require_process_begins, load_config
from infrastructure.constants import ErrorCode, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger
from plc_controller.common_plc import PlcConfigurationError, client_from_config
from programs.ZHANTING.constants import ARM_TASK_KEYS, LOADED_EVENT
from programs.ZHANTING.node_handlers import build_handler_registry


logger = get_error_logger()
_LOG = "ZHANTING"
_EXTERNAL_FLOWS_DIR = "/config/flows"
_LOCAL_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")
_FLOW_ROBOT_ID = "robot_a"


def load_flow(flow_id):
    return _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)


def _prepare_run():
    """校验真实设备配置，返回只属于本轮的上下文和延迟连接的 PLC 客户端。"""
    config = load_config()
    settings = config.get("zhanting") or {}
    if not isinstance(settings, dict):
        raise ValueError("zhanting 配置必须是对象")
    arm_url = settings.get("arm_url")
    parsed_url = urlsplit(arm_url) if isinstance(arm_url, str) else None
    if not parsed_url or parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("请配置 zhanting.arm_url 为机械臂 HTTP 接口完整地址")
    tasks = settings.get("arm_tasks") or {}
    if not isinstance(tasks, dict):
        raise ValueError("zhanting.arm_tasks 必须是对象")
    missing = [key for key in ARM_TASK_KEYS if not isinstance(tasks.get(key), str) or not tasks[key].strip()]
    if missing:
        raise ValueError("缺少机械臂任务配置 zhanting.arm_tasks: " + ", ".join(missing))
    if settings.get("arm_response_mode") != "completed":
        raise ValueError("zhanting.arm_response_mode 必须为 completed，机械臂响应应表示动作已完成")
    success_field = settings.get("arm_success_field")
    if not isinstance(success_field, str) or not success_field.strip() or "arm_success_value" not in settings:
        raise ValueError("请配置 zhanting.arm_success_field 和 arm_success_value 以判断动作完成")
    plc_config = config.get("plc")
    if not isinstance(plc_config, dict):
        raise PlcConfigurationError("缺少 plc 配置")
    # 工厂纯校验配置，第一次 PLC 节点执行时才建立网络连接。
    plc = client_from_config(plc_config)
    if plc is None:
        raise PlcConfigurationError("展厅流程需要启用 PLC 并配置全部点位映射")
    context = {key: copy.deepcopy(settings[key]) for key in (
        "arm_url", "arm_tasks", "arm_response_mode", "arm_success_field", "arm_success_value",
    )}
    context["robot_id"] = _FLOW_ROBOT_ID
    return context, plc


class ZhantingHandler:
    def __init__(self, robots):
        self.robots = robots or {}
        self.task_state_machine = ParallelTaskStateMachine([_FLOW_ROBOT_ID])
        self._lifecycle_lock = threading.RLock()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._signal_bus = SignalBus()
        self._engine = None
        self._engine_thread = None
        self._process_active = False
        self._closed = False
        self._plc = None
        self._handlers = {}
        if self._get_robot(_FLOW_ROBOT_ID) is not None and not get_require_process_begins():
            error = self._launch({})
            if error:
                logger.error(_LOG, error)

    def _get_robot(self, robot_id):
        return self.robots.get(robot_id)

    def _running(self):
        return self._process_active or bool(self._engine_thread and self._engine_thread.is_alive())

    def _on_node_event(self, node_id, status, record):
        if record.type not in ("noop", "condition", "delay"):
            logger.info(_LOG, f"节点 {node_id}({record.type}) [{record.label}] {status} {record.message}")

    def _run_engine(self, engine, context, plc, pending, active_calls):
        try:
            result = engine.run(
                initial_context=context,
                extra={"stop_event": engine.stop_event, "robots": self.robots, "plc_client": plc},
            )
            logger.info(_LOG, f"流程执行结束: status={result.status} message={result.message}")
            if engine.stop_event.is_set() or result.status == "stopped":
                self.task_state_machine.cancel_task()
            elif any(record.status == "failure" and record.type != "condition" for record in result.trace):
                self.task_state_machine.set_error(result.message or "流程节点失败")
            elif not result.success:
                self.task_state_machine.set_error(result.message or "流程执行失败")
        except Exception as exc:  # noqa: BLE001
            logger.exception_occurred(_LOG, "流程执行异常", exc)
            self.task_state_machine.set_error(f"流程执行异常: {exc}")
        finally:
            # 图中的并行超时也不能让在途设备调用越过本轮生命周期。
            with pending:
                engine.stop_event.set()
                engine.pause_event.set()
                while active_calls[0]:
                    pending.wait(timeout=0.2)
            try:
                plc.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(_LOG, f"关闭 PLC 连接失败: {exc}")
            with self._lifecycle_lock:
                self.task_state_machine.mark_robot_done(_FLOW_ROBOT_ID)
                self._process_active = False
                self._plc = None

    def _launch(self, params=None):
        params = params or {}
        with self._lifecycle_lock:
            if self._closed:
                return "展厅任务处理器已关闭，请重新连接机器人"
            if self._running():
                return "上一轮流程尚未退出，请等待停止完成后重新开始"
            flow_id, graph, error = require_one_enabled(_LOCAL_FLOWS_DIR)
            if error:
                return error
            plc = None
            try:
                context, plc = _prepare_run()
                # 每次运行独立事件，绝不清除旧线程持有的停止标志或复用上料确认。
                stop_event = threading.Event()
                pause_event = threading.Event()
                pause_event.set()
                signal_bus = SignalBus()
                handlers = build_handler_registry(
                    self.robots, self.task_state_machine,
                    get_robot=self._get_robot, stop_event=stop_event, plc_client=plc,
                )
                pending = threading.Condition()
                active_calls = [0]

                def track(handler):
                    def tracked(node, ctx):
                        with pending:
                            if stop_event.is_set():
                                return False
                            active_calls[0] += 1
                        try:
                            return handler(node, ctx)
                        finally:
                            with pending:
                                active_calls[0] -= 1
                                pending.notify_all()
                    return tracked

                handlers = {name: track(handler) for name, handler in handlers.items()}
                engine = FlowEngine(
                    graph, handlers=handlers, pause_event=pause_event, stop_event=stop_event,
                    on_event=self._on_node_event, signal_bus=signal_bus, flow_loader=load_flow,
                )
                errors = engine.validate()
                if errors:
                    raise ValueError("流程图校验失败: " + "; ".join(errors))
                if start_wait_event(graph) == "PROCESS_BEGINS":
                    signal_bus.fire("PROCESS_BEGINS", data=params)
                thread = threading.Thread(
                    target=self._run_engine,
                    args=(engine, context, plc, pending, active_calls),
                    daemon=True, name="ZHANTING-engine",
                )
                self._stop_event, self._pause_event, self._signal_bus = stop_event, pause_event, signal_bus
                self._engine, self._engine_thread = engine, thread
                self._plc, self._handlers, self._process_active = plc, handlers, True
                self.task_state_machine.start_task(params.get("cmd_id") or "ZHANTING")
                thread.start()
            except Exception as exc:  # noqa: BLE001
                if plc is not None:
                    plc.close()
                self._process_active = False
                logger.error(_LOG, f"展厅启动预检失败: {exc}")
                return str(exc)
            logger.info(_LOG, f"已启动展厅流程 '{flow_id}'，执行一轮后结束")
            return None

    def shutdown(self):
        """请求停止并等待在途调用；返回 False 时上层应保留本处理器，禁止重建。"""
        with self._lifecycle_lock:
            self._closed = True
            self._stop_event.set()
            self._pause_event.set()
            self.task_state_machine.cancel_task()
            thread = self._engine_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return not bool(thread and thread.is_alive())

    def handle_process_begins(self, cmd_data):
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}
        with self._lifecycle_lock:
            if self._running():
                if not self._stop_event.is_set() and self._engine and self._engine.is_waiting_for("PROCESS_BEGINS"):
                    self._signal_bus.fire("PROCESS_BEGINS", data=params)
                    return make_success_response("已唤醒等待 PROCESS_BEGINS 的流程节点", cmd_id=cmd_id)
                return make_error_response(ErrorCode.ROBOT_BUSY, "展厅流程仍在运行或停止中，请等待上一轮退出", cmd_id=cmd_id)
            if self._get_robot(_FLOW_ROBOT_ID) is None:
                return make_error_response(ErrorCode.ROBOT_NOT_FOUND, "请先发送 START_WORKING 连接 robot_a", cmd_id=cmd_id)
            error = self._launch(dict(params, cmd_id=cmd_id))
            if error:
                return make_error_response(ErrorCode.CMD_EXECUTION_ERROR, error, cmd_id=cmd_id)
        return make_success_response("展厅流程已启动，请在人工上料后发送 ZHANTING_LOADED", cmd_id=cmd_id)

    def handle_process_paused(self, cmd_data):
        with self._lifecycle_lock:
            if not self._running() or self._stop_event.is_set():
                return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有正在运行的流程", cmd_id=cmd_data.get("cmd_id"))
            self._pause_event.clear()
        return make_success_response("流程将在当前节点完成后暂停", cmd_id=cmd_data.get("cmd_id"))

    def handle_process_resumed(self, cmd_data):
        with self._lifecycle_lock:
            if not self._running() or self._stop_event.is_set():
                return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有可恢复的流程", cmd_id=cmd_data.get("cmd_id"))
            self._pause_event.set()
        return make_success_response("流程已恢复", cmd_id=cmd_data.get("cmd_id"))

    def handle_process_ended(self, cmd_data):
        with self._lifecycle_lock:
            self._stop_event.set()
            self._pause_event.set()
            self.task_state_machine.cancel_task()
        return make_success_response("流程结束请求已接收，等待在途节点退出后可重新开始", cmd_id=cmd_data.get("cmd_id"))

    def handle_loaded(self, cmd_data):
        with self._lifecycle_lock:
            if (
                not self._running() or self._stop_event.is_set() or self._engine is None
                or not self._engine.is_waiting_for(LOADED_EVENT)
                or self._signal_bus.wait(LOADED_EVENT, timeout=0)
            ):
                return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前流程没有等待人工上料确认", cmd_id=cmd_data.get("cmd_id"))
            params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}
            self._signal_bus.fire(LOADED_EVENT, data=params)
        return make_success_response("已确认人工上料，流程继续", cmd_id=cmd_data.get("cmd_id"))

    def build_command_map(self):
        return {
            "PROCESS_BEGINS": self.handle_process_begins,
            "PROCESS_PAUSED": self.handle_process_paused,
            "PROCESS_RESUMED": self.handle_process_resumed,
            "PROCESS_ENDED": self.handle_process_ended,
            LOADED_EVENT: self.handle_loaded,
        }
