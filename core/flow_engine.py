"""
core/flow_engine.py

通用流程图执行引擎（Flow Engine）
=================================

背景
----
现在每个项目（ATC.py / KAIAO.py / WRC.py …）的业务流程都是"写死在 Python 函数里的一串
顺序调用"：导航 → 抓取 → 更新状态机 → 导航 → 放置 → 等信号 → ... 每一步调用的其实都是几个
可复用的"原子能力"（导航、发送操作动作、状态机记录、等待外部命令……）。

FlowEngine 把"流程骨架"（先做什么、什么并行、什么等待、失败怎么办）从 Python 代码里拿出来，
变成一份可以被图形化编辑器拖拽生成的 JSON 数据（见下方《流程 JSON 规范》），运行时由本引擎
解释执行；而"动作原语"仍然是各项目自己实现、自己注册的函数，引擎不关心机器人/硬件细节。

设计原则
--------
1. 引擎只负责"控制流"：顺序执行、条件分支、并行/汇合、循环（用环形连线表达）、
   延时、变量赋值、等待外部事件——这些逻辑是通用的，写在引擎里，不需要每个项目重复实现。
2. 真正会碰机器人/硬件的"动作类"节点（navigate / send_operation / plc_action /
   update_step 等）由各项目自己实现处理函数，通过 ``NodeHandlerRegistry`` 注册进来，
   引擎执行到这类节点时按 ``node.type`` 查表调用，引擎本身不 import 任何硬件层代码。
3. 节点参数可以引用之前节点写入的上下文变量，用 ``"{{var_name}}"`` 模板语法。
4. 每个节点执行结果只有 成功 / 失败 两种。默认失败即整条流程失败并停止；
   如果该节点在图上有 ``when="failure"`` 的出边，则按此出边继续（例如"导航失败重试一次"）。
5. 支持暂停 / 恢复 / 结束：引擎在执行每个节点之前检查外部传入的
   ``pause_event`` / ``stop_event``（与现有 WRC.py 的 `_pause_event`/`_stop_event`
   语义完全一致），可以直接复用项目里已有的事件对象。
6. "演练模式"（dry-run）不需要引擎知道任何事情——只需要把 handler 注册表换成一套
   连接 mock 机器人的实现即可，引擎逻辑完全复用，见 ``programs/WRC_FLOW/README.md``。
7. 循环用"图上的环"表达（某节点的出边指回前面的节点），不需要专门的 loop 节点类型，
   这也是大多数图形化流程工具（Node-RED / n8n）的通用做法，图形编辑器画起来也直观。

────────────────────────────────────────────────────────────────────────────
流程 JSON 规范（FlowGraph）
────────────────────────────────────────────────────────────────────────────
{
  "flow_id": "wrc_trans_component_robot_a",
  "start": "n1",                       // 起始节点 id
  "context_vars": {"loop_count": 0},   // 初始上下文变量（可选）
  "nodes": [
    {"id": "n1", "type": "navigate", "label": "导航到P1", "params": {"pose": "P1"}},
    {"id": "n2", "type": "send_operation", "label": "抓取零件A",
     "params": {"call_type": "action", "task": "pick_up_component_A", "area": "P1"}},
    {"id": "n3", "type": "condition", "label": "P1是否有料",
     "params": {"var": "p1_state", "op": "==", "value": "FULL"}},
    {"id": "n4", "type": "parallel", "label": "并行示例",
     "params": {"branches": ["n5", "n6"], "join": "all"}}
  ],
  "edges": [
    {"source": "n1", "target": "n2"},                       // when 省略 = "default"
    {"source": "n2", "target": "n3", "when": "success"},
    {"source": "n2", "target": "nX", "when": "failure"},     // 失败分支（可选）
    {"source": "n3", "target": "n1", "when": "true"},        // 条件分支（条件节点专用）
    {"source": "n3", "target": "nY", "when": "false"}
  ]
}

内置节点类型（引擎自带实现，不需要项目注册 handler）：
    condition       —— 按 params.{var,op,value} 判定 true/false，走对应出边
    set_variable    —— 把 params.value（支持模板）写入 context[params.var]
    delay           —— 等待 params.seconds 秒（每 0.2s 检查一次 stop_event，可被及时打断）
    parallel        —— 并行分叉：从本节点拉出的每条边都是一条独立线程。
                        不汇合则各支路各自跑完；多条支路连到同一个 noop/汇合点时，
                        在汇合点用 params.join=all/any 决定何时从汇合点只往下走一次。
                        并行节点本身没有 success/failure 出边。
    wait_for_command—— 阻塞等待外部通过 SignalBus.fire() 触发的信号（比如 NEXT_STEP 命令）
    sub_flow        —— 调用另一个已注册的流程（通过构造函数传入的 flow_loader 获取）
    noop            —— 占位；作为并行汇合点时用 params.join（all/any）

需要项目自行注册 handler 的"动作类"节点类型（示例，实际类型名由项目自己定义）：
    navigate / send_operation / plc_action / update_step ...
    handler 签名：handler(node: FlowNode, ctx: "FlowContext") -> bool
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()
_LOG = "FlowEngine"

# 节点参数里引用上下文变量的模板语法：{{var_name}}
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

# 内置节点类型（不需要外部注册 handler）
_BUILTIN_TYPES = frozenset({
    "condition", "set_variable", "delay", "parallel", "wait_for_command",
    "sub_flow", "noop",
})

def _to_number(value):
    """尽量把上下文里的值当数字用，返回 (数值, 是否成功)。"""
    if isinstance(value, bool):
        return (int(value), True)
    if isinstance(value, (int, float)):
        return (value, True)
    try:
        return (float(str(value).strip()), True)
    except (TypeError, ValueError):
        return (0, False)


_CONDITION_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "truthy": lambda a, b: bool(a),
    "falsy": lambda a, b: not bool(a),
}


class FlowStopped(Exception):
    """内部信号：流程被 stop_event 中断，非错误，用于跳出执行循环。"""


# ──────────────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FlowNode:
    id: str
    type: str
    label: str = ""
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FlowEdge:
    source: str
    target: str
    when: str = "default"   # default | success | failure | true | false


@dataclass
class NodeRecord:
    """一次节点执行的记录，用于状态查询接口 / GUI 高亮回放。"""
    node_id: str
    type: str
    label: str
    status: str              # running | success | failure | skipped
    message: str = ""
    started_at: str = ""
    ended_at: str = ""


@dataclass
class FlowResult:
    success: bool
    status: str               # completed | failed | stopped | paused_timeout
    message: str
    context: Dict[str, Any]
    trace: List[NodeRecord] = field(default_factory=list)


# 编辑器下拉框用命令名，旧流程图用短名。fire / consume / is_waiting_for 视作同一信号。
_SIGNAL_ALIAS_GROUPS = (
    frozenset({"manual_reset", "MANUAL_RESET_COMPLETED"}),
)


def signal_aliases(name: str) -> tuple:
    for group in _SIGNAL_ALIAS_GROUPS:
        if name in group:
            return tuple(group)
    return (name,)


class SignalBus:
    """
    命名信号总线，供 ``wait_for_command`` 节点与外部 HTTP 命令入口（比如 NEXT_STEP、
    MANUAL_RESET_COMPLETED）之间解耦通信。

    典型用法：
        bus = SignalBus()
        # 流程里某节点会调用 bus.wait("next_step", timeout=600)
        # HTTP 命令处理函数收到 NEXT_STEP 命令后调用：
        bus.fire("next_step", data={"foo": "bar"})
    """

    def __init__(self):
        self._events: Dict[str, threading.Event] = {}
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def _get_event(self, name: str) -> threading.Event:
        with self._lock:
            ev = self._events.get(name)
            if ev is None:
                ev = threading.Event()
                self._events[name] = ev
            return ev

    def fire(self, name: str, data: Any = None):
        for n in signal_aliases(name):
            with self._lock:
                self._data[n] = data
            self._get_event(n).set()

    def wait(self, name: str, timeout: Optional[float] = None) -> bool:
        """返回 True=收到信号，False=超时"""
        ok = self._get_event(name).wait(timeout=timeout)
        return ok

    def consume(self, name: str) -> Any:
        """取出信号携带的数据并清空事件（含别名），供下一次等待复用。"""
        data = None
        for n in signal_aliases(name):
            with self._lock:
                if data is None and n in self._data:
                    data = self._data.pop(n, None)
                else:
                    self._data.pop(n, None)
            self._get_event(n).clear()
        return data


# ──────────────────────────────────────────────────────────────────────────────
# 上下文（运行期共享状态，线程安全）
# ──────────────────────────────────────────────────────────────────────────────

class FlowContext:
    """
    流程运行期共享的上下文对象。

    - ``variables``：流程变量（节点参数里的 ``{{var}}`` 模板从这里取值）。
    - ``robot`` / ``robot_id`` / 项目自定义的其它引用（state_machine、slot_tracker
      等）都可以塞进 ``extra``，供各项目自己的 handler 使用；引擎不关心其内容。
    - 所有读写都加锁，允许并行分支节点同时访问。
    """

    def __init__(self, variables: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None):
        self._lock = threading.RLock()
        self.variables: Dict[str, Any] = dict(variables or {})
        self.extra: Dict[str, Any] = dict(extra or {})

    def get(self, name: str, default: Any = None) -> Any:
        """
        取流程变量。支持点号路径，例如 ``box_initial_area.shelf_type``
        会先精确匹配整个键，没有再沿 dict 逐层往下取。
        这样 HTTP 命令里的嵌套 params 可以直接用在条件判断和 ``{{ }}`` 模板里，
        新增字段不必再改 Python。
        """
        with self._lock:
            if name in self.variables:
                return self.variables[name]
            if "." not in name:
                return default
            cur: Any = self.variables
            for part in name.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return default
            return cur

    def set(self, name: str, value: Any):
        with self._lock:
            self.variables[name] = value

    def update(self, values: Dict[str, Any]):
        with self._lock:
            self.variables.update(values or {})

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.variables)

    # ── 模板渲染 ─────────────────────────────────────────────────────────────
    def render(self, value: Any) -> Any:
        """
        递归渲染 params 里的 ``{{var}}`` 模板：
            "P{{n}}"          -> 字符串替换
            {"a": "{{x}}"}    -> 递归处理 dict
            ["{{x}}", 1]      -> 递归处理 list
            其它类型原样返回
        """

        if isinstance(value, str):
            def _sub(m):
                key = m.group(1)
                v = self.get(key, "")
                return "" if v is None else str(v)
            # 整串恰好是单个模板变量时，保留原始类型（比如数字/布尔/dict）
            whole_match = _TEMPLATE_RE.fullmatch(value)
            if whole_match:
                return self.get(whole_match.group(1))
            return _TEMPLATE_RE.sub(_sub, value)
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        return value


# ──────────────────────────────────────────────────────────────────────────────
# FlowEngine
# ──────────────────────────────────────────────────────────────────────────────

NodeHandler = Callable[[FlowNode, FlowContext], bool]


class FlowEngine:
    """
    通用流程图执行器。

    Parameters
    ----------
    graph:
        流程 JSON（见模块 docstring《流程 JSON 规范》），已解析为 dict。
    handlers:
        项目自定义"动作类"节点的处理函数注册表 {node_type: handler}。
        handler 签名：``handler(node, ctx) -> bool``，返回 True/False 表示该节点成功/失败，
        可以在 handler 内部调用 ``ctx.set(...)`` 把结果写回上下文供后续节点使用。
    pause_event / stop_event:
        与现有项目（如 WRC.py 的 `_pause_event` / `_stop_event`）语义一致的
        ``threading.Event``；可以直接把项目里已有的对象传进来复用，也可以不传
        （此时该流程不支持外部暂停/停止）。
        - pause_event：unset（clear）= 暂停中，引擎会阻塞在这里；set = 正常运行。
        - stop_event：set = 请求终止，引擎会在下一个节点执行前安全退出。
    on_event:
        可选回调 ``on_event(node_id, status, record: NodeRecord)``，每个节点开始/结束
        时调用一次，供 GUI 实时高亮、日志打印等用途。
    flow_loader:
        可选回调 ``flow_loader(flow_id) -> dict``，用于 ``sub_flow`` 节点按 id 加载
        另一份流程 JSON。
    signal_bus:
        可选 ``SignalBus`` 实例，供 ``wait_for_command`` 节点使用；未传入时自动创建一个
        仅本次运行有效的临时实例（适合非跨命令等待的场景）。
    """

    def __init__(
        self,
        graph: Dict[str, Any],
        handlers: Optional[Dict[str, NodeHandler]] = None,
        pause_event: Optional[threading.Event] = None,
        stop_event: Optional[threading.Event] = None,
        on_event: Optional[Callable[[str, str, NodeRecord], None]] = None,
        flow_loader: Optional[Callable[[str], Dict[str, Any]]] = None,
        signal_bus: Optional[SignalBus] = None,
    ):
        self.graph = graph
        self.handlers = dict(handlers or {})
        self.pause_event = pause_event
        self.stop_event = stop_event
        self.on_event = on_event
        self.flow_loader = flow_loader
        self.signal_bus = signal_bus or SignalBus()

        # 当前阻塞在 wait_for_command 上的信号名。外部命令入口据此判断
        # "这条命令是该唤醒流程，还是该当成一条新任务受理"（见 waiting_signals）。
        # 用集合是因为 parallel 分支下可能同时有多个节点在等不同信号。
        self._waiting_signals: Dict[str, int] = {}
        self._waiting_lock = threading.Lock()

        self._nodes: Dict[str, FlowNode] = {}
        self._edges_by_source: Dict[str, List[FlowEdge]] = {}
        self._parse_graph()

    # ── 等待中的外部信号（供命令入口查询）──────────────────────────────────

    def waiting_signals(self) -> set:
        """返回此刻有节点正阻塞等待的信号名集合（线程安全，返回快照）。"""
        with self._waiting_lock:
            return {name for name, cnt in self._waiting_signals.items() if cnt > 0}

    def is_waiting_for(self, name: str) -> bool:
        """是否有节点正在等待名为 name 的信号（含别名）。"""
        with self._waiting_lock:
            return any(self._waiting_signals.get(n, 0) > 0 for n in signal_aliases(name))

    def _mark_waiting(self, name: str, delta: int):
        with self._waiting_lock:
            cnt = self._waiting_signals.get(name, 0) + delta
            if cnt > 0:
                self._waiting_signals[name] = cnt
            else:
                self._waiting_signals.pop(name, None)

    # ── 图解析 & 校验 ────────────────────────────────────────────────────────
    
    def _parse_graph(self):
        for n in self.graph.get("nodes", []):
            node = FlowNode(id=n["id"], type=n["type"], label=n.get("label", ""), params=n.get("params", {}) or {})
            self._nodes[node.id] = node
        for e in self.graph.get("edges", []):
            when = e.get("when")
            if when in (None, ""):
                when = e.get("branch") or "default"
            edge = FlowEdge(source=e["source"], target=e["target"], when=when)
            self._edges_by_source.setdefault(edge.source, []).append(edge)
    
    def validate(self) -> List[str]:
        """
        静态校验流程图合法性，返回错误信息列表（空列表 = 校验通过）。
        供保存前 / GUI 编辑时调用，不依赖真实机器人环境。
        """
        errors: List[str] = []
        if "start" not in self.graph:
            errors.append("缺少 'start' 字段：未指定起始节点")
        elif self.graph["start"] not in self._nodes:
            errors.append(f"起始节点 '{self.graph['start']}' 不存在于 nodes 列表中")

        node_ids = set(self._nodes.keys())
        for node_id, node in self._nodes.items():
            if node.type not in _BUILTIN_TYPES and node.type not in self.handlers:
                errors.append(f"节点 {node_id} 的类型 '{node.type}' 未注册 handler，且不是内置类型")
            if node.type == "condition":
                if "var" not in node.params or "op" not in node.params:
                    errors.append(f"condition 节点 {node_id} 缺少 params.var / params.op")
                elif node.params["op"] not in _CONDITION_OPS:
                    errors.append(f"condition 节点 {node_id} 的 op '{node.params['op']}' 不受支持")
            if node.type == "parallel":
                branches = self._parallel_branch_starts(node)
                if not branches:
                    errors.append(
                        f"parallel 节点 {node_id} 没有分支：请从「分支」端口拉线，"
                        "或在 params.branches 里填写起点节点 id"
                    )
                for b in branches:
                    if b not in node_ids:
                        errors.append(f"parallel 节点 {node_id} 的分支起点 '{b}' 不存在")
            if node.type == "wait_for_command" and "event_name" not in node.params:
                errors.append(f"wait_for_command 节点 {node_id} 缺少 params.event_name")

        for src, edges in self._edges_by_source.items():
            if src not in node_ids:
                errors.append(f"边的起点 '{src}' 不存在")
            for e in edges:
                if e.target not in node_ids:
                    errors.append(f"边 {src} -> {e.target} 的终点不存在")

        return errors

    # ── 执行 ─────────────────────────────────────────────────────────────────

    def run(self, initial_context: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None,
            max_steps: int = 100000) -> FlowResult:
        """
        从 graph['start'] 开始执行整张图，直到走到没有下一个节点的地方、
        流程被判定失败（且无失败分支）、或收到 stop_event。

        max_steps 是安全阀（防止图配置错误导致死循环把线程堵死），默认十万步，
        真实业务流程正常执行数十到数百步就会结束一轮或进入下一次循环等待。
        """
        ctx = FlowContext(variables={**self.graph.get("context_vars", {}), **(initial_context or {})},
                        extra=extra or {})
        trace: List[NodeRecord] = []
        errors = self.validate()
        if errors:
            return FlowResult(False, "invalid", "流程图校验失败: " + "; ".join(errors), ctx.snapshot(), trace)

        current = self.graph.get("start")
        steps = 0
        try:
            while current is not None:
                steps += 1
                if steps > max_steps:
                    return FlowResult(False, "failed", f"超过最大执行步数 {max_steps}，可能存在死循环", ctx.snapshot(), trace)
                self._check_pause_stop()
                success, message = self._execute_one(current, ctx, trace)
                current = self._pick_next(current, success, ctx)
            return FlowResult(True, "completed", "流程执行完成", ctx.snapshot(), trace)
        except FlowStopped:
            return FlowResult(False, "stopped", "流程被外部 stop_event 中断", ctx.snapshot(), trace)
        except Exception as e:  # noqa: BLE001 —— 顶层兜底，避免线程静默崩溃
            logger.error(_LOG, f"流程执行异常: {e}")
            return FlowResult(False, "error", f"流程执行异常: {e}", ctx.snapshot(), trace)

    def run_chain(
        self,
        start_node_id: str,
        ctx: FlowContext,
        trace: List[NodeRecord],
        max_steps: int = 10000,
        stop_before: Optional[set] = None,
        arrived_at: Optional[Dict[str, Optional[str]]] = None,
    ) -> bool:
        """
        执行以 start_node_id 起始的一条子链，直到无路可走或失败无失败分支为止。
        供顶层 run() 与 parallel 节点的分支线程共用。返回 True/False 表示这条子链
        最终是否以"成功"状态收尾。

        stop_before: 即将进入这些节点时停住（不执行），用于并行汇合点。
        arrived_at: 若因汇合点停下，写入 {start_node_id: 汇合节点 id}。
        """
        barriers = stop_before or set()
        current = start_node_id
        steps = 0
        success = True
        while current is not None:
            steps += 1
            if steps > max_steps:
                logger.warning(_LOG, f"子链 {start_node_id} 超过最大步数 {max_steps}")
                return False
            self._check_pause_stop()
            success, _ = self._execute_one(current, ctx, trace)
            nxt = self._pick_next(current, success, ctx)
            if success and nxt is not None and nxt in barriers:
                if arrived_at is not None:
                    arrived_at[start_node_id] = nxt
                return True
            current = nxt
        return success 

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    def _check_pause_stop(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise FlowStopped()
        if self.pause_event is not None:
            # pause_event 未 set 时会一直阻塞，期间每 0.5s 醒来检查一次 stop_event，
            # 保证"暂停中"也能响应 PROCESS_ENDED（stop）。
            while not self.pause_event.wait(timeout=0.5):
                if self.stop_event is not None and self.stop_event.is_set():
                    raise FlowStopped()

    def _emit(self, record: NodeRecord):
        if self.on_event:
            try:
                self.on_event(record.node_id, record.status, record)
            except Exception as e:  # noqa: BLE001
                logger.warning(_LOG, f"on_event 回调异常: {e}")

    def _execute_one(self, node_id: str, ctx: FlowContext, trace: List[NodeRecord]) -> (bool, str):
        node = self._nodes.get(node_id)
        if node is None:
            raise RuntimeError(f"节点 {node_id} 不存在")

        record = NodeRecord(node_id=node.id, type=node.type, label=node.label,
                             status="running", started_at=datetime.now().isoformat())
        trace.append(record)
        self._emit(record)

        try:
            success, message = self._dispatch(node, ctx)
        except FlowStopped:
            raise
        except Exception as e:  # noqa: BLE001
            success, message = False, f"节点执行异常: {e}"
            logger.error(_LOG, f"节点 {node_id}({node.type}) 执行异常: {e}")

        record.status = "success" if success else "failure"
        record.message = message
        record.ended_at = datetime.now().isoformat()
        self._emit(record)
        return success, message

    def _dispatch(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        if node.type in _BUILTIN_TYPES:
            return getattr(self, f"_builtin_{node.type}")(node, ctx)
        handler = self.handlers.get(node.type)
        if handler is None:
            return False, f"未注册节点类型 '{node.type}' 的处理函数"
        ok = handler(node, ctx)
        return bool(ok), "" if ok else f"节点 {node.id} 处理函数返回失败"

    def _pick_next(self, node_id: str, outcome: Any, ctx: FlowContext) -> Optional[str]:
        edges = self._edges_by_source.get(node_id, [])
        if not edges:
            return None
        node = self._nodes[node_id]

        if node.type == "condition":
            want = "true" if outcome else "false"
            for e in edges:
                if e.when == want:
                    return e.target
            return None  # 没配对应分支，流程在此结束

        # parallel 只负责分叉，出边全是支路起点，执行完后不顺着它们再走。
        if node.type == "parallel":
            return None

        # 普通节点：成功优先走 success/default，失败走 failure（没配则终止）
        if outcome:
            for e in edges:
                if e.when in ("success", "default"):
                    return e.target
            return None
        else:
            for e in edges:
                if e.when == "failure":
                    return e.target
            return None

    # ── 内置节点实现 ─────────────────────────────────────────────────────────

    def _builtin_noop(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        return True, ""

    def _builtin_set_variable(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        var = node.params.get("var")
        if not var:
            return False, "set_variable 节点缺少 params.var"
        # 变量名本身也支持 {{var}} 模板，可以拼出动态变量名（比如按 find_slot 找到的
        # 槽位名拼 "{{p3_n}}_state"），用来表达"操作哪个具体槽位"这类动态分支逻辑，
        # 不需要为每个具体槽位单独画一份 condition/set_variable。
        var = ctx.render(var)
        value = ctx.render(node.params.get("value"))

        # op 默认 set（纯赋值），没写 op 的旧流程图行为完全不变。
        # add/sub 是为了让"重试计数"这类循环能在图上表达——否则每加一次 1
        # 都得回 Python 里写个专用节点。
        op = node.params.get("op") or "set"
        if op == "set":
            ctx.set(var, value)
            return True, f"{var} = {ctx.get(var)!r}"

        cur, ok_cur = _to_number(ctx.get(var))
        delta, ok_delta = _to_number(value)
        if not ok_cur or not ok_delta:
            return False, (
                f"{var} 做 {op} 运算需要数字，当前 {var}={ctx.get(var)!r}、value={value!r}"
            )
        result = cur + delta if op == "add" else cur - delta
        if float(result).is_integer():
            result = int(result)
        ctx.set(var, result)
        return True, f"{var} = {result!r}（{op} {delta!r}）"

    def _builtin_delay(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        seconds = float(node.params.get("seconds", 0))
        end = time.time() + seconds
        while time.time() < end:
            self._check_pause_stop()
            time.sleep(min(0.2, max(0.0, end - time.time())))
        return True, f"等待 {seconds}s 完成"

    def _builtin_condition(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        var = ctx.render(node.params.get("var"))  # 变量名支持模板，理由同 _builtin_set_variable
        op = node.params.get("op")
        expect = ctx.render(node.params.get("value"))
        actual = ctx.get(var)
        fn = _CONDITION_OPS.get(op)
        if fn is None:
            return False, f"不支持的 op: {op}"
        result = bool(fn(actual, expect))
        # 条件节点的“成功/失败”即 true/false 本身，交给 _pick_next 按 true/false 选边
        return result, f"{var}({actual!r}) {op} {expect!r} => {result}"

    def _builtin_wait_for_command(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        """
        阻塞等待 SignalBus 上的命名信号。

        没有直接用一次性的 ``signal_bus.wait(timeout=timeout)``，是因为那样在
        ``timeout=None``（无限等待）时，一旦外部发来 PROCESS_ENDED（``stop_event.set()``），
        本节点会因为还卡在一次长阻塞里而无法及时响应——必须每隔一小段时间醒一次，
        重新检查 pause/stop 状态，才能保证"暂停/结束"命令随时能打断等待。
        """
        event_name = node.params["event_name"]
        timeout = node.params.get("timeout")
        # 命令携带的参数写进上下文时的变量名前缀。默认用信号名，避免多个等待节点
        # 互相覆盖；填了 var_prefix 就用它，图上引用起来更短（{{cmd_shelf_level}}）。
        prefix = node.params.get("var_prefix") or event_name
        poll_interval = 0.3
        deadline = None if timeout is None else time.time() + timeout

        self._mark_waiting(event_name, +1)
        try:
            while True:
                self._check_pause_stop()  # stop_event 已 set 时在此抛出 FlowStopped
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return False, f"等待信号 '{event_name}' 超时（{timeout}s）"
                    wait_slice = min(poll_interval, remaining)
                else:
                    wait_slice = poll_interval

                if self.signal_bus.wait(event_name, timeout=wait_slice):
                    data = self.signal_bus.consume(event_name)
                    injected = []
                    if isinstance(data, dict):
                        # 命令 params 同时写两份：
                        #   1. 原键（含嵌套 dict）——图上直接用 {{box_initial_area}} /
                        #      {{box_initial_area.shelf_type}}，新增 HTTP 字段不用改代码
                        #   2. {prefix}_{键} ——多个等待节点并存时避免互相覆盖
                        for k, v in data.items():
                            ctx.set(k, v)
                            injected.append(k)
                            if isinstance(v, dict):
                                for nk, nv in v.items():
                                    dotted = f"{k}.{nk}"
                                    ctx.set(dotted, nv)
                                    injected.append(dotted)
                            if prefix:
                                prefixed = f"{prefix}_{k}"
                                if prefixed != k:
                                    ctx.set(prefixed, v)
                                    injected.append(prefixed)
                        ctx.set(f"{prefix}_payload", data)
                        ctx.set("cmd_payload", data)
                        # 调动作 / 导航节点默认读 {{robot_id}}，不带前缀。
                        # 唤醒命令里带了 robot_id 时同步写一份，否则图上只能手填。
                        if data.get("robot_id"):
                            ctx.set("robot_id", data["robot_id"])
                            injected.append("robot_id")
                    detail = f"，注入变量 {', '.join(injected)}" if injected else ""
                    return True, f"收到信号 '{event_name}'{detail}"
        finally:
            self._mark_waiting(event_name, -1)

    def _parallel_branch_starts(self, node: FlowNode) -> List[str]:
        """并行出边即支路；没有出边时才回退到旧的 params.branches。不存在的 id 丢弃。"""
        starts: List[str] = []
        for e in self._edges_by_source.get(node.id, []):
            if e.target in self._nodes and e.target not in starts:
                starts.append(e.target)
        if starts:
            return starts
        raw = node.params.get("branches") or []
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.strip("[]").replace('"', "").split(",") if part.strip()]
        for b in raw:
            bid = str(b)
            if bid and bid in self._nodes and bid not in starts:
                starts.append(bid)
        return starts

    def _join_mode_of(self, node_id: Optional[str]) -> str:
        node = self._nodes.get(node_id) if node_id else None
        mode = ((node.params or {}).get("join") if node else None) or "all"
        return mode if mode in ("all", "any") else "all"

    def _reachable_from(self, start_id: str) -> set:
        seen = set()
        stack = [start_id]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in self._nodes:
                continue
            seen.add(nid)
            for e in self._edges_by_source.get(nid, []):
                if e.target not in seen:
                    stack.append(e.target)
        return seen

    def _parallel_join_barriers(self, branch_starts: List[str]) -> set:
        """多条支路都能走到、且有支路私有前驱的节点 = 图上的汇合点。"""
        if len(branch_starts) < 2:
            return set()
        reaches = [self._reachable_from(b) for b in branch_starts]
        shared = set.intersection(*reaches) if reaches else set()
        if not shared:
            return set()
        barriers = set()
        for src, edges in self._edges_by_source.items():
            for e in edges:
                if e.target in shared and src not in shared:
                    barriers.add(e.target)
        return barriers

    def _builtin_parallel(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        branches: List[str] = self._parallel_branch_starts(node)
        timeout = node.params.get("timeout")
        barriers = self._parallel_join_barriers(branches)

        results: Dict[str, bool] = {}
        arrived_at: Dict[str, Optional[str]] = {}
        lock = threading.Lock()

        def _run_branch(start_id: str):
            local_trace: List[NodeRecord] = []
            local_arrived: Dict[str, Optional[str]] = {}
            try:
                ok = self.run_chain(
                    start_id, ctx, local_trace,
                    stop_before=barriers, arrived_at=local_arrived,
                )
            except FlowStopped:
                ok = False
            with lock:
                results[start_id] = ok
                if start_id in local_arrived:
                    arrived_at[start_id] = local_arrived[start_id]

        if not branches:
            return False, "parallel 没有分支起点"

        threads = [threading.Thread(target=_run_branch, args=(b,), daemon=True, name=f"flow-parallel-{b}")
                   for b in branches]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)

        hits = [arrived_at[b] for b in branches if results.get(b) and arrived_at.get(b)]
        join_id = hits[0] if hits and len(set(hits)) == 1 else None
        join_mode = self._join_mode_of(join_id)

        if join_id:
            if join_mode == "any":
                success = True
            else:
                success = all(results.get(b, False) for b in branches) and len(hits) == len(branches)
        else:
            success = all(results.get(b, False) for b in branches)

        if join_id and success:
            join_ok = self.run_chain(join_id, ctx, [])
            if not join_ok:
                success = False

        msg = f"并行分支结果: {results}"
        if join_id:
            msg += f"；汇合({join_mode})后继续 {join_id}"
        return success, msg

    def _builtin_sub_flow(self, node: FlowNode, ctx: FlowContext) -> (bool, str):
        if self.flow_loader is None:
            return False, "未提供 flow_loader，无法加载子流程"
        flow_id = node.params.get("flow")
        sub_graph = self.flow_loader(flow_id)
        if not sub_graph:
            return False, f"子流程 '{flow_id}' 未找到"
        sub_engine = FlowEngine(
            sub_graph, handlers=self.handlers, pause_event=self.pause_event,
            stop_event=self.stop_event, on_event=self.on_event,
            flow_loader=self.flow_loader, signal_bus=self.signal_bus,
        )
        result = sub_engine.run(initial_context=ctx.snapshot(), extra=ctx.extra)
        ctx.update(result.context)
        return result.success, result.message


# ──────────────────────────────────────────────────────────────────────────────
# 便捷校验函数（不需要实例化引擎即可用于 GUI/保存前的快速校验）
# ──────────────────────────────────────────────────────────────────────────────

def validate_flow_graph(graph: Dict[str, Any], known_handler_types: Optional[List[str]] = None) -> List[str]:
    """
    独立的流程图静态校验函数，供 flow_api_server 在保存前调用（不需要真正跑一遍流程）。
    """
    engine = FlowEngine(graph, handlers={t: (lambda n, c: True) for t in (known_handler_types or [])})
    return engine.validate()


# ──────────────────────────────────────────────────────────────────────────────
# 内置节点类型的参数表单描述（供图形化编辑器动态生成参数面板用）
# ──────────────────────────────────────────────────────────────────────────────
#
# 每个类型是 {label, category, fields, outputs}：
#   - fields  : 有序数组，每项 {name, type, label, required?, options?, default?}，
#     前端（flow_editor/app.js）按数组顺序渲染表单，type 支持
#     text / number / select（配 options）/ checkbox / json（多行、按 JSON 解析）。
#   - category: 面板里分组显示用的类别名，纯展示，不影响执行。
#   - outputs : 出边可用的分支名列表（"success"/"failure"/"true"/"false"/"default"）。
# 项目自定义节点类型的 schema 由各项目自己在
# `programs/<project>/node_handlers.py` 里提供同样结构的 `NODE_TYPE_SCHEMAS`。

BUILTIN_NODE_TYPE_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "condition": {
        "label": "条件分支",
        "category": "控制流",
        "fields": [
            {"name": "var", "type": "text", "label": "变量名", "required": True,
             "hint": "支持点号路径，如 box_initial_area.shelf_type（来自等待命令注入的嵌套 params）"},
            {"name": "op", "type": "select", "label": "比较符", "required": True,
             "options": list(_CONDITION_OPS.keys())},
            {"name": "value", "type": "text", "label": "比较值"},
        ],
        "outputs": ["true", "false"],
    },
    "set_variable": {
        "label": "设置变量",
        "category": "控制流",
        "fields": [
            {"name": "var", "type": "text", "label": "变量名", "required": True},
            {"name": "op", "type": "select", "label": "运算方式",
             "options": ["set", "add", "sub"], "default": "set",
             "hint": "set=直接赋值；add/sub=在原值上加减，用来在图上做重试计数这类循环"},
            {"name": "value", "type": "text", "label": "值（支持 {{var}} 模板）"},
        ],
        "outputs": ["default"],
    },
    "delay": {
        "label": "延时等待",
        "category": "控制流",
        "fields": [{"name": "seconds", "type": "number", "label": "秒数", "default": 1}],
        "outputs": ["default"],
    },
    "parallel": {
        "label": "并行执行",
        "category": "控制流",
        "fields": [
            {"name": "timeout", "type": "number", "label": "等待各支路结束的超时秒数（可空）"},
        ],
        "outputs": ["default"],
    },
    "wait_for_command": {
        "label": "等待外部命令",
        "category": "控制流",
        "fields": [
            {"name": "event_name", "type": "select", "label": "等待的命令", "required": True,
             "options_source": "commands", "allow_free_text": True,
             "hint": "下拉列出本项目已注册的外部命令。换一条命令会换成该命令的演练默认参数。"},
            {"name": "var_prefix", "type": "text", "label": "参数变量前缀",
             "hint": "命令 params 会原样写入上下文（含嵌套字段，可用 {{box_initial_area.shelf_type}}），"
                     "同时再写一份 {前缀}_{参数名}。留空则前缀用命令名。"},
            {"name": "timeout", "type": "number", "label": "超时秒数（留空=无限等待）"},
            {"name": "dryrun_params", "type": "json", "label": "演练用命令参数",
             "hint": "随上方「等待的命令」切换成该命令的默认入参，之后仍可改。"
                     "结构与 HTTP params 相同（可含 robot_id）；分拣任务放在 jobs 里。"},
        ],
        "outputs": ["success", "failure"],
    },
    "sub_flow": {
        "label": "调用子流程",
        "category": "控制流",
        "fields": [
            {"name": "flow", "type": "select", "label": "子流程", "required": True,
             "options_source": "flows", "allow_free_text": True,
             "hint": "子流程与父流程共享上下文变量；注意它的成功只代表图正常跑完，"
                     "业务结果要靠约定变量传回（例如 install_result）"},
        ],
        "outputs": ["success", "failure"],
    },
    "noop": {
        "label": "占位/汇合点",
        "category": "控制流",
        "fields": [
            {"name": "join", "type": "select", "label": "汇合策略",
             "options": ["all", "any"], "default": "all",
             "hint": "多条并行支路连到本节点时生效。all=全部到达且成功后继续；any=任一支路到达即可。"
                     "不汇合就不要把支路连到这里。"},
        ],
        "outputs": ["default"],
    },
}