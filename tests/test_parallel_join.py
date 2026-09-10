"""并行汇合：多条支路汇入同一节点后，后继只执行一次。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.flow_engine import FlowEngine


def _handlers(log):
    def record(node, ctx):  # noqa: ARG001
        log.append(node.id)
        return True
    return {
        "send_operation": record,
        "navigate": record,
        "plc_action": record,
    }


def _run(graph):
    log = []
    engine = FlowEngine(graph, handlers=_handlers(log))
    result = engine.run()
    return result, log


def test_join_all_runs_plc_once():
    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a_op", "b_op"]}},
            {"id": "a_op", "type": "send_operation", "params": {}},
            {"id": "a_nav1", "type": "navigate", "params": {}},
            {"id": "a_nav2", "type": "navigate", "params": {}},
            {"id": "b_op", "type": "send_operation", "params": {}},
            {"id": "join", "type": "noop", "params": {"join": "all"}},
            {"id": "plc", "type": "plc_action", "params": {}},
        ],
        "edges": [
            {"source": "a_op", "target": "a_nav1", "when": "success"},
            {"source": "a_nav1", "target": "a_nav2", "when": "success"},
            {"source": "a_nav2", "target": "join", "when": "success"},
            {"source": "b_op", "target": "join", "when": "success"},
            {"source": "join", "target": "plc", "when": "default"},
        ],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert log.count("plc") == 1, log
    assert log.count("a_op") == 1
    assert log.count("b_op") == 1
    assert log.count("a_nav1") == 1
    assert log.count("a_nav2") == 1
    assert log.index("plc") > log.index("a_nav2")
    assert log.index("plc") > log.index("b_op")


def test_branch_edges_are_threads():
    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {}},
            {"id": "a", "type": "send_operation", "params": {}},
            {"id": "b", "type": "send_operation", "params": {}},
            {"id": "join", "type": "noop", "params": {"join": "all"}},
            {"id": "plc", "type": "plc_action", "params": {}},
        ],
        "edges": [
            {"source": "p", "target": "a", "when": "default"},
            {"source": "p", "target": "b", "when": "branch"},
            {"source": "a", "target": "join", "when": "success"},
            {"source": "b", "target": "join", "when": "success"},
            {"source": "join", "target": "plc", "when": "default"},
        ],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert sorted(x for x in log if x != "plc") == ["a", "b"]
    assert log.count("plc") == 1


def test_independent_branches_no_join():
    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a", "b"]}},
            {"id": "a", "type": "send_operation", "params": {}},
            {"id": "b", "type": "send_operation", "params": {}},
        ],
        "edges": [],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert sorted(log) == ["a", "b"]


def test_stale_branch_id_ignored():
    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a", "deleted"]}},
            {"id": "a", "type": "send_operation", "params": {}},
        ],
        "edges": [],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert log == ["a"]


def test_join_any_continues_when_one_arrives():
    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a", "b"]}},
            {"id": "a", "type": "send_operation", "params": {}},
            {"id": "b", "type": "send_operation", "params": {}},
            {"id": "join", "type": "noop", "params": {"join": "any"}},
            {"id": "plc", "type": "plc_action", "params": {}},
        ],
        "edges": [
            {"source": "a", "target": "join", "when": "success"},
            {"source": "join", "target": "plc", "when": "default"},
        ],
    }
    result, log = _run(graph)
    assert result.success, result.message
    assert log.count("plc") == 1
    assert "a" in log and "b" in log


def test_join_any_one_branch_fails():
    log = []

    def record(node, ctx):  # noqa: ARG001
        log.append(node.id)
        return node.id != "b"

    graph = {
        "start": "p",
        "nodes": [
            {"id": "p", "type": "parallel", "params": {"branches": ["a", "b"]}},
            {"id": "a", "type": "send_operation", "params": {}},
            {"id": "b", "type": "send_operation", "params": {}},
            {"id": "join", "type": "noop", "params": {"join": "any"}},
            {"id": "plc", "type": "plc_action", "params": {}},
        ],
        "edges": [
            {"source": "a", "target": "join", "when": "success"},
            {"source": "b", "target": "join", "when": "success"},
            {"source": "join", "target": "plc", "when": "default"},
        ],
    }
    engine = FlowEngine(graph, handlers={
        "send_operation": record, "plc_action": record,
    })
    result = engine.run()
    assert result.success, result.message
    assert log.count("plc") == 1
    assert "a" in log and "b" in log


if __name__ == "__main__":
    test_join_all_runs_plc_once()
    test_branch_edges_are_threads()
    test_independent_branches_no_join()
    test_stale_branch_id_ignored()
    test_join_any_continues_when_one_arrives()
    test_join_any_one_branch_fails()
    print("ok")
