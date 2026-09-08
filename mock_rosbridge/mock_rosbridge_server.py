#!/usr/bin/env python3
"""
模拟 ROS Bridge WebSocket 服务器（多机器人版本）

支持多台机器人通过不同端口连接，单进程同时监听多个端口：
  端口 9090 → robot_a
  端口 9091 → robot_b
  端口 9092 → robot_c
  （端口 → 机器人 ID 映射可通过命令行 --robots 覆盖）

每台机器人拥有独立的导航状态，互不干扰。

用法：
  # 默认三台：9090/9091/9092 → robot_a/b/c
  python mock_rosbridge_server.py

  # 同时模拟两台机器人
  python mock_rosbridge_server.py --robots robot_a:9090 robot_b:9091

  # 任意端口/机器人组合
  python mock_rosbridge_server.py --robots robot_a:9090 robot_b:9091 robot_c:9092
"""

import asyncio
import json
import random
import subprocess
import time
import argparse
import websockets

# ── 默认端口 → 机器人 ID 映射 ─────────────────────────────────────────────────
HOST = "0.0.0.0"

DEFAULT_ROBOTS = {
    9090: "robot_a",
    9091: "robot_b",
    9092: "robot_c",
}

# ── 动作延迟配置（秒）────────────────────────────────────────────────────────
ACTION_DELAY = {
    "default":           2,    # 普通 service 调用延迟
    "navigation":        4,    # 导航总时长（actionlib action）
    "assembly":          5,    # ATC 装配延迟
    "pick_up_component": 3,    # ATC 抓取零件
    "put_down":          2,    # ATC 放下零件
    "pick_box":          3,    # ATC 搬箱子
}

# ── 旧协议导航状态（订阅 /navigation_status 的兼容层）────────────────────────
NAV_STANDBY  = {"state": {"value": 1}, "taskstate": {"value": 0}}
NAV_PLANNING = {"state": {"value": 2}, "taskstate": {"value": 1}}
NAV_RUNNING  = {"state": {"value": 3}, "taskstate": {"value": 1}}
NAV_DONE     = {"state": {"value": 5}, "taskstate": {"value": 2}}

# ── 新协议：actionlib topic 终结点（导航）────────────────────────────────────
NAV_ACTION_GOAL     = "/zj_humanoid/navigation/navigation/goal"
NAV_ACTION_FEEDBACK = "/zj_humanoid/navigation/navigation/feedback"
NAV_ACTION_RESULT   = "/zj_humanoid/navigation/navigation/result"
NAV_ACTION_CANCEL   = "/zj_humanoid/navigation/navigation/cancel"

# ── 任务 Action topic 终结点（KAIAO/WAIC send_task_action）──────────────────
TASK_ACTION_GOAL     = "/robot_task/goal"
TASK_ACTION_FEEDBACK = "/robot_task/feedback"
TASK_ACTION_RESULT   = "/robot_task/result"
TASK_ACTION_CANCEL   = "/robot_task/cancel"

# 电池电量（CONST MQTT 心跳 / BatteryMonitor / GET_BATTERY_STATE）
BATTERY_TOPIC = "/zj_humanoid/robot/battery_info"

# NavigationState (navigation/NavigationState.msg)
NAV_STATE_RUNNING = 2   # Running
NAV_STATE_ARRIVED = 3   # Arrived（成功）
NAV_STATE_FAILED  = 7   # Failed

# actionlib_msgs/GoalStatus
ACTIONLIB_ACTIVE    = 1
ACTIONLIB_SUCCEEDED = 3
ACTIONLIB_ABORTED   = 4


def _who_listens(port: int) -> str:
    """查占用 port 的进程，方便提示「已经有一份 mock 在跑」。"""
    try:
        out = subprocess.check_output(
            ["ss", "-lptn", f"sport = :{port}"],
            stderr=subprocess.DEVNULL, text=True,
        )
        for line in out.splitlines():
            if "users:" in line:
                return line.strip()
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["fuser", "-v", f"{port}/tcp"],
            stderr=subprocess.STDOUT, text=True,
        )
        return " ".join(out.split())
    except Exception:
        pass
    return ""


class RobotState:
    """单台机器人的独立状态"""
    def __init__(self, robot_id: str):
        self.robot_id = robot_id
        self.nav_status = dict(NAV_STANDBY)
        # sensor_msgs/BatteryState.percentage：0.0~1.0
        self.battery_percentage = 0.85
        self.battery_voltage = 24.5
        self.battery_current = -1.2
        self.battery_power_supply_status = 2  # 2=放电

    def __repr__(self):
        return f"RobotState({self.robot_id})"

    def battery_msg(self) -> dict:
        now = time.time()
        sec = int(now)
        nsec = int((now - sec) * 1e9)
        return {
            "header": {
                "stamp": {"secs": sec, "nsecs": nsec},
                "frame_id": "",
                "seq": 0,
            },
            "voltage": self.battery_voltage,
            "current": self.battery_current,
            "charge": 0.0,
            "capacity": 0.0,
            "design_capacity": 0.0,
            "percentage": self.battery_percentage,
            "power_supply_status": self.battery_power_supply_status,
            "power_supply_health": 1,
            "power_supply_technology": 0,
            "present": True,
            "cell_voltage": [],
            "cell_temperature": [],
            "location": "",
            "serial_number": "",
        }


class MockRosBridge:
    def __init__(self, port_to_robot: dict):
        """
        参数:
            port_to_robot: {port(int): robot_id(str)}，例如 {9090: "robot_a", 9091: "robot_b"}
        """
        self.port_to_robot = port_to_robot
        self.clients = set()
        # websocket → RobotState（按连接端口确定，连接时立即绑定）
        self.client_robot: dict = {}
        # robot_id → RobotState
        self.robots: dict = {rid: RobotState(rid) for rid in port_to_robot.values()}
        self.robots["unknown"] = RobotState("unknown")
        # websocket → [subscribed_topics]
        self.subscribed_topics: dict = {}

    def make_handler(self, port: int):
        """为指定端口生成 websocket 处理函数，连接时自动绑定机器人"""
        robot_id = self.port_to_robot.get(port, "unknown")

        async def _handler(websocket):
            await self.handle_client(websocket, robot_id)

        return _handler

    # ──────────────────────────────────────────────────────────────────────────
    # 连接生命周期
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_client(self, websocket, robot_id: str):
        self.clients.add(websocket)
        robot_state = self.robots.get(robot_id, self.robots["unknown"])
        self.client_robot[websocket] = robot_state
        addr = websocket.remote_address
        print(f"\n[连接] {robot_id} 客户端已连接: {addr}")
        try:
            async for message in websocket:
                await self.process_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            print(f"[断开] {robot_id} 客户端已断开: {addr}")
        finally:
            self.clients.discard(websocket)
            self.client_robot.pop(websocket, None)
            self.subscribed_topics.pop(websocket, None)

    # ──────────────────────────────────────────────────────────────────────────
    # 消息分发
    # ──────────────────────────────────────────────────────────────────────────

    async def process_message(self, websocket, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            print(f"[错误] JSON解析失败: {message[:100]}")
            return

        op = data.get("op")
        print(f"\n[收到] op={op}  data={json.dumps(data, ensure_ascii=False)[:200]}")

        if op == "call_service":
            await self.handle_service_call(websocket, data)
        elif op == "publish":
            await self.handle_publish(websocket, data)
        elif op == "subscribe":
            await self.handle_subscribe(websocket, data)
        elif op == "unsubscribe":
            await self.handle_unsubscribe(websocket, data)
        else:
            print(f"[警告] 未知操作: {op}")

    # ──────────────────────────────────────────────────────────────────────────
    # service call 处理（ATC new-protocol: task / area / extra_params）
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_service_call(self, websocket, data):
        service = data.get("service", "")
        args    = data.get("args", {})

        # 兼容新旧两种协议：
        #   旧协议: args.action
        #   新协议: args.task (ATC send_service_request_task)
        task   = args.get("task") or args.get("action", "")
        area   = args.get("area", "")
        robot_state = self.client_robot.get(websocket, self.robots["unknown"])

        print(f"  [{robot_state.robot_id}] 服务={service}  task={task}  area={area}")

        # 按 task 确定延迟
        delay = self._get_delay(task)
        print(f"  [{robot_state.robot_id}] 模拟执行 {delay}s ...")
        await asyncio.sleep(delay)

        response = self._generate_response(service, task, area, args, robot_state)
        await websocket.send(json.dumps(response))
        print(f"  [{robot_state.robot_id}] 响应: {json.dumps(response, ensure_ascii=False)}")

    def _get_delay(self, task: str) -> float:
        task_lower = task.lower()
        if "navigation" in task_lower:
            return ACTION_DELAY["navigation"]
        if "assembly" in task_lower:
            return ACTION_DELAY["assembly"]
        if "pick_up" in task_lower or "pick_box" in task_lower:
            return ACTION_DELAY["pick_up_component"]
        if "put_down" in task_lower or "put_box" in task_lower:
            return ACTION_DELAY["put_down"]
        return ACTION_DELAY["default"]

    def _task_return_params(self, task, area, robot_state: RobotState) -> str:
        payload = {"robot_id": robot_state.robot_id, "task": task, "area": area}
        if task == "pick_up_box":
            payload["has_box"] = True
            payload["gauge_count"] = 4
        return json.dumps(payload)

    def _generate_response(self, service, task, area, args, robot_state: RobotState) -> dict:
        """生成统一的 service_response（兼容新旧协议）"""
        # 新协议响应格式（ATC）：success / error_msg / return_params
        # 旧协议响应格式：result / values.finish
        is_new_protocol = bool(args.get("task"))

        if is_new_protocol:
            return {
                "op":      "service_response",
                "service": service,
                "result":  True,
                "values": {
                    "success":       True,
                    "error_msg":     "",
                    "return_params": self._task_return_params(task, area, robot_state),
                },
            }

        # 旧协议
        base = {"op": "service_response", "service": service, "result": True}

        if task == "cv_detect":
            detected = random.choice([True, True, True, False])
            if detected:
                base["values"] = {
                    "finish":      True,
                    "object_pose": f"pose_{random.randint(0, 5)}",
                    "object_type": random.choice(["glass_bottle_500", "plastic_bottle_350"]),
                }
            else:
                base["values"] = {"finish": False}
        elif task == "navigation_to_pose":
            asyncio.create_task(self._simulate_navigation_legacy(robot_state))
            base["values"] = {"finish": True, "message": "导航已启动"}
        else:
            base["values"] = {"finish": True, "message": f"执行完成: {task}"}

        return base

    # ──────────────────────────────────────────────────────────────────────────
    # publish（客户端发布 topic，例如导航目标）
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_publish(self, websocket, data):
        topic = data.get("topic", "")
        msg   = data.get("msg", {})
        robot_state = self.client_robot.get(websocket, self.robots["unknown"])
        print(f"  [{robot_state.robot_id}] 客户端发布 topic={topic}")

        if topic == NAV_ACTION_GOAL:
            # ── actionlib 导航 goal ──────────────────────────────────────────
            goal_id = (msg.get("goal_id") or {}).get("id", "")
            print(f"  [{robot_state.robot_id}] 收到导航 goal  goal_id={goal_id}")
            asyncio.create_task(
                self._simulate_navigation_action(websocket, robot_state, goal_id)
            )
        elif topic == NAV_ACTION_CANCEL:
            print(f"  [{robot_state.robot_id}] 收到导航 cancel，忽略")
        elif topic == TASK_ACTION_GOAL:
            # ── actionlib 任务 goal（KAIAO/WAIC send_task_action）───────────
            goal_id   = (msg.get("goal_id") or {}).get("id", "")
            inner     = msg.get("goal") or {}
            task_name = inner.get("task", "")
            area      = inner.get("area", "")
            print(f"  [{robot_state.robot_id}] 收到任务 goal  task={task_name!r}  area={area!r}  goal_id={goal_id[:16]}")
            asyncio.create_task(
                self._simulate_task_action(websocket, robot_state, goal_id, task_name, area)
            )
        elif topic == TASK_ACTION_CANCEL:
            print(f"  [{robot_state.robot_id}] 收到任务 cancel，忽略")
        elif topic == "/navigation_control":
            # ── 旧协议兼容：更新 nav_status 供 /navigation_status 推送 ─────────
            asyncio.create_task(self._simulate_navigation_legacy(robot_state))

    # ──────────────────────────────────────────────────────────────────────────
    # 新协议：actionlib navigation 模拟（每台机器人独立）
    # ──────────────────────────────────────────────────────────────────────────

    def _make_stamp(self) -> dict:
        now = time.time()
        return {"secs": int(now), "nsecs": int((now % 1) * 1e9)}

    def _make_nav_feedback_msg(self, goal_id: str, nav_state_value: int) -> dict:
        """构造 navigation/NavigationActionFeedback 消息"""
        stamp = self._make_stamp()
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

    def _make_nav_result_msg(self, goal_id: str, succeeded: bool = True) -> dict:
        """构造 navigation/NavigationActionResult 消息"""
        stamp = self._make_stamp()
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
                "duration": {"secs": ACTION_DELAY["navigation"], "nsecs": 0},
            },
        }

    async def _simulate_navigation_action(
        self, websocket, robot_state: RobotState, goal_id: str
    ):
        """
        模拟 actionlib 导航：push feedback → push result。
        消息通过 rosbridge 的 `publish` op 推回给订阅了对应 topic 的客户端。
        """
        rid = robot_state.robot_id
        delay = ACTION_DELAY["navigation"]

        async def _push(topic: str, msg: dict):
            """向所有订阅了该 topic 的客户端推送消息（通常就是发起 goal 的那个连接）"""
            payload = json.dumps({"op": "publish", "topic": topic, "msg": msg})
            for ws, topics in self.subscribed_topics.items():
                if topic in topics and ws in self.clients:
                    # 只推给同一机器人的连接
                    if self.client_robot.get(ws) is robot_state:
                        try:
                            await ws.send(payload)
                        except Exception:
                            pass

        # 1. 推送 RUNNING feedback（0.3s 后，让订阅先稳定）
        await asyncio.sleep(0.3)
        fb_running = self._make_nav_feedback_msg(goal_id, NAV_STATE_RUNNING)
        await _push(NAV_ACTION_FEEDBACK, fb_running)
        print(f"  [{rid}] 导航 feedback → RUNNING  goal_id={goal_id}")

        # 2. 模拟行进时间，中途持续推 RUNNING feedback（每秒一次）
        elapsed = 0.3
        while elapsed < delay - 0.5:
            await asyncio.sleep(1.0)
            elapsed += 1.0
            await _push(NAV_ACTION_FEEDBACK, self._make_nav_feedback_msg(goal_id, NAV_STATE_RUNNING))

        await asyncio.sleep(max(0.0, delay - elapsed))

        # 3. 推送 ARRIVED result
        result_msg = self._make_nav_result_msg(goal_id, succeeded=True)
        await _push(NAV_ACTION_RESULT, result_msg)
        print(f"  [{rid}] 导航 result   → ARRIVED  goal_id={goal_id}")

        # 4. 顺带更新旧协议 nav_status（保持向后兼容）
        robot_state.nav_status = dict(NAV_DONE)
        await asyncio.sleep(0.5)
        robot_state.nav_status = dict(NAV_STANDBY)

    # ──────────────────────────────────────────────────────────────────────────
    # 任务 Action 模拟（KAIAO / WAIC send_task_action → /robot_task/*）
    # ──────────────────────────────────────────────────────────────────────────

    def _make_task_feedback_msg(self, goal_id: str, status: str, current_params: str = "") -> dict:
        """构造 ChemProjectActionFeedback / 兼容格式"""
        stamp = self._make_stamp()
        return {
            "header": {"seq": 0, "stamp": stamp, "frame_id": ""},
            "status": {
                "goal_id": {"stamp": stamp, "id": goal_id},
                "status": ACTIONLIB_ACTIVE,
                "text": "",
            },
            "feedback": {
                "status":         status,
                "current_params": current_params,
            },
        }

    def _make_task_result_msg(self, goal_id: str, succeeded: bool = True,
                               error_msg: str = "", return_params: str = "") -> dict:
        """构造 ChemProjectActionResult / 兼容格式"""
        stamp = self._make_stamp()
        actionlib_status = ACTIONLIB_SUCCEEDED if succeeded else ACTIONLIB_ABORTED
        return {
            "header": {"seq": 0, "stamp": stamp, "frame_id": ""},
            "status": {
                "goal_id": {"stamp": stamp, "id": goal_id},
                "status": actionlib_status,
                "text": "Succeeded" if succeeded else "Failed",
            },
            "result": {
                "success":       succeeded,
                "error_msg":     error_msg,
                "return_params": return_params,
            },
        }

    async def _simulate_task_action(
        self, websocket, robot_state: RobotState,
        goal_id: str, task_name: str, area: str
    ):
        """
        模拟任务 Action（pick_up_box / put_down_box / pick_up_component / put_down_component 等）。
        推送 feedback → 延迟 → 推送 result。
        """
        rid = robot_state.robot_id
        delay = self._get_delay(task_name)

        async def _push(topic: str, msg: dict):
            payload = json.dumps({"op": "publish", "topic": topic, "msg": msg})
            for ws, topics in self.subscribed_topics.items():
                if topic in topics and ws in self.clients:
                    if self.client_robot.get(ws) is robot_state:
                        try:
                            await ws.send(payload)
                        except Exception:
                            pass

        # 1. 立即推送 step_1 feedback
        await asyncio.sleep(0.2)
        await _push(TASK_ACTION_FEEDBACK,
                    self._make_task_feedback_msg(goal_id, "step_1_of_2", ""))
        print(f"  [{rid}] 任务 feedback → step_1_of_2  task={task_name!r}")

        # 2. 等待模拟执行时长
        await asyncio.sleep(delay - 0.2)

        # 3. 推送成功 result
        result_msg = self._make_task_result_msg(
            goal_id, succeeded=True,
            return_params=self._task_return_params(task_name, area, robot_state),
        )
        await _push(TASK_ACTION_RESULT, result_msg)
        print(f"  [{rid}] 任务 result   → success  task={task_name!r}")

    # ──────────────────────────────────────────────────────────────────────────
    # 旧协议：/navigation_status topic 模拟（向后兼容）
    # ──────────────────────────────────────────────────────────────────────────

    async def _simulate_navigation_legacy(self, robot_state: RobotState):
        rid = robot_state.robot_id
        print(f"  [{rid}] [旧协议] 导航: 规划中...")
        robot_state.nav_status = dict(NAV_PLANNING)
        await asyncio.sleep(0.5)
        robot_state.nav_status = dict(NAV_RUNNING)
        await asyncio.sleep(ACTION_DELAY["navigation"] - 0.5)
        robot_state.nav_status = dict(NAV_DONE)
        await asyncio.sleep(0.5)
        robot_state.nav_status = dict(NAV_STANDBY)

    # ──────────────────────────────────────────────────────────────────────────
    # Topic 订阅 / 取消订阅
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_subscribe(self, websocket, data):
        topic = data.get("topic", "")
        robot_state = self.client_robot.get(websocket, self.robots["unknown"])
        print(f"  [{robot_state.robot_id}] 订阅 {topic}")

        if websocket not in self.subscribed_topics:
            self.subscribed_topics[websocket] = []
        if topic not in self.subscribed_topics[websocket]:
            self.subscribed_topics[websocket].append(topic)
            if topic == "/navigation_status":
                asyncio.create_task(self._publish_nav_status(websocket, topic, robot_state))
            elif topic == BATTERY_TOPIC:
                asyncio.create_task(self._publish_battery(websocket, topic, robot_state))

    async def handle_unsubscribe(self, websocket, data):
        topic = data.get("topic", "")
        if websocket in self.subscribed_topics and topic in self.subscribed_topics[websocket]:
            self.subscribed_topics[websocket].remove(topic)

    async def _publish_nav_status(self, websocket, topic, robot_state: RobotState):
        rid = robot_state.robot_id
        print(f"  [{rid}] 开始发布 {topic} (2Hz)")
        while websocket in self.clients:
            if websocket not in self.subscribed_topics or topic not in self.subscribed_topics[websocket]:
                break
            try:
                msg = {"op": "publish", "topic": topic, "msg": robot_state.nav_status}
                await websocket.send(json.dumps(msg))
            except Exception:
                break
            await asyncio.sleep(0.5)
        print(f"  [{rid}] 停止发布 {topic}")

    async def _publish_battery(self, websocket, topic, robot_state: RobotState):
        rid = robot_state.robot_id
        print(f"  [{rid}] 开始发布 {topic} (1Hz, {robot_state.battery_percentage * 100:.0f}%)")
        while websocket in self.clients:
            if websocket not in self.subscribed_topics or topic not in self.subscribed_topics[websocket]:
                break
            try:
                msg = {"op": "publish", "topic": topic, "msg": robot_state.battery_msg()}
                await websocket.send(json.dumps(msg))
            except Exception:
                break
            await asyncio.sleep(1.0)
        print(f"  [{rid}] 停止发布 {topic}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

async def main(port_to_robot: dict):
    server = MockRosBridge(port_to_robot)

    print("=" * 60)
    print("Mock ROS Bridge Server  [多机器人版本]")
    print("=" * 60)
    print("端口 → 机器人 映射：")
    for port, rid in sorted(port_to_robot.items()):
        print(f"  ws://{HOST}:{port}  →  {rid}")
    print()
    print("支持的操作：call_service / subscribe / unsubscribe / publish")
    print("订阅即推送: /navigation_status (2Hz)  /zj_humanoid/robot/battery_info (1Hz, 85%)")
    print("新协议 (ATC): args.task + args.area + args.extra_params")
    print("旧协议 (旧项目): args.action + args.extra_params")
    print("=" * 60)

    # 并发启动每个端口的 WebSocket 服务器（兼容 Python 3.9+）
    # 某个端口已被占用时跳过，不把整份 mock 打死（常见原因：上一份 mock 还在后台跑）
    active_servers = []
    skipped = []
    for port, rid in port_to_robot.items():
        try:
            srv = await websockets.serve(
                server.make_handler(port),
                HOST,
                port,
                subprotocols=["rosbridge_v2"],
            )
            active_servers.append(srv)
            print(f"  已监听 ws://{HOST}:{port}  →  {rid}")
        except OSError as e:
            occupant = _who_listens(port)
            hint = f"  占用进程: {occupant}" if occupant else ""
            print(
                f"  跳过端口 {port}（{rid}）：已被占用"
                f"{' — ' + e.strerror if getattr(e, 'strerror', None) else ''}"
                f"{hint}"
            )
            skipped.append((port, rid, occupant))

    if not active_servers:
        print()
        print("没有成功绑定任何端口，本进程退出。")
        print("如果上一份 mock 还在后台跑，直接用那一份即可，不必再启动。")
        print("要重新拉起：  pkill -f mock_rosbridge_server.py")
        print("再执行：      python mock_rosbridge_server.py")
        return

    print()
    if skipped:
        print("部分端口未绑定（多半是已有 mock / 真机 rosbridge 占着），其余端口继续服务。")
    print("等待连接...\n")

    await asyncio.Future()  # 永久运行，直到 KeyboardInterrupt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock ROS Bridge Server (多机器人，按端口区分)")
    parser.add_argument(
        "--robots",
        nargs="+",
        metavar="ROBOT_ID:PORT",
        help="机器人与端口映射，格式: robot_a:9090 robot_b:9091 robot_c:9092"
             "（默认: robot_a:9090 robot_b:9091 robot_c:9092）",
    )
    args = parser.parse_args()

    port_to_robot = {}
    if args.robots:
        for entry in args.robots:
            try:
                robot_id, port_str = entry.rsplit(":", 1)
                port_to_robot[int(port_str)] = robot_id.strip()
            except ValueError:
                parser.error(f"格式错误: {entry!r}，应为 robot_id:port，例如 robot_a:9090")
    else:
        port_to_robot = dict(DEFAULT_ROBOTS)

    try:
        asyncio.run(main(port_to_robot))
    except KeyboardInterrupt:
        print("\n服务器已停止")

