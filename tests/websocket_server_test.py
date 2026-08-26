"""
WebSocket 客户端测试程序

演示如何：
1. 连接到 Robot Connect 的 WebSocket 服务器
2. 接收服务端分配的 client_id
3. 使用 client_id 发送命令
4. 支持 ws:// 和 wss:// 协议
5. 心跳保活机制

使用方法：
    python websocket_test.py
"""

import asyncio
import websockets
import json
import ssl
from datetime import datetime

# ============ 配置区域 ============
# 
# WebSocket 服务器地址配置说明：
# ┌─────────────────────────────────────────────────────────┐
# │ 测试场景                │ WS_HOST 应设置为              │
# ├─────────────────────────┼───────────────────────────────┤
# │ 在服务器本机测试        │ "127.0.0.1" 或 "localhost"    │
# │ 从其他机器远程测试      │ 服务器的实际IP（如robot_config│
# │                        │ .json中websocket_server.host）│
# └─────────────────────────────────────────────────────────┘
#
# 当前 robot_config.json 中的服务器配置:
#   "websocket_server": {
#       "host": "172.16.8.162",  <- 服务器绑定的IP
#       "port": 8091
#   }
#
WS_HOST = "127.0.0.1"  # 本机测试用 127.0.0.1，远程测试改为服务器IP
WS_PORT = 8091

# 是否使用 SSL/TLS (wss://)
# 
# ⚠️ 重要：只有当服务端同时满足以下条件时才设为 True:
#    1. robot_config.json 中 ssl_enabled = true
#    2. ssl_cert_file 配置了有效的证书文件路径（不是null）
#    3. ssl_key_file 配置了有效的密钥文件路径（不是null）
#
# 如果服务端 ssl_cert_file 或 ssl_key_file 为 null，
# 即使 ssl_enabled=true，服务端实际运行的仍是 ws:// 而非 wss://
# 此时客户端必须设置 USE_SSL = False，否则会报错:
#    "[SSL: WRONG_VERSION_NUMBER] wrong version number"
#
USE_SSL = False  # 服务端 ssl_enabled=false，使用 ws://

# 心跳间隔（秒），设为0禁用心跳
HEARTBEAT_INTERVAL = 30
# ==================================


class WebSocketClient:
    """WebSocket 客户端封装类"""
    
    def __init__(self, host: str, port: int, use_ssl: bool = False):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.websocket = None
        self.client_id = None  # 服务端分配的 client_id
        self.connected = False
        self._heartbeat_task = None
    
    @property
    def uri(self) -> str:
        """获取 WebSocket URI"""
        protocol = "wss" if self.use_ssl else "ws"
        return f"{protocol}://{self.host}:{self.port}"
    
    async def connect(self):
        """建立 WebSocket 连接"""
        print(f"\n{'='*60}")
        print(f"正在连接 WebSocket 服务器: {self.uri}")
        print(f"{'='*60}")
        
        try:
            # SSL 配置
            ssl_context = None
            if self.use_ssl:
                ssl_context = ssl.create_default_context()
                # 如果是自签名证书，可以禁用验证（仅测试用）
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            
            # 建立连接
            # ping_interval/ping_timeout 设大一些，避免在等待命令响应（如 ROS topic 查询）
            # 期间被 ping 超时误判为断线（默认 20/30 秒，查询 topic 可能需要更长时间）
            self.websocket = await websockets.connect(
                self.uri,
                ssl=ssl_context,
                ping_interval=60,
                ping_timeout=120
            )
            
            # 接收欢迎消息，获取 client_id
            welcome_msg = await self.websocket.recv()
            welcome_data = json.loads(welcome_msg)
            
            print(f"\n✓ 连接成功!")
            print(f"  服务端响应: {json.dumps(welcome_data, ensure_ascii=False, indent=2)}")
            
            # 提取 client_id
            if welcome_data.get("type") == "connected":
                self.client_id = welcome_data.get("client_id")
                print(f"\n{'*'*60}")
                print(f"  ★ 分配的 Client ID: {self.client_id}")
                print(f"{'*'*60}")
            
            self.connected = True
            
            # 启动心跳任务
            if HEARTBEAT_INTERVAL > 0:
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            return True
            
        except Exception as e:
            print(f"\n✗ 连接失败: {e}")
            return False
    
    async def _heartbeat_loop(self):
        """心跳保活循环"""
        while self.connected:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self.connected and self.websocket:
                    ping_msg = {"type": "ping", "client_id": self.client_id}
                    await self.websocket.send(json.dumps(ping_msg))
                    print(f"  [心跳] 发送 ping (client_id={self.client_id})")
            except Exception as e:
                print(f"  [心跳] 错误: {e}")
                break
    
    async def send_command(self, cmd_type: str, cmd_id: str = None, params: dict = None) -> dict:
        """
        发送命令并等待响应
        
        参数:
            cmd_type: 命令类型 (如 "GET_TASK_STATE", "SCAN_QRCODE" 等)
            cmd_id: 命令ID (可选，自动生成)
            params: 命令参数 (可选)
        
        返回:
            服务端响应的字典
        """
        if not self.connected or not self.websocket:
            raise Exception("WebSocket 未连接")
        
        # 构造命令
        if cmd_id is None:
            cmd_id = f"{cmd_type.lower()}_{datetime.now().strftime('%H%M%S')}"
        
        command = {
            "cmd_type": cmd_type,
            "cmd_id": cmd_id,
            "params": params or {},
            "client_id": self.client_id  # 附带 client_id
        }
        
        print(f"\n>>> 发送命令:")
        print(f"    {json.dumps(command, ensure_ascii=False)}")
        
        # 发送命令
        await self.websocket.send(json.dumps(command, ensure_ascii=False))
        
        # 循环接收消息，直到收到匹配 cmd_id 的命令响应
        # 中间可能夹杂心跳 pong 等应用层消息，需跳过
        deadline = asyncio.get_event_loop().time() + 60  # 总超时 60 秒
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise Exception(f"等待服务端响应超时（60s），命令: {cmd_type}")
            try:
                response_str = await asyncio.wait_for(
                    self.websocket.recv(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise Exception(f"等待服务端响应超时（60s），命令: {cmd_type}")
            
            response = json.loads(response_str)
            msg_type = response.get("type", "")
            
            # 跳过心跳 pong 和其他非命令响应消息
            if msg_type in ("pong", "ping", "connected", "registered"):
                print(f"  [跳过 {msg_type} 消息，继续等待命令响应]")
                continue
            
            # 检查是否是本次命令的响应（通过 _request_cmd_id 或 cmd_id 匹配）
            resp_cmd_id = response.get("_request_cmd_id") or response.get("cmd_id")
            if resp_cmd_id and resp_cmd_id != cmd_id:
                print(f"  [跳过 cmd_id={resp_cmd_id} 的响应，等待 {cmd_id}]")
                continue
            
            # 匹配到本次命令的响应
            break
        
        print(f"\n<<< 收到响应:")
        print(f"    {json.dumps(response, ensure_ascii=False, indent=2)}")
        
        return response
    
    async def close(self):
        """关闭连接"""
        self.connected = False
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        
        if self.websocket:
            await self.websocket.close()
            print(f"\n✓ 连接已关闭 (client_id={self.client_id})")


async def main():
    """主函数 - 演示 WebSocket 客户端使用"""
    
    # 创建客户端
    client = WebSocketClient(WS_HOST, WS_PORT, USE_SSL)
    
    try:
        # 1. 建立连接（获取 client_id）
        if not await client.connect():
            return
        
        print(f"\n{'='*60}")
        print("开始交互式命令发送（输入 'quit' 退出）")
        print(f"{'='*60}")
        
        # 2. 交互式发送命令
        while True:
            print("\n可用命令:")
            print("  1. GET_TASK_STATE - 查询任务状态")
            print("  2. GET_STATION_COUNTER - 查询工位计数")
            print("  3. GET_UPPER_LIMB_JOINT_STATES - 查询上肢关节")
            print("  4. GET_HAND_JOINT_STATES - 查询手部关节")
            print("  5. GET_FINGER_PRESSURES - 查询手指压力")
            print("  6. GET_ROBOT_MOTION_STATE - 查询运动状态")
            print("  7. 自定义命令")
            print("  q. 退出")
            
            choice = input("\n请选择 (1-7/q): ").strip()
            
            if choice.lower() in ['q', 'quit', 'exit']:
                break
            
            cmd_type = None
            params = {}
            
            if choice == '1':
                cmd_type = "GET_TASK_STATE"
            elif choice == '2':
                cmd_type = "GET_STATION_COUNTER"
            elif choice == '3':
                cmd_type = "GET_UPPER_LIMB_JOINT_STATES"
                params = {"robot_id": "robot_a"}
            elif choice == '4':
                cmd_type = "GET_HAND_JOINT_STATES"
                params = {"robot_id": "robot_a"}
            elif choice == '5':
                cmd_type = "GET_FINGER_PRESSURES"
                params = {"robot_id": "robot_a"}
            elif choice == '6':
                cmd_type = "GET_ROBOT_MOTION_STATE"
                params = {"robot_id": "robot_a"}
            elif choice == '7':
                cmd_type = input("输入命令类型 (cmd_type): ").strip()
                params_str = input("输入参数 JSON (直接回车跳过): ").strip()
                if params_str:
                    try:
                        params = json.loads(params_str)
                    except:
                        print("JSON 解析失败，使用空参数")
                        params = {}
            else:
                print("无效选择")
                continue
            
            if cmd_type:
                try:
                    await client.send_command(cmd_type, params=params)
                except Exception as e:
                    print(f"发送命令失败: {e}")
        
    except KeyboardInterrupt:
        print("\n\n用户中断")
    except Exception as e:
        print(f"\n错误: {e}")
    finally:
        await client.close()


async def quick_test():
    """快速测试 - 连接并发送一个命令"""
    client = WebSocketClient(WS_HOST, WS_PORT, USE_SSL)
    
    try:
        if await client.connect():
            # 发送测试命令
            await client.send_command("GET_TASK_STATE")
    finally:
        await client.close()


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        # 快速测试模式
        print("快速测试模式")
        asyncio.run(quick_test())
    else:
        # 交互式模式
        asyncio.run(main())
