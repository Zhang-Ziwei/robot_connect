"""
WRC_FLOW 任务处理器（流程图编排版）

对外的 HTTP 命令接口与 programs/WRC/WRC.py 保持一致（PROCESS_BEGINS / PROCESS_PAUSED /
PROCESS_RESUMED / PROCESS_ENDED / MANUAL_RESET_COMPLETED），方便直接复用现有的
test_commands/*.json 测试用例，只需要把 active_project 切到 "WRC_FLOW" 即可对比两种
实现方式，互不影响、随时可切回。

区别在于内部实现：本文件不再用 Python 顺序代码描述业务流程，而是把流程骨架放在
``flows/<flow_id>.json`` 里，由 ``core.flow_engine.FlowEngine`` 解释执行；本文件
只做"命令入口 ⇄ 引擎生命周期管理"这层薄薄的胶水逻辑：

    PROCESS_BEGINS           -> 加载流程 JSON，起一个线程运行 FlowEngine
    PROCESS_PAUSED / RESUMED -> 控制 FlowEngine 的 pause_event
    PROCESS_ENDED            -> 控制 FlowEngine 的 stop_event（安全终止）
    MANUAL_RESET_COMPLETED   -> 通过 SignalBus 唤醒流程里的 wait_for_command 节点

同样是"独立类，不继承、不修改旧项目任何文件"，只依赖稳定的底层接口，与
programs/WRC/ 完全解耦（互不 import）。
"""

import os
import threading
from typing import Dict, Optional

from infrastructure.constants import ErrorCode, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import get_require_process_begins
from core.task_state_machine import ParallelTaskStateMachine
from core.flow_engine import FlowEngine, FlowResult, NodeRecord, SignalBus
from core.flow_store import load_flow as store_load_flow, require_one_enabled, start_wait_event

from programs.WRC_FLOW.node_handlers import build_handler_registry

logger = get_error_logger()

# Docker 外部挂载的流程配置目录（现场调试用，优先级最高，见 core/FLOW_ENGINE_GUIDE.md 第 7 节）
_EXTERNAL_FLOWS_DIR = "/config/flows"
_LOCAL_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")

# 本项目参与"连续流程"闭环追踪的机器人（与 WRC.py 的约定一致）
_FLOW_ROBOT_IDS = ("robot_a", "robot_b")


class WRCFlowHandler:
    """
    WRC_FLOW 任务处理器（独立类）。

    使用方式（在 cmd_handler.py 中按 active_project 条件注册）：

        wrc_flow = WRCFlowHandler(robots={"robot_a": robot_a, "robot_b": robot_b, "robot_c": robot_c})

        "PROCESS_BEGINS"            -> wrc_flow.handle_process_begins(cmd_data)
        "PROCESS_PAUSED"            -> wrc_flow.handle_process_paused(cmd_data)
        "PROCESS_RESUMED"           -> wrc_flow.handle_process_resumed(cmd_data)
        "PROCESS_ENDED"             -> wrc_flow.handle_process_ended(cmd_data)
        "MANUAL_RESET_COMPLETED"    -> wrc_flow.handle_manual_reset_completed(cmd_data)
    """

    def __init__(self, robots: dict):
        self.robots = robots or {}

        flow_ids = [rid for rid in _FLOW_ROBOT_IDS if rid in self.robots] or list(self.robots.keys())
        self._flow_robot_ids = flow_ids
        self.task_state_machine = ParallelTaskStateMachine(flow_ids)

        # 暂停/结束：语义与 WRC.py 的 _pause_event / _stop_event 完全一致，
        # 直接传给 FlowEngine 即可接管暂停/结束控制，见 core/FLOW_ENGINE_GUIDE.md 第 5 节。
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()

        # 人工复位信号总线：MANUAL_RESET_COMPLETED 命令触发，流程里的
        # wait_for_command 节点（event_name="manual_reset"）据此被唤醒。
        self._signal_bus = SignalBus()

        self._handlers = build_handler_registry(
            self.robots, self.task_state_machine, get_robot=self._get_robot,
            stop_event=self._stop_event,
        )

        self._engine: Optional[FlowEngine] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._process_active = False

        # 上位机关掉「必须 PROCESS_BEGINS」时，连上机器人就开跑；展会默认不会走这里。
        if self.robots and not get_require_process_begins():
            _fid, err = self._launch_enabled_flow({})
            if err:
                logger.warning("WRC_FLOW", f"免 PROCESS_BEGINS 自动启动失败: {err}")

    # ──────────────────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────────────────

    def _get_robot(self, robot_id: str):
        return self.robots.get(robot_id)

    def _load_flow(self, flow_id: str) -> Optional[Dict]:
        """
        按优先级加载流程 JSON：
            1. /config/flows/<flow_id>.json      —— Docker 外部挂载（现场改流程用）
            2. programs/WRC_FLOW/flows/<flow_id>.json —— 项目内置（随代码仓库发布）
        """
        try:
            graph = store_load_flow(flow_id, _LOCAL_FLOWS_DIR)
        except Exception as e:  # noqa: BLE001
            logger.error("WRC_FLOW", f"加载流程文件失败 {flow_id}: {e}")
            return None
        if graph is None:
            logger.error("WRC_FLOW", f"流程 '{flow_id}' 未找到（已尝试 {_EXTERNAL_FLOWS_DIR} 与 {_LOCAL_FLOWS_DIR}）")
            return None
        logger.info("WRC_FLOW", f"加载流程 '{flow_id}'")
        return graph

    def _on_node_event(self, node_id: str, status: str, record: NodeRecord):
        """节点执行事件回调：目前只打日志；GUI 演练模式会用另一个回调实时推送到前端。"""
        if status == "running":
            logger.info("WRC_FLOW", f"▶ 节点 {node_id}({record.type}) [{record.label}] 开始执行")
        else:
            mark = "✓" if status == "success" else "✗"
            logger.info("WRC_FLOW", f"{mark} 节点 {node_id}({record.type}) 结果={status} {record.message}")

    def _run_engine(self, engine: FlowEngine):
        try:
            result: FlowResult = engine.run()
            logger.info("WRC_FLOW", f"流程执行结束: status={result.status} message={result.message}")
            if not result.success and result.status not in ("stopped",):
                self.task_state_machine.set_error(result.message)
        except Exception as e:  # noqa: BLE001
            logger.exception_occurred("WRC_FLOW", "流程执行线程异常", e)
            self.task_state_machine.set_error(f"流程执行线程异常: {e}")
        finally:
            for rid in self._flow_robot_ids:
                self.task_state_machine.mark_robot_done(rid)
            self._process_active = False

    def _launch_enabled_flow(self, params: Optional[Dict] = None) -> tuple:
        """加载当前已激活的图并开跑。返回 (flow_id, error)，成功时 error 为 None。"""
        params = params or {}
        flow_id, graph, err = require_one_enabled(_LOCAL_FLOWS_DIR)
        if err:
            return None, err
        for robot_id in _FLOW_ROBOT_IDS:
            if self._get_robot(robot_id) is None:
                return None, f"机器人 {robot_id} 不存在"
        self._stop_event.clear()
        self._pause_event.set()
        self._process_active = True
        self.task_state_machine.start_task(params.get("cmd_id") or "WRC_FLOW")
        engine = FlowEngine(
            graph,
            handlers=self._handlers,
            pause_event=self._pause_event,
            stop_event=self._stop_event,
            on_event=self._on_node_event,
            signal_bus=self._signal_bus,
        )
        self._engine = engine
        if start_wait_event(graph) == "PROCESS_BEGINS":
            self._signal_bus.fire("PROCESS_BEGINS", data=params)
        self._engine_thread = threading.Thread(
            target=self._run_engine, args=(engine,), daemon=True,
            name="WRC_FLOW-engine",
        )
        self._engine_thread.start()
        logger.info("WRC_FLOW", f"已启动流程 '{flow_id}'")
        return flow_id, None

    # ──────────────────────────────────────────────────────────────────────────
    # 公开命令入口（与 programs/WRC/WRC.py 的流程控制命令接口保持一致）
    # ──────────────────────────────────────────────────────────────────────────

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_BEGINS：流程总开关。展会必须先发这条才开跑（连上机器人不会自动开始）。
        只启动当前已激活的那一份图。若已在跑且正等待 PROCESS_BEGINS，则只唤醒，不开第二份。
        """
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}

        if self.task_state_machine.is_busy() or self._process_active:
            if self._engine and self._engine.is_waiting_for("PROCESS_BEGINS"):
                self._signal_bus.fire("PROCESS_BEGINS", data=params)
                logger.info("WRC_FLOW", "流程已在运行，PROCESS_BEGINS 已唤醒等待节点")
                return make_success_response("已唤醒等待 PROCESS_BEGINS 的流程节点", cmd_id=cmd_id)
            waiting = self._engine.waiting_signals() if self._engine else set()
            if waiting & {"manual_reset", "MANUAL_RESET_COMPLETED"}:
                return make_error_response(
                    ErrorCode.ROBOT_BUSY,
                    "流程已打开，当前在等人工复位。请发送 MANUAL_RESET_COMPLETED，不要再发 PROCESS_BEGINS。若要重开请先 PROCESS_ENDED",
                    cmd_id=cmd_id,
                )
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "流程已在运行中，请先发送 PROCESS_ENDED 结束当前流程",
                cmd_id=cmd_id,
            )

        # 注意：显式对照 _FLOW_ROBOT_IDS（本流程业务需要的机器人），而不是
        # self._flow_robot_ids（初始化时按 self.robots 是否非空算出来的交集）——
        # 否则当 self.robots 完全为空时，交集也是空列表，for 循环不会执行，
        # 这个校验就会被跳过，流程会在没有任何机器人的情况下"假装"启动成功。
        for robot_id in _FLOW_ROBOT_IDS:
            if self._get_robot(robot_id) is None:
                return make_error_response(
                    ErrorCode.ROBOT_NOT_FOUND,
                    f"机器人 {robot_id} 不存在",
                    cmd_id=cmd_id,
                )
        if self._get_robot("robot_c") is None:
            logger.warning(
                "WRC_FLOW",
                "robot_c 未配置：人工复位后图上的拆垛节点会失败，P1 不会置 FULL",
            )

        flow_id, err = self._launch_enabled_flow(dict(params, cmd_id=cmd_id))
        if err:
            self._process_active = False
            code = ErrorCode.ROBOT_NOT_FOUND if "不存在" in err else ErrorCode.CMD_EXECUTION_ERROR
            return make_error_response(code, err, cmd_id=cmd_id)

        return make_success_response(
            f"已接收 PROCESS_BEGINS 命令，已激活流程 '{flow_id}' 开始工作",
            cmd_id=cmd_id,
            flow_id=flow_id,
            note="使用 GET_TASK_STATE 命令查询任务状态",
        )

    def handle_process_paused(self, cmd_data: Dict) -> Dict:
        """PROCESS_PAUSED：流程会在当前节点执行完成后暂停（引擎在下一个节点前阻塞）。"""
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有正在运行的流程", cmd_id=cmd_id)
        self._pause_event.clear()
        logger.info("WRC_FLOW", "收到 PROCESS_PAUSED，流程将在当前节点完成后暂停")
        return make_success_response("流程已暂停", cmd_id=cmd_id)

    def handle_process_resumed(self, cmd_data: Dict) -> Dict:
        """PROCESS_RESUMED：恢复被 PROCESS_PAUSED 暂停的流程。"""
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(ErrorCode.TASK_NOT_FOUND, "当前没有正在运行的流程", cmd_id=cmd_id)
        self._pause_event.set()
        logger.info("WRC_FLOW", "收到 PROCESS_RESUMED，流程继续执行")
        return make_success_response("流程已恢复", cmd_id=cmd_id)

    def handle_process_ended(self, cmd_data: Dict) -> Dict:
        """PROCESS_ENDED：请求安全终止流程（引擎在下一个节点前检测到 stop_event 后退出）。"""
        cmd_id = cmd_data.get("cmd_id")
        self._stop_event.set()
        self._pause_event.set()  # 避免流程正好处于暂停状态而无法响应 stop
        self._process_active = False
        logger.info("WRC_FLOW", "收到 PROCESS_ENDED，流程将安全终止")
        return make_success_response("流程结束请求已接收", cmd_id=cmd_id)

    def handle_manual_reset_completed(self, cmd_data: Dict) -> Dict:
        """
        MANUAL_RESET_COMPLETED：唤醒等待人工复位的 wait_for_command 节点。
        图上 event_name 可以是 ``manual_reset``（旧图）或 ``MANUAL_RESET_COMPLETED``（编辑器下拉）。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                "流程尚未启动。请先发送 PROCESS_BEGINS，等流程图进入「等待人工复位」后再发本命令",
                cmd_id=cmd_id,
            )
        self._signal_bus.fire("MANUAL_RESET_COMPLETED", data={})
        logger.info("WRC_FLOW", "收到 MANUAL_RESET_COMPLETED，已唤醒等待中的流程节点")
        return make_success_response("人工复位信号已接收", cmd_id=cmd_id)

    def handle_signal(self, cmd_data: Dict) -> Dict:
        """
        WRC_FLOW_SIGNAL：通用信号入口。
        params.event_name 指定要唤醒的 wait_for_command 节点；其余 params 原样带进总线。
        """
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params") or {}
        event_name = params.get("event_name") or params.get("event") or "manual_reset"
        payload = {k: v for k, v in params.items() if k not in ("event_name", "event")}
        self._signal_bus.fire(event_name, data=payload)
        logger.info("WRC_FLOW", f"收到 WRC_FLOW_SIGNAL，已唤醒等待 '{event_name}' 的流程节点")
        return make_success_response(f"信号 '{event_name}' 已接收", cmd_id=cmd_id)