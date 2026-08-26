#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟导航 Action 服务器

模拟机器人上的 ROS Bridge，用于测试导航 Action 功能。
支持接收 call_action、cancel_action 消息，并返回 feedback 和 result。

使用方法:
    python mock_navigation_action_server.py --port 9090
    python mock_navigation_action_server.py --port 9090 --duration 10 --feedback-interval 1
"""

import argparse
import asyncio
import json
import time
import random
from typing import Dict, Set
from dataclasses import dataclass
from enum import IntEnum

try:
    import websockets
except ImportError:
    print("请安装 websockets: pip install websockets")
    exit(1)


# ==================== 导航状态枚举 ====================

class NavigationState(IntEnum):
    NONE = 0
    RUNNING = 1
    SUCCEEDED = 2
    FAILED = 3
    CANCELLED = 4
    ABORTED = 5


# ==================== 活动的 Action 跟踪 ====================

@dataclass
class ActiveAction:
    """活动的 Action 信息"""
    action_name: str
    goal: dict
    start_time: float
    websocket: "websockets.WebSocketServerProtocol"
    cancelled: bool = False
    task: asyncio.Task = None


# 全局存储活动的 Actions
active_actions: Dict[str, ActiveAction] = {}


# ==================== 配置 ====================

class Config:
    # 模拟导航持续时间（秒）
    navigation_duration: float = 10.0
    # 反馈发送间隔（秒）
    feedback_interval: float = 1.0
    # 失败概率（0-1）
    failure_probability: float = 0.0
    # 是否打印详细日志
    verbose: bool = True


config = Config()


# ==================== 消息处理 ====================

async def handle_call_action(websocket, message: dict):
    """处理 call_action 消息"""
    action_name = message.get("action", "")
    action_type = message.get("action_type", "")
    goal = message.get("args", {})
    
    print(f"\n{'='*60}")
    print(f"📥 收到 Action 请求")
    print(f"   action: {action_name}")
    print(f"   type: {action_type}")
    print(f"{'='*60}")
    
    if config.verbose:
        waypoints = goal.get("waypoints", [])
        print(f"   路点数量: {len(waypoints)}")
        for i, wp in enumerate(waypoints):
            pose = wp.get("pose", {})
            pos = pose.get("position", {})
            print(f"   路点{i+1}: ({pos.get('x', 0):.2f}, {pos.get('y', 0):.2f}, {pos.get('z', 0):.2f})")
    
    # 创建 Action 记录
    action = ActiveAction(
        action_name=action_name,
        goal=goal,
        start_time=time.time(),
        websocket=websocket,
    )
    active_actions[action_name] = action
    
    # 启动执行任务
    action.task = asyncio.create_task(
        execute_navigation_action(action)
    )


async def handle_cancel_action(websocket, message: dict):
    """处理 cancel_action 消息"""
    action_name = message.get("action", "")
    
    print(f"\n🛑 收到取消请求: {action_name}")
    
    if action_name in active_actions:
        action = active_actions[action_name]
        action.cancelled = True
        print(f"   ✓ Action 已标记为取消")
    else:
        print(f"   ⚠ 未找到活动的 Action: {action_name}")


async def execute_navigation_action(action: ActiveAction):
    """执行导航 Action（模拟）"""
    action_name = action.action_name
    websocket = action.websocket
    
    print(f"\n🚀 开始执行导航: {action_name}")
    
    # 计算总路点数
    waypoints = action.goal.get("waypoints", [])
    total_waypoints = len(waypoints)
    
    # 模拟导航过程
    elapsed = 0.0
    waypoint_index = 0
    
    while elapsed < config.navigation_duration:
        # 检查是否被取消
        if action.cancelled:
            print(f"   ⏹ 导航已取消")
            await send_action_result(websocket, action_name, NavigationState.CANCELLED)
            return
        
        # 发送反馈
        progress = min(elapsed / config.navigation_duration, 1.0)
        waypoint_index = min(int(progress * total_waypoints), total_waypoints - 1)
        
        await send_action_feedback(
            websocket, 
            action_name, 
            NavigationState.RUNNING,
            current_waypoint=waypoint_index,
            total_waypoints=total_waypoints,
            progress=progress,
        )
        
        # 等待
        await asyncio.sleep(config.feedback_interval)
        elapsed += config.feedback_interval
    
    # 检查是否被取消
    if action.cancelled:
        print(f"   ⏹ 导航已取消")
        await send_action_result(websocket, action_name, NavigationState.CANCELLED)
        return
    
    # 随机失败
    if random.random() < config.failure_probability:
        print(f"   ❌ 导航失败（模拟）")
        await send_action_result(
            websocket, action_name, NavigationState.FAILED,
            causes=[{"code": 1001, "message": "模拟导航失败"}]
        )
        return
    
    # 导航成功
    print(f"   ✅ 导航成功")
    await send_action_result(
        websocket, action_name, NavigationState.SUCCEEDED,
        duration_secs=elapsed,
        distance_deviation=random.uniform(0.01, 0.1),
        heading_deviation=random.uniform(0.01, 0.05),
    )
    
    # 清理
    if action_name in active_actions:
        del active_actions[action_name]


async def send_action_feedback(
    websocket,
    action_name: str,
    state: NavigationState,
    current_waypoint: int = 0,
    total_waypoints: int = 1,
    progress: float = 0.0,
):
    """发送 Action 反馈"""
    current_time = time.time()
    secs = int(current_time)
    nsecs = int((current_time - secs) * 1e9)
    
    feedback_msg = {
        "op": "action_feedback",
        "action": action_name,
        "values": {
            "header": {
                "stamp": {"secs": secs, "nsecs": nsecs},
                "frame_id": "map"
            },
            "state": {"value": state.value},
            "faults": [],
            # 扩展信息（非标准，用于调试）
            "current_waypoint": current_waypoint,
            "total_waypoints": total_waypoints,
            "progress": progress,
        }
    }
    
    await websocket.send(json.dumps(feedback_msg))
    
    if config.verbose:
        print(f"   📶 发送反馈: state={state.name}, progress={progress*100:.1f}%")


async def send_action_result(
    websocket,
    action_name: str,
    state: NavigationState,
    duration_secs: float = 0.0,
    distance_deviation: float = 0.0,
    heading_deviation: float = 0.0,
    causes: list = None,
):
    """发送 Action 结果"""
    current_time = time.time()
    secs = int(current_time)
    nsecs = int((current_time - secs) * 1e9)
    
    duration_int_secs = int(duration_secs)
    duration_nsecs = int((duration_secs - duration_int_secs) * 1e9)
    
    result_msg = {
        "op": "action_result",
        "action": action_name,
        "values": {
            "header": {
                "stamp": {"secs": secs, "nsecs": nsecs},
                "frame_id": "map"
            },
            "duration": {
                "secs": duration_int_secs,
                "nsecs": duration_nsecs
            },
            "distance_deviation": distance_deviation,
            "heading_deviation": heading_deviation,
            "state": {"value": state.value},
            "causes": causes or [],
        }
    }
    
    await websocket.send(json.dumps(result_msg))
    
    print(f"   🏁 发送结果: state={state.name}, duration={duration_secs:.2f}s")


# ==================== WebSocket 服务器 ====================

async def handle_client(websocket, path=None):
    """处理客户端连接"""
    client_id = id(websocket)
    remote = websocket.remote_address
    
    print(f"\n🔗 新连接: {remote} (ID: {client_id})")
    
    try:
        async for message_str in websocket:
            try:
                message = json.loads(message_str)
                op = message.get("op", "")
                
                if op == "call_action":
                    await handle_call_action(websocket, message)
                    
                elif op == "cancel_action":
                    await handle_cancel_action(websocket, message)
                    
                elif op == "subscribe":
                    # 处理 topic 订阅（简单回复）
                    topic = message.get("topic", "")
                    print(f"   📡 订阅 topic: {topic}")
                    
                elif op == "publish":
                    # 处理 topic 发布
                    topic = message.get("topic", "")
                    print(f"   📤 发布到 topic: {topic}")
                    
                elif op == "call_service":
                    # 处理服务调用（简单回复成功）
                    service = message.get("service", "")
                    print(f"   🔧 调用服务: {service}")
                    
                    response = {
                        "op": "service_response",
                        "service": service,
                        "result": True,
                        "values": {"result": True, "finish": True}
                    }
                    await websocket.send(json.dumps(response))
                    
                else:
                    print(f"   ⚠ 未知操作: {op}")
                    if config.verbose:
                        print(f"     消息: {message_str[:200]}")
                        
            except json.JSONDecodeError as e:
                print(f"   ❌ JSON 解析错误: {e}")
                
    except websockets.exceptions.ConnectionClosed as e:
        print(f"\n🔌 连接关闭: {remote} (code={e.code})")
        
    except Exception as e:
        print(f"\n❌ 连接错误: {e}")
    
    finally:
        # 清理该连接的所有活动 Actions
        to_remove = [
            name for name, action in active_actions.items()
            if action.websocket == websocket
        ]
        for name in to_remove:
            action = active_actions.pop(name, None)
            if action and action.task:
                action.task.cancel()
        
        if to_remove:
            print(f"   清理 {len(to_remove)} 个活动 Action")


async def start_server(host: str, port: int):
    """启动 WebSocket 服务器"""
    print(f"\n{'='*60}")
    print(f"🤖 模拟导航 Action 服务器")
    print(f"{'='*60}")
    print(f"地址: ws://{host}:{port}")
    print(f"导航持续时间: {config.navigation_duration}秒")
    print(f"反馈间隔: {config.feedback_interval}秒")
    print(f"失败概率: {config.failure_probability*100:.1f}%")
    print(f"{'='*60}")
    print(f"\n等待连接...")
    
    # 兼容不同版本的 websockets
    try:
        # websockets >= 10.0
        async with websockets.serve(handle_client, host, port):
            await asyncio.Future()  # 永久运行
    except TypeError:
        # websockets < 10.0
        server = await websockets.serve(handle_client, host, port)
        await server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description="模拟导航 Action 服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9090, help="监听端口")
    parser.add_argument("--duration", type=float, default=10.0, 
                        help="模拟导航持续时间（秒）")
    parser.add_argument("--feedback-interval", type=float, default=1.0,
                        help="反馈发送间隔（秒）")
    parser.add_argument("--failure-rate", type=float, default=0.0,
                        help="模拟失败概率（0-1）")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="减少输出")
    args = parser.parse_args()
    
    # 配置
    config.navigation_duration = args.duration
    config.feedback_interval = args.feedback_interval
    config.failure_probability = args.failure_rate
    config.verbose = not args.quiet
    
    try:
        asyncio.run(start_server(args.host, args.port))
    except KeyboardInterrupt:
        print("\n\n⏹ 服务器已停止")


if __name__ == "__main__":
    main()
