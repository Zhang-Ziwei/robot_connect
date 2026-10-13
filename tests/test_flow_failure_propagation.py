"""失败传播与执行记录完整性。

回归两个曾经同时存在的问题：
  1. 只要走到没有下一个节点，run() 就无条件报 completed，节点失败也报成功；
  2. 并行支路和子流程内部的节点不写入主 trace，依赖 trace 查失败的上层跟着失灵。
两者叠加时，并行支路里的设备动作失败会被当成"流程执行完成"。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.flow_engine import FlowEngine


def _run(graph, fail=None, **kwargs):
    log = []

    def record(node, ctx):  # noqa: ARG001
        log.append(node.id)
        return node.id != fail

    engine = FlowEngine(graph, handlers={t: record for t in ("op", "nav")}, **kwargs)
    assert engine.validate() == []
    return engine.run(), log


def _chain():
    return {
        "start": "a",
        "nodes": [{"id": n, "type": "op", "params": {}} for n in ("a", "b", "c")],
        "edges": [
            {"source": "a", "target": "b", "when": "success"},
            {"source": "b", "target": "c", "when": "success"},
        ],
    }


def _parallel():
    """两条支路汇入 join，join 之后还有一个节点。"""
    return {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a1", "b1"]}},
            {"id": "a1", "type": "op", "params": {}},
            {"id": "a2", "type": "op", "params": {}},
            {"id": "b1", "type": "op", "params": {}},
            {"id": "join", "type": "noop", "params": {"join": "all"}},
            {"id": "tail", "type": "op", "params": {}},
        ],
        "edges": [
            {"source": "a1", "target": "a2", "when": "success"},
            {"source": "a2", "target": "join", "when": "success"},
            {"source": "b1", "target": "join", "when": "success"},
            {"source": "join", "target": "tail", "when": "default"},
        ],
    }


def test_failed_node_without_failure_edge_reports_failed():
    result, log = _run(_chain(), fail="b")
    assert not result.success
    assert result.status == "failed"
    assert "b" in result.message, result.message
    assert log == ["a", "b"]
    assert [r.node_id for r in result.trace if r.status == "failure"] == ["b"]


def test_failure_edge_keeps_flow_running():
    graph = _chain()
    graph["nodes"].append({"id": "rescue", "type": "op", "params": {}})
    graph["edges"].append({"source": "b", "target": "rescue", "when": "failure"})
    result, log = _run(graph, fail="b")
    assert result.success, result.message
    assert result.status == "completed"
    assert log == ["a", "b", "rescue"]


def test_parallel_branch_failure_propagates_and_records_trace():
    result, log = _run(_parallel(), fail="a2")
    assert not result.success
    assert result.status == "failed"
    assert "tail" not in log
    # 支路内部的节点必须出现在主 trace 里，否则上层查不到真正失败的那个。
    recorded = {r.node_id for r in result.trace}
    assert {"a1", "a2", "b1"} <= recorded, recorded
    assert "a2" in {r.node_id for r in result.trace if r.status == "failure"}


def test_join_chain_failure_propagates():
    """支路全部成功、汇合之后才失败，同样不能报完成。"""
    result, log = _run(_parallel(), fail="tail")
    assert not result.success
    assert result.status == "failed"
    assert log.count("tail") == 1
    assert "tail" in {r.node_id for r in result.trace if r.status == "failure"}


def test_branch_failure_edge_to_join_counts_as_arrival():
    """支路自己处理了失败并接到汇合点，算抵达，不拖垮整张图。"""
    graph = _parallel()
    graph["edges"].append({"source": "a2", "target": "join", "when": "failure"})
    result, log = _run(graph, fail="a2")
    assert result.success, result.message
    assert log.count("tail") == 1


def test_successful_parallel_records_every_node():
    result, _ = _run(_parallel())
    assert result.success, result.message
    assert {r.node_id for r in result.trace} == {"p", "a1", "a2", "b1", "join", "tail"}


def test_condition_false_without_edge_ends_normally():
    """condition 的 false 是分支结果不是故障，没接线就是正常收尾。"""
    graph = {
        "start": "chk",
        "nodes": [
            {"id": "chk", "type": "condition",
             "params": {"var": "missing", "op": "truthy"}},
            {"id": "yes", "type": "op", "params": {}},
        ],
        "edges": [{"source": "chk", "target": "yes", "when": "true"}],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert result.status == "completed"
    assert log == []


def _branch_with_condition(false_target=None):
    """把支路 a 的第二个节点换成 condition，可选把 false 分支接到汇合点。"""
    graph = _parallel()
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "a2"]
    graph["nodes"].append({"id": "a2", "type": "condition",
                           "params": {"var": "missing", "op": "truthy"}})
    graph["edges"] = [e for e in graph["edges"] if e["source"] != "a2"]
    graph["edges"].append({"source": "a2", "target": "join", "when": "true"})
    if false_target:
        graph["edges"].append({"source": "a2", "target": "false_target", "when": "false"})
        graph["edges"][-1]["target"] = false_target
    return graph


def test_branch_that_never_reaches_join_reports_failed():
    """join=all 要求每条支路都抵达汇合点，没抵达就不能当成跑完。

    支路里的 condition 判否后悬空结束，汇合之后的收尾节点永远不会执行；
    这种图是配错了，报失败才能暴露出来，静默 completed 会掩盖问题。
    """
    result, log = _run(_branch_with_condition())
    assert "a2" in {r.node_id for r in result.trace}
    assert "tail" not in log
    assert not result.success
    assert result.status == "failed"


def test_conditional_branch_reaching_join_completes():
    """同一张图把 false 分支也接到汇合点，就是正常的"有条件跳过"。"""
    result, log = _run(_branch_with_condition(false_target="join"))
    assert result.success, result.message
    assert result.status == "completed"
    assert log.count("tail") == 1


def test_sub_flow_trace_merged_into_parent():
    sub = {
        "start": "s1",
        "nodes": [{"id": "s1", "type": "op", "params": {}},
                  {"id": "s2", "type": "op", "params": {}}],
        "edges": [{"source": "s1", "target": "s2", "when": "success"}],
    }
    graph = {
        "start": "call",
        "nodes": [{"id": "call", "type": "sub_flow", "params": {"flow": "sub"}}],
        "edges": [],
    }
    result, log = _run(graph, flow_loader=lambda fid: sub if fid == "sub" else None)
    assert result.success, result.message
    assert log == ["s1", "s2"]
    assert {"s1", "s2"} <= {r.node_id for r in result.trace}


def test_sub_flow_failure_propagates_with_trace():
    sub = {
        "start": "s1",
        "nodes": [{"id": "s1", "type": "op", "params": {}},
                  {"id": "s2", "type": "op", "params": {}}],
        "edges": [{"source": "s1", "target": "s2", "when": "success"}],
    }
    graph = {
        "start": "call",
        "nodes": [{"id": "call", "type": "sub_flow", "params": {"flow": "sub"}},
                  {"id": "after", "type": "op", "params": {}}],
        "edges": [{"source": "call", "target": "after", "when": "success"}],
    }
    result, log = _run(graph, fail="s2",
                       flow_loader=lambda fid: sub if fid == "sub" else None)
    assert not result.success
    assert "after" not in log
    assert "s2" in {r.node_id for r in result.trace if r.status == "failure"}
