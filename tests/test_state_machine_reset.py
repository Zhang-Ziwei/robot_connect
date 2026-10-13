"""两个任务状态机的重置行为，以及它们与通用命令流程的接口对齐。

起因：RESET_SYSTEM 在用 ParallelTaskStateMachine 的 7 个项目上报
「'ParallelTaskStateMachine' object has no attribute 'reset'」。
dispatcher_base.handle_reset_system 是所有项目共用的，它调 task_state_machine.reset()，
而并行状态机当时只有内部的 _init_fields()。异常被 try/except 吞成 False，
重置做了一半就停下（机器人已断开，配置却没重载）。
"""

import ast
import inspect
from pathlib import Path

import pytest

from core.task_state_machine import (
    ParallelTaskStateMachine,
    TaskStateMachine,
    TaskStatus,
)

ROBOTS = ["robot_a", "robot_b"]


def _running(**kw):
    tsm = ParallelTaskStateMachine(ROBOTS)
    tsm.start_task("cmd-1")
    return tsm


def test_parallel_reset_exists_and_clears_running():
    """原始故障点：方法得存在，且能把运行中的任务清掉。"""
    tsm = _running()
    assert tsm.status == TaskStatus.RUNNING

    tsm.reset()

    assert tsm.status == TaskStatus.NOT_STARTED
    assert tsm.task_id is None
    assert tsm.is_busy() is False


def test_parallel_reset_clears_error_and_per_robot_progress():
    """错误信息和每台机器人的步骤、done 标志都要清干净。

    只清任务级状态是不够的：留着 done=True 的话，下一轮任务一旦有机器人
    mark_robot_done，all(done) 立刻成立，整体会被判成 COMPLETED。
    """
    tsm = _running()
    tsm.update_step("robot_a", "分液")
    tsm.mark_robot_done("robot_a")
    tsm.set_error("分液超时", robot_id="robot_b")

    tsm.reset()

    assert tsm.status == TaskStatus.NOT_STARTED
    assert tsm.error_message is None
    assert tsm.start_time is None and tsm.end_time is None
    state = tsm.get_state()
    for rid in ROBOTS:
        assert state["robots"][rid]["current_step"] == "空闲"
        assert state["robots"][rid]["completed_steps"] == []
        assert state["robots"][rid]["done"] is False


def test_parallel_reset_works_from_any_state():
    """reset 不看当前状态，这是它和 cancel_task 的区别。

    cancel_task 只在 RUNNING 时生效（故意不覆盖已完成/已出错的任务），
    所以它不能充当 RESET_SYSTEM 的重置手段。
    """
    tsm = ParallelTaskStateMachine(ROBOTS)
    tsm.reset()                       # 全新状态机上调用也不该抛
    assert tsm.status == TaskStatus.NOT_STARTED

    tsm.start_task("cmd-2")
    tsm.set_error("炸了")
    assert tsm.status == TaskStatus.ERROR
    tsm.cancel_task()                 # 已是 ERROR，取消不生效
    assert tsm.status == TaskStatus.ERROR
    tsm.reset()                       # 重置不受状态限制
    assert tsm.status == TaskStatus.NOT_STARTED


def test_reset_allows_a_fresh_task_afterwards():
    """重置后能正常接下一轮任务。"""
    tsm = _running()
    tsm.mark_robot_done("robot_a")
    tsm.mark_robot_done("robot_b")
    assert tsm.status == TaskStatus.COMPLETED

    tsm.reset()
    tsm.start_task("cmd-3")

    assert tsm.status == TaskStatus.RUNNING
    assert tsm.task_id == "cmd-3"
    assert tsm.is_busy() is True


def test_single_robot_reset_still_clears_everything():
    """单机状态机的 reset 没动，顺带钉住，避免两边行为分叉。"""
    tsm = TaskStateMachine()
    tsm.start_task("cmd-4", robot_id="robot_a")
    tsm.set_error("出错了")

    tsm.reset()

    assert tsm.status == TaskStatus.NOT_STARTED
    assert tsm.task_id is None
    assert tsm.error_message is None
    assert tsm.is_busy() is False


def _calls_made_by_generic_dispatcher():
    """从 dispatcher_base 源码里取出它对 self.task_state_machine 的所有调用。

    连位置参数个数一起取：本次两个故障里，reset 是「方法不存在」，
    get_state 是「方法在但不收那个参数」，只查方法名抓不到后者。
    写死一份清单的话，以后通用流程新调一个方法这里不会有反应——而那正是
    这两个故障的形态。改成从源码提取，新增调用会自动纳入检查。
    """
    src = Path(__file__).resolve().parents[1] / "handlers" / "dispatcher_base.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        owner = fn.value
        if (isinstance(owner, ast.Attribute) and owner.attr == "task_state_machine"
                and isinstance(owner.value, ast.Name) and owner.value.id == "self"):
            calls.add((fn.attr, len(node.args)))
    assert calls, "没能从 dispatcher_base 里提取到状态机调用，提取逻辑要跟着代码改"
    return sorted(calls)


@pytest.mark.parametrize("machine", [
    TaskStateMachine(),
    ParallelTaskStateMachine(ROBOTS),
], ids=["single", "parallel"])
def test_both_machines_accept_generic_dispatcher_calls(machine):
    """通用命令流程发出的每一种调用，两个状态机都得接得住。

    所有项目共用 dispatcher_base，状态机却有两种实现且不同源（并行版不继承
    单机版），差一个方法或差一个参数，就是一条命令在一半项目上直接报错。
    这里只检查调用能否成立（签名层面），不校验返回内容。
    """
    problems = []
    for name, argc in _calls_made_by_generic_dispatcher():
        fn = getattr(machine, name, None)
        if not callable(fn):
            problems.append(f"{name} 不存在")
            continue
        try:
            inspect.signature(fn).bind(*(["probe"] * argc))
        except TypeError as exc:
            problems.append(f"{name} 收不下 {argc} 个位置参数: {exc}")
    assert not problems, f"{type(machine).__name__}: {problems}"


@pytest.mark.parametrize("machine", [
    TaskStateMachine(),
    ParallelTaskStateMachine(ROBOTS),
], ids=["single", "parallel"])
def test_query_unknown_task_id_reports_not_found(machine):
    """按不认识的命令 id 查询时，两边都要给出「未找到」而不是抛异常。

    dispatcher_base.handle_get_task_state 靠 status == "未找到" 来决定返回
    TASK_NOT_FOUND，并从 current_task_id 取当前任务，所以这几个键不能少。
    """
    state = machine.get_state("不存在的-cmd-id")

    assert state["status"] == "未找到"
    assert state["cmd_id"] == "不存在的-cmd-id"
    assert "current_task_id" in state
    assert state.get("message")


@pytest.mark.parametrize("machine_factory", [
    lambda: TaskStateMachine(),
    lambda: ParallelTaskStateMachine(ROBOTS),
], ids=["single", "parallel"])
def test_query_matching_task_id_returns_real_state(machine_factory):
    """命令 id 对得上时要返回真实状态，不能误判成未找到。"""
    machine = machine_factory()
    machine.start_task("cmd-9")

    state = machine.get_state("cmd-9")

    assert state["status"] != "未找到"
    assert state["task_id"] == "cmd-9"


def test_parallel_machine_has_no_accidental_private_only_api():
    """reset 必须是公开方法，_init_fields 只是它的实现细节。

    故障当时并行状态机确实有归零逻辑（start_task 内部就在用），只是名字是私有的，
    外部调用方看不见也不该去调。
    """
    sig = inspect.signature(ParallelTaskStateMachine.reset)
    assert list(sig.parameters) == ["self"], "reset 不该要求额外参数"
