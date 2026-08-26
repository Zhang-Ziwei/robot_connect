#!/usr/bin/env python3
"""
WebSocket客户端测试脚本

用于测试 websocket_client.py 模块的功能：
1. 连接到外部WebSocket服务器
2. 使用Client ID方式认证
3. 发送和接收消息

使用方法：
1. 确保目标WebSocket服务器正在运行
2. 修改下方的配置参数
3. 运行: python websocket_client_test.py
"""

import json
from re import T
import time
import asyncio
import ssl
from datetime import datetime

try:
    import websockets
except ImportError:
    print("请先安装 websockets: pip install websockets")
    exit(1)

# ============ 配置区域 ============
# 目标WebSocket服务器地址
TARGET_HOST = "0.0.0.0"
TARGET_PORT = 8091

# 是否使用SSL（wss://）
USE_SSL = False

# 客户端ID（发送给服务器的标识）
CLIENT_ID = "8848"

# 是否发送测试命令
SEND_TEST_COMMANDS = False
# =================================


class WebSocketClientTester:
    """WebSocket客户端测试器"""
    
    def __init__(self):
        self.websocket = None
        self.running = True
    
    @property
    def uri(self) -> str:
        protocol = "wss" if USE_SSL else "ws"
        return f"{protocol}://{TARGET_HOST}:{TARGET_PORT}"
    
    async def connect(self):
        """连接到服务器"""
        print(f"\n{'='*60}")
        print(f"🔗 正在连接: {self.uri}")
        print(f"{'='*60}")
        
        ssl_context = None
        if USE_SSL:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            print("   使用SSL加密连接")
        
        try:
            self.websocket = await websockets.connect(
                self.uri,
                ssl=ssl_context,
                ping_interval=30,
                ping_timeout=60
            )
            print(f"✓ 连接成功！")
            
            # 主动发送注册消息，告知服务器我们的client_id
            register_msg = {
                "type": "register",
                "client_id": CLIENT_ID,
                "timestamp": datetime.now().isoformat()
            }
            await self.websocket.send(json.dumps(register_msg, ensure_ascii=False))
            print(f"\n📤 已发送注册消息:")
            print(json.dumps(register_msg, ensure_ascii=False, indent=2))
            print(f"\n✓ 已注册 Client ID: {CLIENT_ID}")
            
            # 等待服务器确认（可选）
            try:
                ack_msg = await asyncio.wait_for(self.websocket.recv(), timeout=5)
                ack_data = json.loads(ack_msg)
                print(f"\n📨 收到服务端响应:")
                print(json.dumps(ack_data, ensure_ascii=False, indent=2))
                
                if ack_data.get("type") == "register_ack":
                    print(f"✓ 服务端确认: {ack_data.get('message', 'OK')}")
            except asyncio.TimeoutError:
                print("ℹ️  服务端未发送确认（正常，部分服务器不回复）")
            except json.JSONDecodeError:
                print(f"⚠️  响应不是JSON格式: {ack_msg}")
            
            return True
            
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return False
    
    async def send_message(self, data: dict):
        """发送消息"""
        if not self.websocket:
            print("❌ 未连接")
            return
        
        # 自动添加client_id（使用我们主动注册的ID）
        if "client_id" not in data:
            data["client_id"] = CLIENT_ID
        
        message = json.dumps(data, ensure_ascii=False)
        print(f"\n📤 发送消息:")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        
        await self.websocket.send(message)
    
    async def receive_messages(self):
        """接收消息循环"""
        try:
            async for message in self.websocket:
                timestamp = datetime.now().strftime("%H:%M:%S")
                try:
                    data = json.loads(message)
                    print(f"\n[{timestamp}] 📨 收到消息:")
                    print(json.dumps(data, ensure_ascii=False, indent=2))
                except json.JSONDecodeError:
                    print(f"\n[{timestamp}] 📨 收到原始消息: {message}")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"\n⚠️  连接关闭: code={e.code}, reason={e.reason}")
        except Exception as e:
            print(f"\n❌ 接收消息错误: {e}")
    
    async def send_test_commands(self):
        """发送测试命令"""
        await asyncio.sleep(1)  # 等待1秒
        
        # 发送测试命令列表
        test_commands = [
            {
                "cmd_type": "GET_TASK_STATE",
                "cmd_id": f"test_{int(time.time())}",
                "params": {}
            },
            {
                "cmd_type": "GET_STATION_COUNTER",
                "cmd_id": f"counter_{int(time.time())}",
                "params": {}
            }
        ]
        
        for cmd in test_commands:
            await self.send_message(cmd)
            await asyncio.sleep(2)  # 等待响应
    
    async def run(self):
        """运行测试"""
        if not await self.connect():
            return
        
        tasks = [self.receive_messages()]
        
        if SEND_TEST_COMMANDS:
            tasks.append(self.send_test_commands())
        
        print("\n" + "="*60)
        print("📡 开始监听消息... (按Ctrl+C退出)")
        print("="*60)
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            if self.websocket:
                await self.websocket.close()
                print("\n⏹️  连接已关闭")


async def interactive_mode():
    """交互式模式"""
    tester = WebSocketClientTester()
    
    if not await tester.connect():
        return
    
    # 启动接收任务
    receive_task = asyncio.create_task(tester.receive_messages())
    
    print("\n" + "="*60)
    print("交互模式 - 输入命令发送到服务器")
    print("  输入 'quit' 退出")
    print("  输入 'status' 发送状态查询")
    print("  输入 JSON 直接发送")
    print("="*60)
    
    try:
        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, input, "\n输入命令> "
                )
                
                if user_input.lower() == 'quit':
                    break
                elif user_input.lower() == 'status':
                    await tester.send_message({
                        "cmd_type": "GET_TASK_STATE",
                        "cmd_id": f"status_{int(time.time())}",
                        "params": {}
                    })
                else:
                    try:
                        data = json.loads(user_input)
                        await tester.send_message(data)
                    except json.JSONDecodeError:
                        print("⚠️  无效的JSON格式，请重新输入")
            except EOFError:
                break
    finally:
        receive_task.cancel()
        if tester.websocket:
            await tester.websocket.close()


def main():
    print("="*60)
    print("   WebSocket客户端测试工具")
    print("="*60)
    print(f"\n目标服务器: {('wss' if USE_SSL else 'ws')}://{TARGET_HOST}:{TARGET_PORT}")
    print(f"Client ID: {CLIENT_ID}")
    print(f"自动发送测试命令: {'是' if SEND_TEST_COMMANDS else '否'}")
    
    tester = WebSocketClientTester()
    
    try:
        asyncio.get_event_loop().run_until_complete(tester.run())
    except KeyboardInterrupt:
        print("\n\n用户中断，退出...")


if __name__ == "__main__":
    main()
