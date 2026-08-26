#!/usr/bin/env python3
"""
WebSocket客户端测试程序
用于测试 robot_connect 的 WebSocket 服务器功能

使用方法:
    python test_websocket_client.py [host] [port] [command_file]
    
示例:
    # 交互模式
    python test_websocket_client.py localhost 8091
    
    # 直接发送命令文件
    python test_websocket_client.py localhost 8091 SCAN_QR_CODE_command.json
    python test_websocket_client.py 172.16.10.100 8091 ROBOT_ACTION_command
    
支持的命令文件:
    位于 test_commands 文件夹中的所有 .json 文件
    
注意:
    服务器会自动分配配置文件中的默认 Client ID (default_client_id)
    如果需要使用自定义ID，可以在交互模式下发送 register 消息
"""

import asyncio
import json
import sys
import os
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("错误: 需要安装 websockets 模块")
    print("请运行: pip install websockets")
    sys.exit(1)

# 获取test_commands文件夹路径
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
TEST_COMMANDS_DIR = PROJECT_ROOT / "test_commands"


class WebSocketTestClient:
    """WebSocket测试客户端"""
    
    def __init__(self, host: str = "localhost", port: int = 8091):
        self.host = host
        self.port = port
        self.uri = f"ws://{host}:{port}"
        self.websocket = None
        self.client_id = None
        self.running = False
    
    async def connect(self):
        """连接到WebSocket服务器"""
        print(f"\n{'='*60}")
        print(f"正在连接到 {self.uri} ...")
        print(f"{'='*60}")
        
        try:
            self.websocket = await websockets.connect(self.uri)
            self.running = True
            
            # 等待欢迎消息
            welcome = await self.websocket.recv()
            welcome_data = json.loads(welcome)
            
            if welcome_data.get("type") == "connected":
                self.client_id = welcome_data.get("client_id")
                print(f"\n✓ 连接成功!")
                print(f"  Client ID: {self.client_id}")
                print(f"  服务器时间: {welcome_data.get('server_time')}")
            else:
                print(f"收到消息: {welcome}")
            
            return True
            
        except Exception as e:
            print(f"\n✗ 连接失败: {e}")
            return False
    
    async def send_command(self, cmd_type: str, cmd_id: str = None, params: dict = None):
        """发送命令"""
        if not self.websocket:
            print("错误: 未连接到服务器")
            return None
        
        cmd_id = cmd_id or f"test-{datetime.now().strftime('%H%M%S')}"
        
        command = {
            "cmd_type": cmd_type,
            "cmd_id": cmd_id,
            "params": params or {}
        }
        
        print(f"\n>>> 发送命令: {cmd_type}")
        print(f"    数据: {json.dumps(command, ensure_ascii=False, indent=2)}")
        
        await self.websocket.send(json.dumps(command, ensure_ascii=False))
        
        # 等待响应
        response = await self.websocket.recv()
        response_data = json.loads(response)
        
        print(f"\n<<< 收到响应:")
        print(f"    成功: {response_data.get('success')}")
        print(f"    代码: {response_data.get('code')}")
        print(f"    消息: {response_data.get('message')}")
        if response_data.get('data'):
            print(f"    数据: {json.dumps(response_data.get('data'), ensure_ascii=False, indent=2)}")
        
        return response_data
    
    async def send_ping(self):
        """发送心跳"""
        if not self.websocket:
            return
        
        ping_msg = {"type": "ping"}
        print("\n>>> 发送心跳 ping...")
        await self.websocket.send(json.dumps(ping_msg))
        
        response = await self.websocket.recv()
        response_data = json.loads(response)
        print(f"<<< 收到响应: {response_data}")
    
    async def register_custom_id(self, custom_id: str):
        """注册自定义Client ID"""
        if not self.websocket:
            return
        
        register_msg = {
            "type": "register",
            "client_id": custom_id
        }
        print(f"\n>>> 注册自定义ID: {custom_id}")
        await self.websocket.send(json.dumps(register_msg, ensure_ascii=False))
        
        response = await self.websocket.recv()
        response_data = json.loads(response)
        
        if response_data.get("type") == "registered":
            self.client_id = response_data.get("client_id")
            print(f"✓ 注册成功, 新ID: {self.client_id}")
        else:
            print(f"注册响应: {response_data}")
    
    def list_command_files(self) -> list:
        """列出test_commands文件夹中的JSON文件"""
        if not TEST_COMMANDS_DIR.exists():
            print(f"警告: test_commands 文件夹不存在: {TEST_COMMANDS_DIR}")
            return []
        
        json_files = sorted(TEST_COMMANDS_DIR.glob("*.json"))
        return json_files
    
    def load_command_file(self, filepath: Path) -> dict:
        """加载JSON命令文件"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载文件失败: {e}")
            return None
    
    async def send_from_file(self, filepath: Path):
        """从JSON文件发送命令"""
        if not self.websocket:
            print("错误: 未连接到服务器")
            return None
        
        command = self.load_command_file(filepath)
        if not command:
            return None
        
        print(f"\n>>> 从文件发送命令: {filepath.name}")
        print(f"    数据: {json.dumps(command, ensure_ascii=False, indent=2)}")
        
        await self.websocket.send(json.dumps(command, ensure_ascii=False))
        
        # 等待响应
        response = await self.websocket.recv()
        response_data = json.loads(response)
        
        print(f"\n<<< 收到响应:")
        print(f"    成功: {response_data.get('success')}")
        print(f"    代码: {response_data.get('code')}")
        print(f"    消息: {response_data.get('message')}")
        if response_data.get('data'):
            print(f"    数据: {json.dumps(response_data.get('data'), ensure_ascii=False, indent=2)}")
        
        return response_data
    
    def show_command_files_menu(self) -> Path:
        """显示命令文件菜单并返回选择的文件"""
        json_files = self.list_command_files()
        
        if not json_files:
            print("没有找到命令文件")
            return None
        
        print("\n" + "-"*50)
        print("可用的命令文件:")
        print("-"*50)
        
        for i, f in enumerate(json_files, 1):
            # 尝试读取文件获取cmd_type
            try:
                with open(f, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    cmd_type = data.get('cmd_type', '未知')
                    print(f"  {i:2}. {f.name:<45} [{cmd_type}]")
            except:
                print(f"  {i:2}. {f.name}")
        
        print("-"*50)
        print(f"  0. 返回上级菜单")
        print("-"*50)
        
        try:
            choice = input("请选择文件编号: ").strip()
            if choice == "0" or choice == "":
                return None
            
            idx = int(choice) - 1
            if 0 <= idx < len(json_files):
                return json_files[idx]
            else:
                print("无效的选择")
                return None
        except ValueError:
            print("请输入有效的数字")
            return None
    
    async def close(self):
        """关闭连接"""
        if self.websocket:
            await self.websocket.close()
            print("\n✓ 连接已关闭")
    
    async def interactive_mode(self):
        """交互模式"""
        print("\n" + "="*60)
        print("进入交互模式")
        print("="*60)
        print("可用命令:")
        print("  1. ping        - 发送心跳")
        print("  2. register    - 注册自定义ID")
        print("  3. status      - 查询系统状态 (GET_TASK_STATE)")
        print("  4. scan        - 发送扫码命令 (SCAN_QRCODE)")
        print("  5. bottle      - 查询瓶子状态 (BOTTLE_GET)")
        print("  6. custom      - 发送自定义命令")
        print("  7. file        - 从 test_commands 文件夹发送命令 ★")
        print("  8. list        - 列出所有可用命令文件")
        print("  9. quit        - 退出")
        print("="*60)
        
        while self.running:
            try:
                cmd = input("\n请输入命令 > ").strip().lower()
                
                if cmd == "1" or cmd == "ping":
                    await self.send_ping()
                
                elif cmd == "2" or cmd == "register":
                    custom_id = input("请输入自定义ID: ").strip()
                    if custom_id:
                        await self.register_custom_id(custom_id)
                
                elif cmd == "3" or cmd == "status":
                    task_id = input("请输入任务ID (留空查询当前): ").strip()
                    await self.send_command("GET_TASK_STATE", params={"task_id": task_id} if task_id else {})
                
                elif cmd == "4" or cmd == "scan":
                    robot_id = input("请输入机器人ID [robot_a]: ").strip() or "robot_a"
                    await self.send_command("SCAN_QRCODE", params={"robot_id": robot_id})
                
                elif cmd == "5" or cmd == "bottle":
                    await self.send_command("BOTTLE_GET")
                
                elif cmd == "6" or cmd == "custom":
                    cmd_type = input("命令类型 (cmd_type): ").strip()
                    params_str = input("参数 (JSON格式，留空为{}): ").strip()
                    params = json.loads(params_str) if params_str else {}
                    await self.send_command(cmd_type, params=params)
                
                elif cmd == "7" or cmd == "file":
                    filepath = self.show_command_files_menu()
                    if filepath:
                        await self.send_from_file(filepath)
                
                elif cmd == "8" or cmd == "list":
                    json_files = self.list_command_files()
                    if json_files:
                        print(f"\ntest_commands 文件夹路径: {TEST_COMMANDS_DIR}")
                        print(f"共 {len(json_files)} 个命令文件:")
                        for f in json_files:
                            print(f"  - {f.name}")
                
                elif cmd == "9" or cmd == "quit" or cmd == "exit" or cmd == "q":
                    self.running = False
                    break
                
                else:
                    print("未知命令，请重新输入")
                    
            except KeyboardInterrupt:
                print("\n收到中断信号")
                break
            except json.JSONDecodeError as e:
                print(f"JSON解析错误: {e}")
            except websockets.exceptions.ConnectionClosed:
                print("连接已断开")
                break
            except Exception as e:
                print(f"错误: {e}")


async def run_basic_test(host: str, port: int):
    """运行基本测试"""
    client = WebSocketTestClient(host, port)
    
    if not await client.connect():
        return
    
    try:
        # 测试心跳
        await client.send_ping()
        
        # 测试注册
        await client.register_custom_id("test-client-001")
        
        # 测试发送命令
        await client.send_command("GET_TASK_STATE", cmd_id="test-001")
        
        # 测试瓶子查询
        await client.send_command("BOTTLE_GET", cmd_id="test-002")
        
        print("\n" + "="*60)
        print("基本测试完成!")
        print("="*60)
        
    finally:
        await client.close()


async def run_interactive(host: str, port: int):
    """运行交互模式"""
    client = WebSocketTestClient(host, port)
    
    if not await client.connect():
        return
    
    try:
        await client.interactive_mode()
    finally:
        await client.close()


async def run_send_file(host: str, port: int, filename: str):
    """发送指定文件的命令"""
    client = WebSocketTestClient(host, port)
    
    # 查找文件
    filepath = TEST_COMMANDS_DIR / filename
    if not filepath.exists():
        # 尝试添加.json后缀
        filepath = TEST_COMMANDS_DIR / f"{filename}.json"
    
    if not filepath.exists():
        print(f"错误: 找不到命令文件: {filename}")
        print(f"搜索路径: {TEST_COMMANDS_DIR}")
        return
    
    if not await client.connect():
        return
    
    try:
        await client.send_from_file(filepath)
    finally:
        await client.close()


def main():
    """主函数"""
    # 解析命令行参数
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8091
    cmd_file = sys.argv[3] if len(sys.argv) > 3 else None
    
    print("="*60)
    print("WebSocket 客户端测试程序")
    print("="*60)
    print(f"目标服务器: ws://{host}:{port}")
    
    # 如果指定了命令文件，直接发送
    if cmd_file:
        print(f"命令文件: {cmd_file}")
        asyncio.run(run_send_file(host, port, cmd_file))
        return
    
    print()
    print("请选择测试模式:")
    print("  1. 基本测试 (自动运行一系列测试)")
    print("  2. 交互模式 (手动输入命令)")
    print("  3. 发送命令文件 (从 test_commands 选择)")
    print()
    
    choice = input("请选择 [1/2/3, 默认2]: ").strip() or "2"
    
    if choice == "1":
        asyncio.run(run_basic_test(host, port))
    elif choice == "3":
        # 显示文件列表并选择
        client = WebSocketTestClient(host, port)
        filepath = client.show_command_files_menu()
        if filepath:
            asyncio.run(run_send_file(host, port, filepath.name))
    else:
        asyncio.run(run_interactive(host, port))


if __name__ == "__main__":
    main()
