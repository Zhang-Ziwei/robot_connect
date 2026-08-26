#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟"部署在机器人上的 chem_project ROS 节点"

本程序同时模拟两种 ROS 通信协议，均使用相同的任务执行逻辑：

━━ ROS Service (/chem_project_service) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
同步请求/响应。srv 文件：

    # Request
    navi_types/RobotTaskTypes robot_task_types
    string task
    string area
    string extra_params
    ---
    # Response
    bool   success
    string error_msg
    string return_params

main.py 调用路径：robot.send_service_request_task(ROSService.CHEM_PROJECT_SERVICE, ...)

━━ ROS Action (/robot_task/*) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
异步 topic 流（actionlib 协议）。.action 文件：

    # Goal
    navi_types/RobotTaskTypes robot_task_types
    string task
    string area
    string extra_params
    ---
    # Result
    bool   success
    string error_msg
    string return_params
    ---
    # Feedback
    string status
    string current_params

main.py 调用路径：hardware.task_utils.send_task_action(robot, task=...)

━━ 架构 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本程序的正确角色是作为 ROS 节点，机器人自带 rosbridge 负责向 main.py 桥接：

    本 mock（ROS 节点：service server + action server）
           │
           │  ROS 原生 topic / service
           ▼
    机器人自带 rosbridge（把 ROS ↔ WebSocket 双向桥接）
           │
           │  ws://机器人IP:9090
           ▼
    main.py / robot_connect

━━ 运行模式 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- **ros-node**（默认）：rospy + actionlib，需 source ROS 环境。
- **bridge-client**（备用）：无 ROS 时通过 rosbridge WebSocket 协议模拟。
- **embedded-server**（历史）：本程序自己伪装成 rosbridge server，main.py 直连。

━━ 测试用 task 分发 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    task="success_fast"       → 立即成功，无 feedback（service/action 通用）
    task="success_with_steps" → 3 条 feedback 后成功（action 才有 feedback）
    task="fail"               → 立即失败，error_msg 带诊断
    task="fail_with_steps"    → 2 条 feedback 后失败
    task="long"               → 运行 30s，用于测试 cancel / 超时
    其他 task                  → 等同 success_with_steps
"""

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple

try:
    import websockets
except ImportError:
    print("请先安装 websockets: pip install websockets")
    raise SystemExit(1)


# ==================== GoalStatus（与 actionlib_msgs/GoalStatus 对齐） ====================

class GoalStatus:
    PENDING    = 0
    ACTIVE     = 1
    PREEMPTED  = 2
    SUCCEEDED  = 3
    ABORTED    = 4
    REJECTED   = 5
    PREEMPTING = 6
    RECALLING  = 7
    RECALLED   = 8
    LOST       = 9
    NAMES = {
        0: "PENDING", 1: "ACTIVE", 2: "PREEMPTED", 3: "SUCCEEDED",
        4: "ABORTED", 5: "REJECTED", 6: "PREEMPTING", 7: "RECALLING",
        8: "RECALLED", 9: "LOST",
    }


# ==================== 配置 ====================

@dataclass
class ServerConfig:
    mode: str = "ros-node"          # ros-node | bridge-client | embedded-server
    rosbridge_url: str = "ws://127.0.0.1:9090"
    reconnect_interval: float = 3.0
    host: str = "0.0.0.0"
    port: int = 19090

    # Action 端点（actionlib topic 协议）
    action_base: str = "/robot_task"
    action_pkg: str = "navi_types"
    action_name: str = "RobotAction"

    # Service 端点（ROS Service 同步协议）
    service_name: str = "/robot_task"

    default_feedback_steps: int = 3
    default_step_interval: float = 1.0
    long_task_duration: float = 30.0
    status_rate_hz: float = 5.0
    verbose: bool = True


cfg = ServerConfig()


# ==================== 活跃 goal 跟踪（action 专用） ====================

@dataclass
class ActiveGoal:
    goal_id: str
    goal_fields: dict
    started_at: float
    status: int = GoalStatus.ACTIVE
    cancel_requested: bool = False
    run_task: Optional[asyncio.Task] = None
    origin_ws: Optional["websockets.WebSocketServerProtocol"] = None


active_goals: Dict[str, ActiveGoal] = {}
topic_subscribers: Dict[str, Set["websockets.WebSocketServerProtocol"]] = {}
bridge_ws: Optional["websockets.WebSocketClientProtocol"] = None
status_publisher_task: Optional[asyncio.Task] = None


# ==================== 工具函数 ====================

def log(*args):
    if cfg.verbose:
        print(*args, flush=True)


def now_stamp() -> dict:
    t = time.time()
    secs = int(t)
    return {"secs": secs, "nsecs": int((t - secs) * 1e9)}


# ── Action topic 名 ──
def topic_goal() -> str:     return f"{cfg.action_base}/goal"
def topic_cancel() -> str:   return f"{cfg.action_base}/cancel"
def topic_status() -> str:   return f"{cfg.action_base}/status"
def topic_feedback() -> str: return f"{cfg.action_base}/feedback"
def topic_result() -> str:   return f"{cfg.action_base}/result"

# ── Action/Service 消息类型 ──
def type_action_goal() -> str:     return f"{cfg.action_pkg}/{cfg.action_name}ActionGoal"
def type_action_feedback() -> str: return f"{cfg.action_pkg}/{cfg.action_name}ActionFeedback"
def type_action_result() -> str:   return f"{cfg.action_pkg}/{cfg.action_name}ActionResult"
def type_service() -> str:         return f"{cfg.action_pkg}/{cfg.action_name}Service"


def topic_type(topic: str) -> str:
    m = {
        topic_goal():     type_action_goal(),
        topic_cancel():   "actionlib_msgs/GoalID",
        topic_status():   "actionlib_msgs/GoalStatusArray",
        topic_feedback(): type_action_feedback(),
        topic_result():   type_action_result(),
    }
    return m.get(topic, "")


# ── 导航 Action topic 终结点（actionlib 协议） ─────────────────────────────────
NAV_ACTION_BASE     = "/zj_humanoid/navigation/navigation"
NAV_ACTION_GOAL     = f"{NAV_ACTION_BASE}/goal"
NAV_ACTION_FEEDBACK = f"{NAV_ACTION_BASE}/feedback"
NAV_ACTION_RESULT   = f"{NAV_ACTION_BASE}/result"
NAV_ACTION_CANCEL   = f"{NAV_ACTION_BASE}/cancel"
NAV_ACTION_STATUS   = f"{NAV_ACTION_BASE}/status"

# NavigationState.value（navigation/NavigationState.msg）
NAV_STATE_RUNNING = 2   # Running
NAV_STATE_ARRIVED = 3   # Arrived（成功）
NAV_STATE_FAILED  = 7   # Failed

# actionlib_msgs/GoalStatus
ACTIONLIB_ACTIVE    = 1
ACTIONLIB_SUCCEEDED = 3
ACTIONLIB_ABORTED   = 4

# 导航模拟时长（秒）
NAV_SIMULATE_DELAY = 3.0


def _make_stamp() -> dict:
    t = time.time()
    return {"secs": int(t), "nsecs": int((t % 1) * 1e9)}


def _make_nav_feedback_msg(goal_id: str, nav_state_value: int) -> dict:
    """构造 navigation/NavigationActionFeedback 消息"""
    stamp = _make_stamp()
    return {
        "header": {"seq": 0, "stamp": stamp, "frame_id": ""},
        "status": {
            "goal_id": {"stamp": stamp, "id": goal_id},
            "status": ACTIONLIB_ACTIVE,
            "text": "",
        },
        "feedback": {
            "state": {"value": nav_state_value},
            "faults": [],
        },
    }


def _make_nav_result_msg(goal_id: str, succeeded: bool = True) -> dict:
    """构造 navigation/NavigationActionResult 消息"""
    stamp = _make_stamp()
    nav_state = NAV_STATE_ARRIVED if succeeded else NAV_STATE_FAILED
    actionlib_status = ACTIONLIB_SUCCEEDED if succeeded else ACTIONLIB_ABORTED
    return {
        "header": {"seq": 0, "stamp": stamp, "frame_id": ""},
        "status": {
            "goal_id": {"stamp": stamp, "id": goal_id},
            "status": actionlib_status,
            "text": "Succeeded" if succeeded else "Failed",
        },
        "result": {
            "state": {"value": nav_state},
            "distance_deviation": 0.0,
            "heading_deviation":  0.0,
            "causes": [],
            "duration": {"secs": int(NAV_SIMULATE_DELAY), "nsecs": 0},
        },
    }


async def _simulate_navigation_action(ws, goal_id: str):
    """
    模拟 actionlib 导航全流程：
        0.3s → feedback(RUNNING)
        每秒  → feedback(RUNNING)
        到时  → result(ARRIVED)

    推送方式：向所有订阅了对应 topic 的客户端广播（bridge-client 模式广播给 rosbridge，
    embedded-server 模式直接推给订阅者）。
    """
    async def _push(topic: str, msg: dict):
        await broadcast_to_topic(topic, msg)

    await asyncio.sleep(0.3)
    await _push(NAV_ACTION_FEEDBACK, _make_nav_feedback_msg(goal_id, NAV_STATE_RUNNING))
    log(f"📶 导航 feedback → RUNNING  goal_id={goal_id[:16]}...")

    elapsed = 0.3
    while elapsed < NAV_SIMULATE_DELAY - 0.5:
        await asyncio.sleep(1.0)
        elapsed += 1.0
        await _push(NAV_ACTION_FEEDBACK, _make_nav_feedback_msg(goal_id, NAV_STATE_RUNNING))

    await asyncio.sleep(max(0.0, NAV_SIMULATE_DELAY - elapsed))
    await _push(NAV_ACTION_RESULT, _make_nav_result_msg(goal_id, succeeded=True))
    log(f"✅ 导航 result   → ARRIVED  goal_id={goal_id[:16]}...")


async def broadcast_to_topic(topic: str, payload: dict):
    """
    发布一条 rosbridge topic 消息（bridge-client / embedded-server 模式）。
    ros-node 模式下 rospy 直接发布，不走本函数。
    """
    if cfg.mode == "bridge-client":
        if bridge_ws is None:
            return
        try:
            await bridge_ws.send(json.dumps(
                {"op": "publish", "topic": topic, "msg": payload},
                ensure_ascii=False,
            ))
        except Exception as e:
            log(f"   ⚠ publish 失败 topic={topic}: {type(e).__name__}: {e}")
        return

    subs = list(topic_subscribers.get(topic, set()))
    if not subs:
        return
    raw = json.dumps({"op": "publish", "topic": topic, "msg": payload})
    stale = []
    for ws in subs:
        try:
            await ws.send(raw)
        except Exception:
            stale.append(ws)
    for ws in stale:
        topic_subscribers.get(topic, set()).discard(ws)


# ==================== 公共任务执行逻辑 ====================
# 两种协议（service / action）共用同一套 task 分发逻辑。
# 区别：service 是同步的（无 feedback），action 是异步的（有 feedback 流）。

async def _execute_task(
    task_fields: dict,
    feedback_fn: Optional[Callable] = None,
    cancel_fn:   Optional[Callable] = None,
) -> Tuple[bool, str, str]:
    """
    公共任务调度协程，返回 (success, error_msg, return_params)。

    参数：
        task_fields  : {"task", "area", "extra_params", "robot_task_types"}
        feedback_fn  : async callable(status_text, current_params) — 仅 action 传入；
                       service 调用时传 None（跳过 feedback）。
        cancel_fn    : callable() -> bool — 检测是否被取消（仅 action 传入）。
    """
    task_name = (task_fields.get("task") or "").strip()
    area      = task_fields.get("area", "")
    extra     = task_fields.get("extra_params", "")

    async def _fb(status_text: str, current_params: str = ""):
        if feedback_fn:
            await feedback_fn(status_text, current_params)
        else:
            log(f"   [svc fb] {status_text} | {current_params}")

    def _cancelled() -> bool:
        return cancel_fn() if cancel_fn else False

    try:
        if task_name == "success_fast":
            return True, "", json.dumps({"area": area, "note": "fast"})

        if task_name == "fail":
            return False, f"mock task 'fail' 模拟业务错误 (area={area})", ""

        if task_name == "fail_with_steps":
            for i in range(2):
                if _cancelled():
                    return False, "cancelled by client", ""
                await _fb(f"step_{i+1}_of_2", f"i={i+1}")
                await asyncio.sleep(cfg.default_step_interval)
            return False, "mock task 'fail_with_steps' 在第 2 步失败", ""

        if task_name == "long":
            start = time.time()
            step = 0
            while time.time() - start < cfg.long_task_duration:
                if _cancelled():
                    return False, "cancelled by client", ""
                step += 1
                elapsed = time.time() - start
                await _fb(f"long_running_step_{step}", f"elapsed={elapsed:.1f}s")
                await asyncio.sleep(cfg.default_step_interval)
            return True, "", json.dumps({"total_steps": step})

        # 默认：success_with_steps
        for i in range(cfg.default_feedback_steps):
            if _cancelled():
                return False, "cancelled by client", ""
            await _fb(
                f"step_{i+1}_of_{cfg.default_feedback_steps}",
                f"progress={(i+1)/cfg.default_feedback_steps:.2f}",
            )
            await asyncio.sleep(cfg.default_step_interval)

        if _cancelled():
            return False, "cancelled by client", ""

        return True, "", json.dumps({
            "echo_task":         task_name,
            "echo_area":         area,
            "echo_extra_params": extra,
            "steps":             cfg.default_feedback_steps,
        })

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"   ❌ 任务执行异常: {type(e).__name__}: {e}")
        return False, f"{type(e).__name__}: {e}", ""


# ==================== Action 协议（topic 流） ====================

async def send_status_array():
    status_list = [
        {
            "goal_id": {"stamp": now_stamp(), "id": g.goal_id},
            "status":  g.status,
            "text":    GoalStatus.NAMES.get(g.status, "UNKNOWN"),
        }
        for g in active_goals.values()
    ]
    await broadcast_to_topic(
        topic_status(),
        {"header": {"stamp": now_stamp(), "frame_id": ""}, "status_list": status_list},
    )


async def _send_action_feedback(goal: ActiveGoal, status_text: str, current_params: str = ""):
    payload = {
        "header": {"stamp": now_stamp(), "frame_id": ""},
        "status": {
            "goal_id": {"stamp": now_stamp(), "id": goal.goal_id},
            "status":  goal.status,
            "text":    GoalStatus.NAMES.get(goal.status, "UNKNOWN"),
        },
        "feedback": {"status": status_text, "current_params": current_params},
    }
    await broadcast_to_topic(topic_feedback(), payload)
    log(f"   📶 action feedback [{goal.goal_id}] status={status_text!r}")


async def _send_action_result(goal: ActiveGoal, success: bool,
                              error_msg: str = "", return_params: str = ""):
    payload = {
        "header": {"stamp": now_stamp(), "frame_id": ""},
        "status": {
            "goal_id": {"stamp": now_stamp(), "id": goal.goal_id},
            "status":  goal.status,
            "text":    GoalStatus.NAMES.get(goal.status, "UNKNOWN"),
        },
        "result": {
            "success":       success,
            "error_msg":     error_msg,
            "return_params": return_params,
        },
    }
    await broadcast_to_topic(topic_result(), payload)
    log(f"   🏁 action result [{goal.goal_id}] success={success} "
        f"error_msg={error_msg!r} return_params={return_params!r}")


async def _finish_action(goal: ActiveGoal, success: bool,
                         error_msg: str = "", return_params: str = ""):
    if goal.status not in (GoalStatus.PREEMPTED, GoalStatus.ABORTED, GoalStatus.RECALLED):
        goal.status = GoalStatus.SUCCEEDED if success else GoalStatus.ABORTED
    await send_status_array()
    await _send_action_feedback(
        goal,
        status_text="FINISHED" if success else "FAILED",
        current_params="",
    )
    await _send_action_result(goal, success, error_msg, return_params)
    await asyncio.sleep(0.2)
    active_goals.pop(goal.goal_id, None)
    await send_status_array()


async def execute_action_goal(goal: ActiveGoal):
    log(f"\n🚀 [Action] 开始执行 goal_id={goal.goal_id}")
    log(f"   task={goal.goal_fields.get('task')!r} "
        f"area={goal.goal_fields.get('area')!r}")
    await send_status_array()

    try:
        async def fb(status_text: str, current_params: str = ""):
            await _send_action_feedback(goal, status_text, current_params)

        success, error_msg, return_params = await _execute_task(
            goal.goal_fields,
            feedback_fn=fb,
            cancel_fn=lambda: goal.cancel_requested,
        )

        if goal.cancel_requested:
            goal.status = GoalStatus.PREEMPTED
            await _finish_action(goal, False, "cancelled by client")
        else:
            if not success:
                goal.status = GoalStatus.ABORTED
            await _finish_action(goal, success, error_msg, return_params)

    except asyncio.CancelledError:
        log(f"   ⚠ goal {goal.goal_id} 协程被取消")
        goal.status = GoalStatus.RECALLED
        await _finish_action(goal, False, "server side coroutine cancelled")
        raise
    except Exception as e:
        log(f"   ❌ goal {goal.goal_id} 执行异常: {type(e).__name__}: {e}")
        goal.status = GoalStatus.ABORTED
        await _finish_action(goal, False, f"{type(e).__name__}: {e}")


async def handle_goal(ws, msg: dict):
    """处理 <base>/goal topic 上的 XxxActionGoal 消息（bridge-client / embedded-server）"""
    goal_id_field = (msg.get("goal_id") or {})
    goal_id = goal_id_field.get("id") or f"auto-{uuid.uuid4().hex[:8]}"
    inner   = msg.get("goal") or {}

    log(f"\n📥 [Action] 收到 Goal  goal_id={goal_id}")
    log(f"   task={inner.get('task')!r}  area={inner.get('area')!r}")

    goal = ActiveGoal(
        goal_id=goal_id,
        goal_fields=inner,
        started_at=time.time(),
        status=GoalStatus.ACTIVE,
        origin_ws=ws,
    )
    active_goals[goal_id] = goal
    goal.run_task = asyncio.create_task(execute_action_goal(goal))


async def handle_cancel(ws, msg: dict):
    """处理 <base>/cancel topic 上的 GoalID 消息"""
    goal_id = msg.get("id") or ""
    log(f"\n🛑 [Action] 收到 Cancel  goal_id={goal_id!r}")
    if not goal_id:
        for g in list(active_goals.values()):
            g.cancel_requested = True
        return
    goal = active_goals.get(goal_id)
    if goal is None:
        log(f"   ⚠ 未找到活跃 goal: {goal_id}")
        return
    goal.cancel_requested = True
    goal.status = GoalStatus.PREEMPTING
    await send_status_array()


# ==================== Service 协议（同步请求/响应） ====================

async def handle_call_service_task(ws, message: dict):
    """
    处理对 /chem_project_service 的 call_service 请求。

    支持两种 args 格式（兼容 send_service_request_task 和完整 srv）：
        {"task": ..., "area": ..., "extra_params": ...}
        {"robot_task_types": ..., "task": ..., "area": ..., "extra_params": ...}
    """
    service = message.get("service", cfg.service_name)
    req_id  = message.get("id", "")
    args    = message.get("args", {}) or {}

    task_fields = {
        "task":             args.get("task", ""),
        "area":             args.get("area", ""),
        "extra_params":     args.get("extra_params", ""),
        "robot_task_types": args.get("robot_task_types"),
    }

    log(f"\n📥 [Service] 收到 call_service  service={service}")
    log(f"   task={task_fields['task']!r}  area={task_fields['area']!r}")

    # service 不发 feedback；_execute_task 的 feedback_fn=None 时只打日志
    success, error_msg, return_params = await _execute_task(task_fields)

    log(f"   🏁 [Service] 响应  success={success}  error_msg={error_msg!r}")

    response = {
        "op":      "service_response",
        "id":      req_id,
        "service": service,
        "values":  {
            "success":       success,
            "error_msg":     error_msg,
            "return_params": return_params,
        },
        "result": success,
    }
    try:
        await ws.send(json.dumps(response, ensure_ascii=False))
    except Exception as e:
        log(f"   ⚠ service_response 发送失败: {type(e).__name__}: {e}")


# ==================== rosbridge op 路由（bridge-client / embedded-server 共用） ====================

async def handle_subscribe(ws, message: dict):
    topic = message.get("topic", "")
    if not topic:
        return
    topic_subscribers.setdefault(topic, set()).add(ws)
    log(f"   📡 subscribe: {topic} ({len(topic_subscribers[topic])} 订阅者)")


async def handle_unsubscribe(ws, message: dict):
    topic = message.get("topic", "")
    topic_subscribers.get(topic, set()).discard(ws)
    log(f"   📡 unsubscribe: {topic}")


async def handle_publish_op(ws, message: dict):
    topic = message.get("topic", "")
    msg   = message.get("msg", {}) or {}
    if topic == topic_goal():
        await handle_goal(ws, msg)
    elif topic == topic_cancel():
        await handle_cancel(ws, msg)
    elif topic == NAV_ACTION_GOAL:
        # ── 导航 Action goal ──────────────────────────────────────────────────
        goal_id = (msg.get("goal_id") or {}).get("id", "")
        waypoints = (msg.get("goal") or {}).get("waypoints", [])
        log(f"\n🗺  [导航 goal] goal_id={goal_id[:16]}...  waypoints={len(waypoints)}")
        asyncio.create_task(_simulate_navigation_action(ws, goal_id))
    elif topic == NAV_ACTION_CANCEL:
        log(f"   🛑 [导航 cancel] 收到取消请求，忽略（mock 不实际取消）")
    else:
        log(f"   📤 publish (忽略) topic={topic}")


async def dispatch_op(ws, message: dict):
    """统一分发 rosbridge op（bridge-client 收到的转发消息 / embedded-server 收到的直连消息）"""
    op = message.get("op", "")
    if op == "subscribe":
        await handle_subscribe(ws, message)
    elif op == "unsubscribe":
        await handle_unsubscribe(ws, message)
    elif op == "publish":
        await handle_publish_op(ws, message)
    elif op == "call_service":
        service = message.get("service", "")
        if service == cfg.service_name:
            await handle_call_service_task(ws, message)
        else:
            # 未知 service：返回 not-implemented
            log(f"   🔧 call_service (未实现): {service}")
            await ws.send(json.dumps({
                "op": "service_response", "id": message.get("id", ""),
                "service": service, "result": False,
                "values": f"mock 仅实现 {cfg.service_name} service/action",
            }))
    elif op == "status":
        log(f"   ℹ rosbridge status: level={message.get('level')} msg={message.get('msg')}")
    else:
        log(f"   ℹ unknown op={op!r}")


# ==================== 后台 status 广播（bridge-client / embedded-server） ====================

async def status_publisher_loop():
    interval = 1.0 / max(cfg.status_rate_hz, 0.1)
    try:
        while True:
            await send_status_array()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


# ==================== ros-node 模式（rospy + actionlib） ====================

def _make_goal_fields(goal_msg) -> dict:
    try:
        rtt = goal_msg.robot_task_types
        if hasattr(rtt, '__dict__'):
            rtt = {k: v for k, v in vars(rtt).items() if not k.startswith('_')}
    except AttributeError:
        rtt = None
    return {
        "task":             getattr(goal_msg, "task", ""),
        "area":             getattr(goal_msg, "area", ""),
        "extra_params":     getattr(goal_msg, "extra_params", ""),
        "robot_task_types": rtt,
    }


def _run_action_goal_sync(goal_id: str, goal_fields: dict,
                          result_cls, feedback_cls, action_handle) -> None:
    """在 rospy 线程里同步执行 action goal（阻塞）"""
    import rospy

    log(f"\n🚀 [ROS Action] 开始执行 goal_id={goal_id}")
    log(f"   task={goal_fields.get('task')!r}  area={goal_fields.get('area')!r}")

    # 把 asyncio _execute_task 的异步接口适配成同步
    # 用 asyncio.run() 在独立线程里跑（rospy 线程没有 loop）
    import asyncio as _asyncio

    async def _run():
        async def fb(status_text: str, current_params: str = ""):
            fmsg = feedback_cls()
            fmsg.status = status_text
            fmsg.current_params = current_params
            action_handle.publish_feedback(fmsg)
            log(f"   📶 [ROS Action feedback] {status_text!r}")

        return await _execute_task(
            goal_fields,
            feedback_fn=fb,
            cancel_fn=action_handle.is_cancel_requested,
        )

    success, error_msg, return_params = _asyncio.run(_run())

    result = result_cls()
    result.success = success
    result.error_msg = error_msg
    result.return_params = return_params

    if action_handle.is_cancel_requested():
        action_handle.set_preempted(result)
    elif success:
        action_handle.set_succeeded(result)
    else:
        action_handle.set_aborted(result)

    log(f"   🏁 [ROS Action result] success={success}  error_msg={error_msg!r}")


def _handle_ros_service_request(request, result_cls):
    """rospy.Service 请求处理器（同步）"""
    import asyncio as _asyncio

    task_fields = {
        "task":             getattr(request, "task", ""),
        "area":             getattr(request, "area", ""),
        "extra_params":     getattr(request, "extra_params", ""),
        "robot_task_types": getattr(request, "robot_task_types", None),
    }
    log(f"\n📥 [ROS Service] 收到请求  task={task_fields['task']!r}")

    success, error_msg, return_params = _asyncio.run(_execute_task(task_fields))

    resp = result_cls()
    resp.success = success
    resp.error_msg = error_msg
    resp.return_params = return_params
    log(f"   🏁 [ROS Service] 响应  success={success}  error_msg={error_msg!r}")
    return resp


def _build_stub_msgs():
    """
    在没有安装 chem_project_msgs 时，构建最小 stub 消息类（仅用于 mock）。
    """
    class _StubResult:
        __slots__ = ["success", "error_msg", "return_params"]
        def __init__(self): self.success = False; self.error_msg = ""; self.return_params = ""

    class _StubFeedback:
        __slots__ = ["status", "current_params"]
        def __init__(self): self.status = ""; self.current_params = ""

    class _StubAction:
        pass  # actionlib.ActionServer 需要 Action 类，stub 时用占位

    class _StubSrvResponse:
        __slots__ = ["success", "error_msg", "return_params"]
        def __init__(self): self.success = False; self.error_msg = ""; self.return_params = ""

    return _StubAction, _StubResult, _StubFeedback, _StubSrvResponse


def run_ros_node():
    """
    ros-node 模式：使用 rospy 同时启动 Action Server 和 Service Server。

    rosbridge 作为透明传输层，把 ROS topic/service 桥接给 main.py（WebSocket）。
    """
    try:
        import rospy
        import actionlib
    except ImportError:
        print("❌ 未找到 rospy / actionlib。")
        print("   请先 source ROS 环境（如 source /opt/ros/noetic/setup.bash）")
        print("   或改用 --mode bridge-client。")
        raise SystemExit(1)

    # 尝试导入实际消息包
    try:
        msg_mod = __import__(f"{cfg.action_pkg}.msg", fromlist=[
            f"{cfg.action_name}Action",
            f"{cfg.action_name}Result",
            f"{cfg.action_name}Feedback",
        ])
        srv_mod = __import__(f"{cfg.action_pkg}.srv", fromlist=[
            f"{cfg.action_name}Service",
            f"{cfg.action_name}ServiceResponse",
        ])
        action_cls   = getattr(msg_mod, f"{cfg.action_name}Action")
        result_cls   = getattr(msg_mod, f"{cfg.action_name}Result")
        feedback_cls = getattr(msg_mod, f"{cfg.action_name}Feedback")
        srv_cls      = getattr(srv_mod, f"{cfg.action_name}Service")
        srv_resp_cls = getattr(srv_mod, f"{cfg.action_name}ServiceResponse")
        log(f"✅ 已加载 {cfg.action_pkg} 消息类型")
    except (ImportError, AttributeError) as e:
        # ros-node 模式需要真实 ROS 消息类型（含二进制序列化）；
        # stub 类无法满足 actionlib.ActionServer 的 action_goal/result/feedback 要求。
        # 自动降级到 bridge-client 模式——与 main.py 的交互行为完全一致。
        print(f"⚠  无法导入 {cfg.action_pkg}：{e}")
        print("   ros-node 模式需要真实 ROS 消息包，自动降级到 bridge-client 模式")
        print(f"   （如需 ros-node 模式，请在 ROS 工作空间中编译并安装 {cfg.action_pkg}）")
        cfg.mode = "bridge-client"
        asyncio.run(main_async())
        return

    node_name = f"mock_{cfg.action_name.lower()}_node"
    rospy.init_node(node_name, anonymous=False)
    log(f"✅ ROS 节点已启动: /{node_name}")

    import threading

    # ── Action Server ──────────────────────────────────────────────────────
    def goal_cb(goal_handle):
        goal_handle.set_accepted()
        goal_id = goal_handle.get_goal_id().id or f"ros-{uuid.uuid4().hex[:8]}"
        goal_fields = _make_goal_fields(goal_handle.get_goal())
        t = threading.Thread(
            target=_run_action_goal_sync,
            args=(goal_id, goal_fields, result_cls, feedback_cls, goal_handle),
            daemon=True,
        )
        t.start()

    action_server = actionlib.ActionServer(
        cfg.action_base,
        action_cls,
        goal_cb=goal_cb,
        cancel_cb=lambda gh: log(f"   🛑 [ROS Action] cancel: {gh.get_goal_id().id}"),
        auto_start=False,
    )
    action_server.start()
    log(f"✅ ROS Action Server 已启动: {cfg.action_base}")

    # ── Service Server ─────────────────────────────────────────────────────
    if srv_cls is not None:
        rospy.Service(
            cfg.service_name,
            srv_cls,
            lambda req: _handle_ros_service_request(req, srv_resp_cls),
        )
        log(f"✅ ROS Service Server 已启动: {cfg.service_name}")
    else:
        log(f"⚠  Service Server 跳过（消息包未安装）：{cfg.service_name}")

    _print_banner()
    print("\n等待请求（Ctrl+C 停止）\n")
    rospy.spin()


# ==================== bridge-client 模式 ====================

async def _bridge_send(ws, message: dict):
    await ws.send(json.dumps(message, ensure_ascii=False))


async def _advertise_bridge_action(ws):
    """
    向 rosbridge 声明 action 输出 topic + 订阅输入 topic（仅任务 Action）。

    导航 Action（/zj_humanoid/navigation/navigation/*）不在此处声明：
        bridge-client 模式连接的是真实机器人 rosbridge，导航由真实导航系统
        处理，mock 不应干预，且 'navigation' 消息包通常未安装，声明会报错。
        导航模拟仅在 embedded-server 模式（mock 本身即 rosbridge server）下有效。
    """
    for topic in (topic_status(), topic_feedback(), topic_result()):
        await _bridge_send(ws, {"op": "advertise", "topic": topic, "type": topic_type(topic)})
        log(f"   📢 advertise: {topic}")
    for topic in (topic_goal(), topic_cancel()):
        await _bridge_send(ws, {
            "op": "subscribe", "topic": topic, "type": topic_type(topic),
            "queue_length": 1, "throttle_rate": 0,
        })
        log(f"   📡 subscribe: {topic}")


async def _advertise_bridge_service(ws):
    """向 rosbridge 声明本程序提供的 ROS Service"""
    await _bridge_send(ws, {
        "op":      "advertise_service",
        "type":    type_service(),
        "service": cfg.service_name,
    })
    log(f"   📢 advertise_service: {cfg.service_name}  type={type_service()}")


async def run_bridge_client():
    """
    bridge-client 模式（备用）：通过 rosbridge WebSocket 协议参与 ROS 通信。

    效果与 ros-node 模式等价（main.py 视角看不出差别）：
    - Action：advertise status/feedback/result + subscribe goal/cancel
    - Service：advertise_service → rosbridge 把调用转发过来 → 回 service_response
    """
    global bridge_ws

    while True:
        try:
            print(f"\n连接机器人 rosbridge: {cfg.rosbridge_url}")
            async with websockets.connect(
                cfg.rosbridge_url,
                subprotocols=["rosbridge_v2"],
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                bridge_ws = ws
                print("✅ 已接入 rosbridge")
                await _advertise_bridge_action(ws)
                await _advertise_bridge_service(ws)
                await send_status_array()

                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        log(f"   ⚠ JSON 解析失败: {raw[:120]}")
                        continue
                    await dispatch_op(ws, message)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            bridge_ws = None
            print(f"❌ rosbridge 连接异常: {type(e).__name__}: {e}")
            print(f"   {cfg.reconnect_interval}s 后重连...")
            await asyncio.sleep(cfg.reconnect_interval)
        finally:
            bridge_ws = None


# ==================== embedded-server 模式（历史兼容） ====================

async def handle_client(ws, path=None):
    log(f"\n🔗 新连接: {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log(f"   ⚠ JSON 解析失败: {raw[:120]}")
                continue
            await dispatch_op(ws, message)
    except websockets.exceptions.ConnectionClosed:
        log(f"🔌 连接关闭: {ws.remote_address}")
    finally:
        for subs in topic_subscribers.values():
            subs.discard(ws)


async def run_embedded_server():
    """历史兼容模式：本程序自己伪装成 rosbridge server，监听本地端口。"""
    async with websockets.serve(
        handle_client,
        cfg.host,
        cfg.port,
        subprotocols=["rosbridge_v2"],
    ):
        print(f"\n等待连接... (Ctrl+C 停止)\n")
        await asyncio.Future()


# ==================== 入口 ====================

def _print_banner():
    print("=" * 72)
    print(f" Mock chem_project ROS 节点 — service + action")
    print("=" * 72)
    print(f" 运行模式         : {cfg.mode}")
    if cfg.mode == "ros-node":
        print(f" Action Server   : {cfg.action_base}/*  ({type_action_goal()})")
        print(f" Service Server  : {cfg.service_name}  ({type_service()})")
    elif cfg.mode == "bridge-client":
        print(f" rosbridge       : {cfg.rosbridge_url}")
        print(f" Action topics   : {cfg.action_base}/*")
        print(f" Service         : {cfg.service_name}")
    else:
        print(f" 监听            : ws://{cfg.host}:{cfg.port}")
        print(f" Action topics   : {cfg.action_base}/*")
        print(f" Service         : {cfg.service_name}")
    print(f" task 行为       : success_fast / success_with_steps(默认) / fail / fail_with_steps / long")
    print("=" * 72)


async def main_async():
    global status_publisher_task
    _print_banner()
    status_publisher_task = asyncio.create_task(status_publisher_loop())
    if cfg.mode == "embedded-server":
        await run_embedded_server()
    else:
        await run_bridge_client()


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "Mock chem_project ROS 节点（service + action）\n"
            "  ros-node（默认）: rospy + actionlib，需 source ROS 环境\n"
            "  bridge-client  : 无 ROS 时通过 rosbridge WebSocket 模拟\n"
            "  embedded-server: 本地假 rosbridge server（历史兼容）\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mode", choices=["ros-node", "bridge-client", "embedded-server"],
                   default=cfg.mode)
    p.add_argument("--rosbridge-url", default=cfg.rosbridge_url)
    p.add_argument("--reconnect-interval", type=float, default=cfg.reconnect_interval)
    p.add_argument("--host",         default=cfg.host)
    p.add_argument("--port",         type=int, default=cfg.port)
    p.add_argument("--action-base",  default=cfg.action_base,
                   help="Action 基名，派生 5 个 topic（默认 /robot_task）")
    p.add_argument("--service-name", default=cfg.service_name,
                   help="ROS Service 名称（默认 /robot_task）")
    p.add_argument("--action-pkg",   default=cfg.action_pkg,
                   help="消息包名（默认 navi_types）")
    p.add_argument("--action-name",  default=cfg.action_name,
                   help="动作/服务名（默认 RobotAction）")
    p.add_argument("--feedback-steps",  type=int,   default=cfg.default_feedback_steps)
    p.add_argument("--step-interval",   type=float, default=cfg.default_step_interval)
    p.add_argument("--long-duration",   type=float, default=cfg.long_task_duration)
    p.add_argument("--status-rate",     type=float, default=cfg.status_rate_hz,
                   help="status 广播频率 Hz（bridge-client/embedded-server 模式）")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    cfg.mode               = args.mode
    cfg.rosbridge_url      = args.rosbridge_url
    cfg.reconnect_interval = args.reconnect_interval
    cfg.host               = args.host
    cfg.port               = args.port
    cfg.action_base        = args.action_base
    cfg.service_name       = args.service_name
    cfg.action_pkg         = args.action_pkg
    cfg.action_name        = args.action_name
    cfg.default_feedback_steps = args.feedback_steps
    cfg.default_step_interval  = args.step_interval
    cfg.long_task_duration     = args.long_duration
    cfg.status_rate_hz         = args.status_rate
    cfg.verbose                = not args.quiet

    if cfg.mode == "ros-node":
        run_ros_node()
    else:
        try:
            asyncio.run(main_async())
        except KeyboardInterrupt:
            print("\n✅ 已停止")


if __name__ == "__main__":
    main()
