#!/usr/bin/env python3
"""
HTTP命令测试脚本
用于测试HTTP服务器和命令处理功能
"""

import json
import requests
import time
import os

SERVER_URL = "http://localhost:8080"

def test_server_health():
    """测试服务器健康状态"""
    print("="*70)
    print("测试1: 服务器健康检查")
    print("="*70)
    
    try:
        response = requests.get(SERVER_URL, timeout=5)
        print(f"✓ 服务器响应: {response.status_code}")
        print(f"  响应内容: {response.json()}")
        return True
    except requests.exceptions.ConnectionError:
        print("✗ 服务器未运行，请先启动main.py并选择HTTP服务器模式")
        return False
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        return False

def send_command(json_file):
    """发送命令到服务器"""
    if not os.path.exists(json_file):
        print(f"✗ JSON文件不存在: {json_file}")
        return None
    
    with open(json_file, 'r', encoding='utf-8') as f:
        cmd_data = json.load(f)
    
    cmd_type = cmd_data.get("cmd_type")
    cmd_id = cmd_data.get("cmd_id")
    
    print(f"\n发送命令: {cmd_type} (ID: {cmd_id})")
    print(f"  JSON文件: {json_file}")
    
    try:
        response = requests.post(
            SERVER_URL,
            json=cmd_data,
            headers={'Content-Type': 'application/json'},
            timeout=300  # 5分钟超时
        )
        
        print(f"✓ 服务器响应: {response.status_code}")
        result = response.json()
        print(f"  执行结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return result
    
    except requests.exceptions.Timeout:
        print("✗ 请求超时")
        return None
    except Exception as e:
        print(f"✗ 发送失败: {e}")
        return None

def test_pickup_command():
    """测试PICK_UP命令"""
    print("\n" + "="*70)
    print("测试2: PICK_UP命令")
    print("="*70)
    return send_command("test_commands/pickup_command.json")

def test_put_to_command():
    """测试PUT_TO命令"""
    print("\n" + "="*70)
    print("测试3: PUT_TO命令")
    print("="*70)
    return send_command("test_commands/put_to_command.json")

def test_bottle_get_command():
    """测试BOTTLE_GET命令"""
    print("\n" + "="*70)
    print("测试4: BOTTLE_GET命令")
    print("="*70)
    return send_command("test_commands/bottle_get_command.json")

def test_custom_command():
    """测试自定义命令"""
    print("\n" + "="*70)
    print("测试5: 自定义命令")
    print("="*70)
    
    # 自定义BOTTLE_GET命令（查询所有瓶子）
    custom_cmd = {
        "header": {
            "seq": 999,
            "stamp": {"secs": int(time.time()), "nsecs": 0},
            "frame_id": "test"
        },
        "cmd_id": "TEST_GET_ALL",
        "cmd_type": "BOTTLE_GET",
        "params": {},
        "detail_params": True,
        "extra": {}
    }
    
    print(f"发送自定义命令: BOTTLE_GET (查询所有瓶子)")
    
    try:
        response = requests.post(
            SERVER_URL,
            json=custom_cmd,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        
        print(f"✓ 服务器响应: {response.status_code}")
        result = response.json()
        print(f"  执行结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        return result
    
    except Exception as e:
        print(f"✗ 发送失败: {e}")
        return None

def main():
    """主测试函数"""
    print("\n" + "="*70)
    print("HTTP命令测试程序")
    print("="*70)
    print("\n注意: 请先启动main.py并选择HTTP服务器模式(模式1)")
    print("      然后在新终端中运行此测试脚本\n")
    
    input("按 Enter 开始测试...")
    
    # 测试服务器健康
    if not test_server_health():
        print("\n请先启动服务器!")
        return
    
    time.sleep(1)
    
    try:
        # 测试各种命令
        print("\n开始测试命令...")
        
        # 1. 测试BOTTLE_GET（查询所有瓶子）
        test_bottle_get_command()
        time.sleep(2)
        
        # 2. 测试自定义命令
        test_custom_command()
        time.sleep(2)
        
        # 3. 测试PICK_UP（需要机器人连接）
        if input("\n是否测试PICK_UP命令？(需要机器人连接) [y/N]: ").strip().lower() == 'y':
            test_pickup_command()
            time.sleep(2)
        
        # 4. 测试PUT_TO（需要机器人连接）
        if input("\n是否测试PUT_TO命令？(需要机器人连接) [y/N]: ").strip().lower() == 'y':
            test_put_to_command()
        
        print("\n" + "="*70)
        print("测试完成")
        print("="*70)
        
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
    except Exception as e:
        print(f"\n✗ 测试出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

