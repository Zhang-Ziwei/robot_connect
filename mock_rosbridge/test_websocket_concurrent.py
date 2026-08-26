#!/usr/bin/env python3
"""
WebSocket并发连接测试
测试多客户端同时连接的场景

使用方法:
    python test_websocket_concurrent.py [host] [port] [client_count]
    
示例:
    python test_websocket_concurrent.py localhost 8091 5
"""

import asyncio
import json
import sys
from datetime import datetime

try:
    import websockets
except ImportError:
    print("错误: 需要安装 websockets 模块")
    print("请运行: pip install websockets")
    sys.exit(1)


async def client_task(client_num: int, uri: str, commands_to_send: int = 3):
    """单个客户端任务"""
    client_name = f"Client-{client_num:02d}"
    
    try:
        async with websockets.connect(uri) as ws:
            # 接收欢迎消息
            welcome = await ws.recv()
            welcome_data = json.loads(welcome)
            client_id = welcome_data.get("client_id", "unknown")
            print(f"[{client_name}] 连接成功, ID: {client_id}")
            
            # 注册自定义ID
            register_msg = {"type": "register", "client_id": f"test-{client_num:02d}"}
            await ws.send(json.dumps(register_msg))
            response = await ws.recv()
            print(f"[{client_name}] 注册响应: {json.loads(response).get('client_id')}")
            
            # 发送多个命令
            for i in range(commands_to_send):
                await asyncio.sleep(0.5)  # 间隔发送
                
                command = {
                    "cmd_type": "GET_TASK_STATE",
                    "cmd_id": f"{client_name}-cmd-{i+1}",
                    "params": {}
                }
                
                await ws.send(json.dumps(command))
                response = await ws.recv()
                resp_data = json.loads(response)
                print(f"[{client_name}] 命令 {i+1} 响应: success={resp_data.get('success')}")
            
            # 发送心跳
            await ws.send(json.dumps({"type": "ping"}))
            pong = await ws.recv()
            print(f"[{client_name}] 心跳响应: {json.loads(pong).get('type')}")
            
            print(f"[{client_name}] 测试完成")
            
    except Exception as e:
        print(f"[{client_name}] 错误: {e}")


async def run_concurrent_test(host: str, port: int, client_count: int):
    """运行并发测试"""
    uri = f"ws://{host}:{port}"
    
    print("="*60)
    print(f"WebSocket 并发测试")
    print("="*60)
    print(f"服务器: {uri}")
    print(f"客户端数量: {client_count}")
    print("="*60)
    print()
    
    start_time = datetime.now()
    
    # 创建所有客户端任务
    tasks = [client_task(i+1, uri) for i in range(client_count)]
    
    # 并发执行
    await asyncio.gather(*tasks, return_exceptions=True)
    
    elapsed = (datetime.now() - start_time).total_seconds()
    
    print()
    print("="*60)
    print(f"测试完成!")
    print(f"总耗时: {elapsed:.2f} 秒")
    print("="*60)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8091
    client_count = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    
    asyncio.run(run_concurrent_test(host, port, client_count))


if __name__ == "__main__":
    main()
