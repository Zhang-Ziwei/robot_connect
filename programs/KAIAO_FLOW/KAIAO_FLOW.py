"""
KAIAO_FLOW 任务处理器（流程图编排版）。

对外的 HTTP 命令接口与 programs/KAIAO/KAIAO.py 完全一致，现有的
test_commands/KAIAO_*.json 可以直接复用，只需要把 active_project 切到 "KAIAO_FLOW"
就能对比两种实现，互不影响、随时切回。

与 WRC_FLOW 的关键区别：命令模型不一样
----------------------------------------
WRC_FLOW 是"一条 PROCESS_BEGINS 启动一个常驻循环流程"；
KAIAO 是"每条 HTTP 命令带着参数触发一段一次性任务，跑完回调外部系统"。

所以这里不是加载一张常驻大图，而是：

    cmd_type  ──映射──> 一张参数化的流程图 JSON
    命令入参  ──注入──> FlowContext（图上 wait_for_command 拿到完整 params，
                        嵌套字段可用 {{box_initial_area.shelf_type}}；
                        取放位置由 kaiao_bind_locations 节点解析成导航点名）
    流程结束  ──────> task_state_machine + callback_sender 回调

参数不再在本文件里解析成 pick_nav 再注入。否则每加一个 HTTP 字段、每加一个
条件判断都要改 Python。图上的等待节点 + 绑定节点才是参数的入口。
"""

import functools
import json
import os
import threading
import uuid
from typing import Any, Dict, Optional

from infrastructure.constants import ErrorCode
from infrastructure.error_logger import get_error_logger
from infrastructure.config_loader import get_require_process_begins
from core.flow_engine import FlowEngine, FlowResult, NodeRecord, SignalBus
from core.flow_store import find_enabled_flow_for_command, load_flow as _store_load_flow
from core.task_state_machine import TaskStateMachine

from programs.KAIAO.KAIAO import (
    KAIAOHandler,
    _make_action_response,
)
from programs.KAIAO_FLOW.node_handlers import build_handler_registry

logger = get_error_logger()
_LOG = "KAIAO_FLOW"

# Docker 外部挂载的流程配置目录优先（现场改流程用），项目内置目录兜底
_EXTERNAL_FLOWS_DIR = "/config/flows"
_LOCAL_FLOWS_DIR = os.path.join(os.path.dirname(__file__), "flows")

#: HTTP 命令 -> 流程图 id。新增一个命令的图形化版本，在这里加一行即可。
COMMAND_FLOWS: Dict[str, str] = {
    "PICK_BOX_TO_SP": "kaiao_pick_box_to_sp",
    "PICK_COMPONENT_TO_SP": "kaiao_pick_component_to_sp",
}

#: 还没画成流程图的命令：暂时转交 programs/KAIAO 的现成实现执行。
#:
#: 这是过渡状态，但转交动作**集中声明在本文件**，而不是散在 cmd_handler 里。
#: 差别很实在：cmd_handler 只认 KAIAO_FLOW 一个编排入口，
#: 以后把某条命令画成流程图，只需把它从这里挪到 COMMAND_FLOWS，
#: 不用再动命令分发那一层，也就不会出现"一半命令走图、一半走原生"却看不出来的局面。
PENDING_FLOW_COMMANDS: Dict[str, str] = {
    "PICK_UP_BOX":          "handle_pick_up_box",
    "PUT_DOWN_BOX":         "handle_put_down_box",
    "NAVIGATION":           "handle_navigation",
    "CANCEL_NAVIGATION":    "handle_cancel_navigation",
}

#: 本项目对外注册的全部命令。编辑器「等待外部命令」节点的下拉选项取自这里，
#: 操作人员不用去翻代码猜命令名。
ALL_COMMANDS = list(COMMAND_FLOWS.keys()) + list(PENDING_FLOW_COMMANDS.keys())


class KAIAOFlowHandler:
    """
    KAIAO_FLOW 任务处理器。

    内部持有一个真正的 KAIAOHandler，所有底层能力（导航含走廊中间点、抓放箱、
    货架重量追踪、持箱状态、回调发送）都复用它，本类只负责
    "命令入参 ⇄ 流程图执行" 这层编排。
    """

    def __init__(self, robots: dict = None, task_state_machine: TaskStateMachine = None):
        self.robots = robots or {}
        self.kaiao = KAIAOHandler(robots=self.robots, task_state_machine=task_state_machine)
        self.task_state_machine = self.kaiao.task_state_machine
        self.callback_sender = self.kaiao.callback_sender

        self._handlers = build_handler_registry(self.kaiao)

        # 暂停/终止：语义与 WRC_FLOW 一致，供后续扩展流程控制命令用
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._signal_bus = SignalBus()

        # 正在跑的流程引擎。外部命令进来时要问它"你在等这条命令吗"，
        # 以此决定是唤醒流程还是当成一条新任务受理。
        # KAIAO 是单机器人单任务模型（task_state_machine.is_busy 把关），
        # 同一时刻最多一个流程在跑，所以单个引用够用。
        self._active_engine: Optional[FlowEngine] = None
        self._engine_lock = threading.Lock()
        # 展会默认必须先 PROCESS_BEGINS；上位机把 require_process_begins=false 时视为已打开。
        self._process_begun = not get_require_process_begins()

    # ──────────────────────────────────────────────────────────────────────────
    # 流程加载与执行
    # ──────────────────────────────────────────────────────────────────────────

    def _load_flow(self, flow_id: str) -> Optional[Dict]:
        graph = _store_load_flow(flow_id, _LOCAL_FLOWS_DIR, _EXTERNAL_FLOWS_DIR)
        if graph is not None:
            logger.info(_LOG, f"加载流程 '{flow_id}'")
            return graph
        logger.error(
            _LOG,
            f"流程 '{flow_id}' 未找到（已尝试 {_EXTERNAL_FLOWS_DIR} 与 {_LOCAL_FLOWS_DIR}）",
        )
        return None

    def _resolve_command_flow(self, action_type: str):
        return find_enabled_flow_for_command(
            action_type, _LOCAL_FLOWS_DIR,
            mapped_flow_id=COMMAND_FLOWS.get(action_type),
        )

    def _on_node_event(self, node_id: str, status: str, record: NodeRecord):
        if status == "running":
            logger.info(_LOG, f"▶ 节点 {node_id}({record.type}) [{record.label}] 开始执行")
        else:
            mark = "✓" if status == "success" else "✗"
            logger.info(_LOG, f"{mark} 节点 {node_id}({record.type}) 结果={status} {record.message}")

    def _run_flow_async(self, flow_id: str, action_type: str, task_id: str,
                        context: Dict[str, Any], summary: str,
                        done_verb: str = "搬运完成"):
        """
        在后台线程里跑一遍流程图，结束后写任务状态并回调外部系统。
        与 KAIAO.py 各 handle_* 里 _run() 的收尾行为保持一致。
        """
        self._stop_event.clear()
        self._pause_event.set()
        graph = self._load_flow(flow_id)
        if graph is None:
            err = f"流程定义 '{flow_id}' 加载失败，请检查 flows/ 目录"
            self.task_state_machine.set_error(err)
            self.callback_sender.send(
                cmd_id=task_id, cmd_type=action_type, success=False, message=err,
            )
            return

        engine = FlowEngine(
            graph,
            handlers=self._handlers,
            pause_event=self._pause_event,
            stop_event=self._stop_event,
            on_event=self._on_node_event,
            signal_bus=self._signal_bus,
        )
        with self._engine_lock:
            self._active_engine = engine

        try:
            result: FlowResult = engine.run(initial_context=context)
        except Exception as exc:  # noqa: BLE001
            err = f"{action_type} 流程执行异常: {exc}"
            logger.exception_occurred(_LOG, action_type, exc)
            self.task_state_machine.set_error(err)
            self.callback_sender.send(
                cmd_id=task_id, cmd_type=action_type, success=False, message=err,
            )
            return
        finally:
            # 流程结束后必须摘掉，否则后续命令会被误判成"该唤醒某个等待中的节点"
            with self._engine_lock:
                if self._active_engine is engine:
                    self._active_engine = None

        if self._flow_succeeded(result):
            self.task_state_machine.complete_task(True, summary)
            self.callback_sender.send(
                cmd_id=task_id, cmd_type=action_type, success=True,
                message=f"{done_verb}: {summary}",
            )
            logger.info(_LOG, f"{action_type} 完成: {summary}")
        else:
            err = f"{action_type} 未正常完成: {self._failure_reason(result)}"
            logger.error(_LOG, err)
            self.task_state_machine.set_error(err)
            self.callback_sender.send(
                cmd_id=task_id, cmd_type=action_type, success=False, message=err,
            )

    @staticmethod
    def _flow_succeeded(result: FlowResult) -> bool:
        """
        判定这一趟任务到底成没成。

        不能只看 result.success：引擎的 success 表示"图正常走完没有异常中断"，
        某个节点失败后如果没有后继边，引擎会认为流程优雅结束并返回 success=True。
        对 WRC_FLOW 那种常驻循环无所谓，但 KAIAO 的一次性任务必须严格区分——
        否则半路抓箱失败也会给外部系统回调一条"搬运完成"。

        所以以图上两个显式出口为准：走到 mark_done 才算成功，碰过 mark_failed 就算失败。
        """
        if not result.success:
            return False
        ctx = result.context or {}
        return bool(ctx.get("task_done")) and not ctx.get("task_failed")

    @staticmethod
    def _failure_reason(result: FlowResult) -> str:
        """从执行轨迹里找出最后一个失败的节点，作为回调给外部系统的失败原因。"""
        for record in reversed(result.trace or []):
            if record.status == "failure":
                return f"节点 [{record.label or record.node_id}] 失败: {record.message}"
        ctx = result.context or {}
        if result.success and not ctx.get("task_done"):
            return "流程未走到成功出口（mark_done），请检查流程图连线是否完整"
        return result.message

    # ──────────────────────────────────────────────────────────────────────────
    # 命令注册与分发
    # ──────────────────────────────────────────────────────────────────────────

    def build_command_map(self) -> Dict[str, Any]:
        """
        本项目对外注册的全部命令，供 cmd_handler 一次性登记。

        cmd_handler 只跟这一个入口打交道，不再直接引用 programs/KAIAO 的处理器——
        编排层的归属因此是明确的：所有 KAIAO_FLOW 命令都从这里进，
        至于某条命令内部是走流程图还是暂时转交原生实现，属于本项目的内部决定。
        """
        mapping = {cmd: functools.partial(self.dispatch, cmd) for cmd in ALL_COMMANDS}
        mapping.update({
            "PROCESS_BEGINS": self.handle_process_begins,
            "PROCESS_PAUSED": self.handle_process_paused,
            "PROCESS_RESUMED": self.handle_process_resumed,
            "PROCESS_ENDED": self.handle_process_ended,
        })
        return mapping

    def dispatch(self, action_type: str, cmd_data: Dict) -> Dict:
        """
        统一命令分发，按三种情况处理：

        1. **有流程正等着这条命令** → 唤醒它，把命令参数注入流程变量。
           这样流程图里可以画"等人工确认""等上位机放行"这类停顿点。
        2. **这条命令已经画成流程图** → 起一趟新任务，按图执行。
        3. **还没画成流程图** → 转交 programs/KAIAO 的现成实现（过渡状态）。

        顺序不能反：先判唤醒，再判新任务。否则流程停在等待节点时，
        外部发来的唤醒命令会被 task_state_machine 的忙碌检查挡掉，流程永远醒不过来。
        """
        woken = self._try_wake_flow(action_type, cmd_data)
        if woken is not None:
            return woken

        if get_require_process_begins() and not self._process_begun:
            return _make_action_response(
                False, action_type,
                message="流程尚未打开，请先发送 PROCESS_BEGINS",
                code=ErrorCode.TASK_NOT_STARTED,
            )

        if action_type in COMMAND_FLOWS:
            starter = self._FLOW_STARTERS.get(action_type)
            if starter is None:
                return _make_action_response(
                    False, action_type,
                    message=f"命令 {action_type} 已配置流程图但缺少参数解析入口",
                    code=ErrorCode.INVALID_PARAMS,
                )
            return starter(self, cmd_data)

        method_name = PENDING_FLOW_COMMANDS.get(action_type)
        if method_name is None:
            return _make_action_response(
                False, action_type, message=f"未注册的命令: {action_type}",
                code=ErrorCode.INVALID_PARAMS,
            )
        logger.info(_LOG, f"{action_type} 尚未图形化，转交 KAIAO 原生实现执行")
        return getattr(self.kaiao, method_name)(cmd_data)

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        self._process_begun = True
        self._stop_event.clear()
        self._pause_event.set()
        params = cmd_data.get("params") if isinstance(cmd_data.get("params"), dict) else {}
        with self._engine_lock:
            engine = self._active_engine
        if engine is not None and engine.is_waiting_for("PROCESS_BEGINS"):
            self._signal_bus.fire("PROCESS_BEGINS", data=params)
            return _make_action_response(
                True, "PROCESS_BEGINS",
                message="已唤醒等待 PROCESS_BEGINS 的流程节点",
            )
        return _make_action_response(
            True, "PROCESS_BEGINS",
            message="流程开关已打开，可接收业务命令",
        )

    def handle_process_paused(self, cmd_data: Dict) -> Dict:
        with self._engine_lock:
            engine = self._active_engine
        if engine is None:
            return _make_action_response(
                False, "PROCESS_PAUSED",
                message="当前没有正在运行的流程",
                code=ErrorCode.TASK_NOT_FOUND,
            )
        self._pause_event.clear()
        return _make_action_response(True, "PROCESS_PAUSED", message="流程已暂停")

    def handle_process_resumed(self, cmd_data: Dict) -> Dict:
        with self._engine_lock:
            engine = self._active_engine
        if engine is None:
            return _make_action_response(
                False, "PROCESS_RESUMED",
                message="当前没有正在运行的流程",
                code=ErrorCode.TASK_NOT_FOUND,
            )
        self._pause_event.set()
        return _make_action_response(True, "PROCESS_RESUMED", message="流程已恢复")

    def handle_process_ended(self, cmd_data: Dict) -> Dict:
        self._stop_event.set()
        self._pause_event.set()
        if get_require_process_begins():
            self._process_begun = False
        return _make_action_response(True, "PROCESS_ENDED", message="流程结束请求已接收")

    def _try_wake_flow(self, action_type: str, cmd_data: Dict) -> Optional[Dict]:
        """
        若有流程正阻塞在等待该命令的节点上，就唤醒它；否则返回 None 交由常规分发。
        """
        with self._engine_lock:
            engine = self._active_engine
        if engine is None or not engine.is_waiting_for(action_type):
            return None

        params = dict(cmd_data.get("params") or {})
        extra = cmd_data.get("extra") or {}
        # robot_id 经常写在 extra 里（KAIAO 分拣命令就是这样），唤醒后
        # 图上 {{robot_id}} / 调动作节点才能选到这台机器人。
        if isinstance(extra, dict) and extra.get("robot_id") and not params.get("robot_id"):
            params["robot_id"] = extra["robot_id"]
        self._signal_bus.fire(action_type, data=params)
        logger.info(_LOG, f"命令 {action_type} 唤醒了等待中的流程，注入参数: {list(params)}")
        return _make_action_response(
            True, action_type,
            message=f"已唤醒等待 {action_type} 的流程节点",
        )

    def waiting_commands(self) -> list:
        """当前流程正在等待的命令名（供状态查询/排障用）。"""
        with self._engine_lock:
            engine = self._active_engine
        return sorted(engine.waiting_signals()) if engine else []

    # ──────────────────────────────────────────────────────────────────────────
    # 命令入口（与 programs/KAIAO/KAIAO.py 的接口保持一致）
    # ──────────────────────────────────────────────────────────────────────────

    def handle_pick_box_to_sp(self, cmd_data: Dict) -> Dict:
        """
        PICK_BOX_TO_SP —— 启动（或唤醒）flows/kaiao_pick_box_to_sp.json。

        入参原样交给流程图：开头的 wait_for_command 收到完整 params，
        kaiao_bind_locations 再把 box_initial_area / box_target_area 解析成
        pick_nav 等导航变量。本函数只做机器人存在性和忙碌检查。
        """
        action_type = "PICK_BOX_TO_SP"
        params = dict(cmd_data.get("params") or {})
        extra = cmd_data.get("extra") or {}
        if isinstance(extra, dict) and extra.get("robot_id") and not params.get("robot_id"):
            params["robot_id"] = extra["robot_id"]
        cmd_id = cmd_data.get("cmd_id", "")

        robot_id, robot, err_resp = self.kaiao._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        if self.task_state_machine.is_busy():
            return self.kaiao._busy_response(action_type, robot_id)

        flow_id, _graph, err = self._resolve_command_flow(action_type)
        if err:
            return _make_action_response(
                False, action_type, message=err, code=ErrorCode.CMD_EXECUTION_ERROR,
            )

        task_id = cmd_id or str(uuid.uuid4())
        context: Dict[str, Any] = {
            "robot_id": robot_id,
            "task_id": task_id,
            "action_type": action_type,
        }
        context.update(params)
        src = params.get("box_initial_area") or {}
        dst = params.get("box_target_area") or {}
        summary = (
            f"{src.get('shelf_type', '?')}{src.get('shelf_num', '')} → "
            f"{dst.get('shelf_type', '?')}{dst.get('shelf_num', '')}"
        )

        self.task_state_machine.start_task(task_id, robot_id)
        self._stop_event.clear()
        self._pause_event.set()
        # 图从 wait_for_command(PICK_BOX_TO_SP) 开始：先把本条命令打进总线，
        # 引擎跑到等待节点时事件已置位，立刻拿到这包 params，不用再等第二次 HTTP。
        # 若流程已在跑、正停在同一个等待节点上，dispatch 会走 _try_wake_flow，到不了这里。
        self._signal_bus.fire(action_type, data=params)

        threading.Thread(
            target=self._run_flow_async,
            args=(flow_id, action_type, task_id, context, summary),
            daemon=True, name=f"kaiao-flow-pick-box-{robot_id}",
        ).start()

        return _make_action_response(
            True, action_type, message=f"动作 {action_type} 已受理，后台执行中（流程图编排）",
        )

    def handle_pick_component_to_sp(self, cmd_data: Dict) -> Dict:
        """
        PICK_COMPONENT_TO_SP —— 启动 flows/kaiao_pick_component_to_sp.json。

        params 是对象或对象列表。入口只做机器人/忙碌检查，把列表原样写成 jobs
        打进等待节点；kaiao_bind_component_jobs 再摊成逐件队列，图上循环抓放。
        """
        action_type = "PICK_COMPONENT_TO_SP"
        raw = cmd_data.get("params")
        extra = cmd_data.get("extra") or {}
        cmd_id = cmd_data.get("cmd_id", "")

        robot_id, robot, err_resp = self.kaiao._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        parsed_jobs, job_err = self.kaiao._parse_component_jobs(raw)
        if job_err:
            return _make_action_response(
                False, action_type, message=job_err, code=ErrorCode.INVALID_PARAMS,
            )

        if self.task_state_machine.is_busy():
            return self.kaiao._busy_response(action_type, robot_id)

        flow_id, _graph, err = self._resolve_command_flow(action_type)
        if err:
            return _make_action_response(
                False, action_type, message=err, code=ErrorCode.CMD_EXECUTION_ERROR,
            )

        payload: Dict[str, Any] = {"jobs": raw, "robot_id": robot_id}
        if isinstance(extra, dict) and extra.get("robot_id"):
            payload["robot_id"] = extra["robot_id"]

        summary = ", ".join(
            f"{job['comp_type']}x{sum(t['component_number'] for t in job['targets'])}"
            f"(box{job['box_num']})"
            for job in parsed_jobs
        )

        task_id = cmd_id or str(uuid.uuid4())
        context: Dict[str, Any] = {
            "robot_id": robot_id,
            "task_id": task_id,
            "action_type": action_type,
            "jobs": raw,
        }

        self.task_state_machine.start_task(task_id, robot_id)
        self._stop_event.clear()
        self._pause_event.set()
        self._signal_bus.fire(action_type, data=payload)

        threading.Thread(
            target=self._run_flow_async,
            args=(flow_id, action_type, task_id, context,
                  summary, "零件搬运完成"),
            daemon=True, name=f"kaiao-flow-pick-comp-{robot_id}",
        ).start()

        return _make_action_response(
            True, action_type, message=f"动作 {action_type} 已受理，后台执行中（流程图编排）",
        )

    #: 已图形化命令的参数解析入口。键要与 COMMAND_FLOWS 一致，
    #: 新增一张流程图时两处一起加，dispatch 会据此找到对应的入参解析。
    _FLOW_STARTERS = {
        "PICK_BOX_TO_SP": handle_pick_box_to_sp,
        "PICK_COMPONENT_TO_SP": handle_pick_component_to_sp,
    }
