"""
WRC_FLOW 任务处理器（流程图编排版）

对外的 HTTP 命令接口与 programs/WRC/WRC.py 保持一致（PROCESS_BEGINS / PROCESS_PAUSED /
PROCESS_RESUMED / PROCESS_ENDED / MANUAL_RESET_COMPLETED），方便直接复用现有的
test_commands/*.json 测试用例，只需要把 active_project 切到 "WRC_FLOW" 即可对比两种
实现方式，互不影响、随时可切回。

区别在于内部实现：本文件不再用 Python 顺序代码描述业务流程，而是把流程骨架放在
``flows/wrc_flow_main.json`` 里，由 ``core.flow_engine.FlowEngine`` 解释执行；本文件
只做"命令入口 ⇄ 引擎生命周期管理"这层薄薄的胶水逻辑：

    PROCESS_BEGINS           -> 加载流程 JSON，起一个线程运行 FlowEngine
    PROCESS_PAUSED / RESUMED -> 控制 FlowEngine 的 pause_event
    PROCESS_ENDED            -> 控制 FlowEngine 的 stop_event（安全终止）
    MANUAL_RESET_COMPLETED   -> 通过 SignalBus 唤醒流程里的 wait_for_command 节点

同样是"独立类，不继承、不修改旧项目任何文件"，只依赖稳定的底层接口，与
programs/WRC/ 完全解耦（互不 import）。
"""

import json
import os
import threading
from typing import Dict, Optional

from infrastructure.constants import ErrorCode, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger
from core.task_state_machine import ParallelTaskStateMachine
from core.flow_engine import FlowEngine, FlowResult, NodeRecord, SignalBus

from programs.WRC_FLOW.node_handlers import build_handler_registry

logger = get_error_logger()

# Docker 外部挂载的流程配置目录（现场调试用，优先级最高，见 core/FLOW_ENGINE_GUIDE.md 第 7 节）
_EXTERNAL_FLOWS_DIR = "/config/flows"
_LOCAL_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")

# 本项目参与"连续流程"闭环追踪的机器人（与 WRC.py 的约定一致）
_FLOW_ROBOT_IDS = ("robot_a", "robot_b")

# 主流程 id（对应 flows/wrc_flow_main.json）
_MAIN_FLOW_ID = "wrc_flow_main"


class WRCFlowHandler:
    """
    WRC_FLOW 任务处理器（独立类）。

    使用方式（在 cmd_handler.py 中按 active_project 条件注册）：

        wrc_flow = WRCFlowHandler(robots={"robot_a": robot_a, "robot_b": robot_b})

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
        )

        self._engine: Optional[FlowEngine] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._process_active = False

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
        for base_dir in (_EXTERNAL_FLOWS_DIR, _LOCAL_FLOWS_DIR):
            path = os.path.join(base_dir, f"{flow_id}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        graph = json.load(f)
                    logger.info("WRC_FLOW", f"加载流程 '{flow_id}' <- {path}")
                    return graph
                except Exception as e:
                    logger.error("WRC_FLOW", f"加载流程文件失败 {path}: {e}")
        logger.error("WRC_FLOW", f"流程 '{flow_id}' 未找到（已尝试 {_EXTERNAL_FLOWS_DIR} 与 {_LOCAL_FLOWS_DIR}）")
        return None

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

    # ──────────────────────────────────────────────────────────────────────────
    # 公开命令入口（与 programs/WRC/WRC.py 的流程控制命令接口保持一致）
    # ──────────────────────────────────────────────────────────────────────────

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_BEGINS：加载 wrc_flow_main 流程，起后台线程运行 FlowEngine（连续模式，
        直到收到 PROCESS_ENDED 才会停止）。
        """
        cmd_id = cmd_data.get("cmd_id")

        if self.task_state_machine.is_busy():
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

        graph = self._load_flow(_MAIN_FLOW_ID)
        if graph is None:
            return make_error_response(
                ErrorCode.CMD_EXECUTION_ERROR,
                f"流程定义 '{_MAIN_FLOW_ID}' 加载失败，请检查 flows/ 目录",
                cmd_id=cmd_id,
            )

        # 重置流程控制标志，激活流程
        self._stop_event.clear()
        self._pause_event.set()
        self._process_active = True
        self.task_state_machine.start_task(cmd_id)

        engine = FlowEngine(
            graph,
            handlers=self._handlers,
            pause_event=self._pause_event,
            stop_event=self._stop_event,
            on_event=self._on_node_event,
            signal_bus=self._signal_bus,
        )
        self._engine = engine
        self._engine_thread = threading.Thread(
            target=self._run_engine, args=(engine,), daemon=True, name=f"WRC_FLOW-{cmd_id}",
        )
        self._engine_thread.start()

        return make_success_response(
            "已接收 PROCESS_BEGINS 命令，WRC_FLOW 流程开始工作",
            cmd_id=cmd_id,
            flow_id=_MAIN_FLOW_ID,
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
        MANUAL_RESET_COMPLETED：唤醒流程里等待 'manual_reset' 信号的 wait_for_command 节点
        （对应 flows/wrc_flow_main.json 里的 a1_wait_reset 节点）。
        """
        cmd_id = cmd_data.get("cmd_id")
        self._signal_bus.fire("manual_reset", data={})
        logger.info("WRC_FLOW", "收到 MANUAL_RESET_COMPLETED，已唤醒等待中的流程节点")
        return make_success_response("人工复位信号已接收", cmd_id=cmd_id)