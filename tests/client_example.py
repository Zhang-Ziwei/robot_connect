#!/usr/bin/env python3
"""
HTTP客户端示例 - 对方电脑使用
用于向机器人控制系统发送命令并获取结果

使用方法：
    python client_example.py
"""

import requests
import time
import json
import sys

# 配置服务器地址（修改为实际IP和端口）
SERVER_URL = "http://172.16.11.130:8090"

def send_command(command_data):
    """
    发送命令到服务器
    
    Args:
        command_data: 命令数据（字典或JSON文件路径）
    
    Returns:
        task_id: 任务ID，用于后续查询
    """
    # 如果是文件路径，读取文件
    if isinstance(command_data, str) and command_data.endswith('.json'):
        print(f"读取命令文件: {command_data}")
        with open(command_data, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = command_data
    
    print(f"\n发送命令: {data.get('cmd_type')}")
    print(f"命令ID: {data.get('cmd_id')}")
    
    try:
        response = requests.post(
            SERVER_URL,
            headers={'Content-Type': 'application/json'},
            json=data,
            timeout=10  # 10秒超时（足够了，因为是异步）
        )
        
        result = response.json()
        
        if result.get('success'):
            task_id = result.get('task_id')
            queue_size = result.get('queue_size', 0)
            print(f"✓ 任务已提交成功")
            print(f"  Task ID: {task_id}")
            print(f"  队列中任务数: {queue_size}")
            return task_id
        else:
            print(f"✗ 提交失败: {result.get('message')}")
            return None
            
    except requests.exceptions.Timeout:
        print("✗ 连接超时，请检查网络和服务器状态")
        return None
    except requests.exceptions.ConnectionError:
        print(f"✗ 无法连接到服务器: {SERVER_URL}")
        print("  请检查：")
        print("  1. 服务器IP和端口是否正确")
        print("  2. 服务器程序是否正在运行")
        print("  3. 网络是否连通（ping测试）")
        return None
    except Exception as e:
        print(f"✗ 发送命令出错: {e}")
        return None


def get_task_status(task_id):
    """
    查询任务状态
    
    Args:
        task_id: 任务ID
    
    Returns:
        任务信息字典
    """
    try:
        response = requests.get(
            f"{SERVER_URL}/task/{task_id}",
            timeout=5
        )
        return response.json()
    except Exception as e:
        print(f"✗ 查询状态出错: {e}")
        return None


def wait_for_task(task_id, timeout=600, interval=2, show_progress=True):
    """
    等待任务完成（轮询）
    
    Args:
        task_id: 任务ID
        timeout: 超时时间（秒）
        interval: 查询间隔（秒）
        show_progress: 是否显示进度
    
    Returns:
        任务结果
    """
    print(f"\n等待任务完成...")
    print(f"  每 {interval} 秒查询一次状态")
    print(f"  最长等待 {timeout} 秒")
    print(f"  按 Ctrl+C 可以停止等待（不影响任务执行）\n")
    
    start_time = time.time()
    check_count = 0
    
    try:
        while time.time() - start_time < timeout:
            check_count += 1
            task_info = get_task_status(task_id)
            
            if not task_info:
                print("⚠ 无法获取任务状态，继续等待...")
                time.sleep(interval)
                continue
            
            status = task_info.get('status')
            cmd_type = task_info.get('cmd_type', 'Unknown')
            
            if show_progress:
                elapsed = int(time.time() - start_time)
                print(f"[{elapsed}s] 第{check_count}次查询 - 状态: {status} ({cmd_type})")
            
            if status == 'completed':
                print("\n✓ 任务完成!")
                result = task_info.get('result')
                
                # 显示结果摘要
                if result:
                    print(f"  成功: {result.get('success')}")
                    print(f"  消息: {result.get('message')}")
                    
                    # 显示特定命令的结果
                    if 'scanned_count' in result:
                        print(f"  扫描数量: {result.get('scanned_count')}")
                
                return result
                
            elif status == 'failed':
                print("\n✗ 任务失败!")
                result = task_info.get('result')
                if result:
                    print(f"  错误信息: {result.get('message')}")
                return result
            
            time.sleep(interval)
        
        print(f"\n✗ 等待超时（{timeout}秒）")
        print("  任务可能仍在执行中，可以稍后查询:")
        print(f"  curl {SERVER_URL}/task/{task_id}")
        return None
        
    except KeyboardInterrupt:
        print("\n\n⚠ 用户中断等待")
        print("  注意：任务仍在服务器上继续执行")
        print(f"  可以随时查询状态: curl {SERVER_URL}/task/{task_id}")
        return None


def get_queue_status():
    """查询队列整体状态"""
    try:
        response = requests.get(f"{SERVER_URL}/queue/status", timeout=5)
        queue_info = response.json()
        
        print("\n队列状态:")
        print(f"  当前队列中任务数: {queue_info.get('queue_size', 0)}")
        print(f"  总任务数: {queue_info.get('total_tasks', 0)}")
        print(f"  已完成: {queue_info.get('completed_tasks', 0)}")
        print(f"  失败: {queue_info.get('failed_tasks', 0)}")
        
        running_task = queue_info.get('running_task')
        if running_task:
            print(f"  正在执行: {running_task}")
        
        return queue_info
    except Exception as e:
        print(f"✗ 查询队列状态出错: {e}")
        return None


def test_connection():
    """测试服务器连接"""
    print(f"测试连接到服务器: {SERVER_URL}")
    try:
        response = requests.get(f"{SERVER_URL}/", timeout=3)
        info = response.json()
        print(f"✓ 连接成功")
        print(f"  服务器状态: {info.get('status')}")
        print(f"  队列模式: {info.get('queue_mode')}")
        return True
    except Exception as e:
        print(f"✗ 连接失败: {e}")
        return False


# ============================================================================
# 使用示例
# ============================================================================

def example_scan_qrcode():
    """示例：发送SCAN_QRCODE命令"""
    command = {
        "header": {
            "seq": 1,
            "stamp": {"secs": int(time.time()), "nsecs": 0},
            "frame_id": "camera_link"
        },
        "cmd_id": "SCAN_QR_CODE_001",
        "cmd_type": "SCAN_QRCODE",
        "params": {
            "scan_mode": "hand_scanner",
            "target_type": "bottle_label",
            "timeout": 5.0
        },
        "extra": {}
    }
    
    # 发送命令
    task_id = send_command(command)
    
    if task_id:
        # 等待完成
        result = wait_for_task(task_id, timeout=600, interval=3)
        return result
    
    return None


def example_from_file():
    """示例：从JSON文件发送命令"""
    json_file = "test_commands/SCAN_QR_CODE_command.json"
    
    # 发送命令
    task_id = send_command(json_file)
    
    if task_id:
        # 等待完成
        result = wait_for_task(task_id)
        return result
    
    return None


def main():
    """主函数"""
    print("="*70)
    print("机器人控制系统 - HTTP客户端")
    print("="*70)
    print(f"服务器地址: {SERVER_URL}")
    print("="*70)
    
    # 测试连接
    if not test_connection():
        print("\n请检查服务器配置后重试")
        sys.exit(1)
    
    print("\n选择操作:")
    print("  1. 发送SCAN_QRCODE命令（使用代码）")
    print("  2. 从JSON文件发送命令")
    print("  3. 查询队列状态")
    print("  4. 查询指定任务状态")
    print("  0. 退出")
    
    choice = input("\n请选择 [1]: ").strip() or "1"
    
    if choice == "1":
        result = example_scan_qrcode()
        if result:
            print("\n最终结果:")
            print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif choice == "2":
        json_file = input("输入JSON文件路径 [test_commands/SCAN_QR_CODE_command.json]: ").strip()
        if not json_file:
            json_file = "test_commands/SCAN_QR_CODE_command.json"
        
        result = example_from_file()
        if result:
            print("\n最终结果:")
            print(json.dumps(result, ensure_ascii=False, indent=2))
    
    elif choice == "3":
        get_queue_status()
    
    elif choice == "4":
        task_id = input("输入Task ID: ").strip()
        if task_id:
            task_info = get_task_status(task_id)
            if task_info:
                print("\n任务信息:")
                print(json.dumps(task_info, ensure_ascii=False, indent=2))
    
    elif choice == "0":
        print("退出")
    
    else:
        print("无效选择")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程序已退出")
    except Exception as e:
        print(f"\n程序出错: {e}")
        import traceback
        traceback.print_exc()

