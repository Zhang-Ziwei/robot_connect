#!/usr/bin/env python3
"""
NavigationStatus Topic 测试脚本
测试订阅/navigation_status topic并实时显示导航状态
"""

import sys
import time
from robot_controller import RobotController
from constants import RobotType

# 导航状态码映射
STATUS_NAMES = {
    0: "NONE (无状态)",
    1: "STANDBY (待机)",
    2: "PLANNING (规划中)",
    3: "RUNNING (运行中)",
    4: "STOPPING (停止中)",
    5: "FINISHED (完成)",
    6: "FAILURE (失败)"
}

def test_navigation_status():
    """测试NavigationStatus topic订阅"""
    
    print("="*70)
    print("NavigationStatus Topic 测试")
    print("="*70)
    print()
    
    # 机器人A连接配置
    ROBOT_A_HOST = "10.114.126.51"
    ROBOT_A_PORT = 9091
    
    print(f"正在连接机器人A...")
    print(f"地址: {ROBOT_A_HOST}:{ROBOT_A_PORT}")
    print()
    
    # 创建机器人控制器
    robot_a = RobotController(
        host=ROBOT_A_HOST,
        port=ROBOT_A_PORT,
        robot_type=RobotType.ROBOT_A,
        max_retry_attempts=3,
        retry_interval=5
    )
    
    # 连接机器人
    if not robot_a.connect():
        print("✗ 连接失败，请检查:")
        print("  1. 机器人IP地址是否正确")
        print("  2. 网络连接是否正常")
        print("  3. ROS bridge是否运行")
        sys.exit(1)
    
    print()
    print("="*70)
    print("开始订阅 /navigation_status topic")
    print("="*70)
    print()
    
    try:
        # 订阅导航状态topic
        success = robot_a.subscribe_topic(
            topic_name="/navigation_status",
            msg_type="navi_types/NavigationStatus",
            throttle_rate=0,
            queue_length=1
        )
        
        if not success:
            print("✗ 订阅失败")
            sys.exit(1)
        
        print("✓ 订阅成功")
        print()
        print("正在监听导航状态... (按Ctrl+C停止)")
        print("-"*70)
        print()
        
        # 持续监听并显示状态
        last_status = None
        message_count = 0
        
        while True:
            # 获取最新消息
            nav_status = robot_a.get_topic_message("/navigation_status", "navi_types/NavigationStatus")
            print(nav_status)
            if nav_status:
                status_code = nav_status.get("state", 0).get("value", 0)
                print(status_code)
                # 只在状态变化时打印
                if status_code != last_status:
                    status_name = STATUS_NAMES.get(status_code, f"UNKNOWN({status_code})")
                    timestamp = time.strftime("%H:%M:%S")
                    message_count += 1
                    
                    # 根据状态选择不同的显示格式
                    if status_code == 6:  # FAILURE
                        print(f"[{timestamp}] #{message_count:03d} ✗ 状态: {status_code} - {status_name}")
                    elif status_code == 5:  # FINISHED
                        print(f"[{timestamp}] #{message_count:03d} ✓ 状态: {status_code} - {status_name}")
                    else:
                        print(f"[{timestamp}] #{message_count:03d} ⚫ 状态: {status_code} - {status_name}")
                    
                    last_status = status_code
            else:
                # 未收到消息（仅在首次显示）
                if last_status is None:
                    print("⏳ 等待首条消息...")
            
            # 检查连接状态
            if not robot_a.is_connected():
                print()
                print("⚠️  连接已断开，尝试重连...")
                
                # 等待重连
                reconnect_count = 0
                while not robot_a.is_connected() and reconnect_count < 10:
                    time.sleep(1)
                    reconnect_count += 1
                
                if robot_a.is_connected():
                    print("✓ 重新连接成功")
                    # 重新订阅
                    robot_a.subscribe_topic(
                        topic_name="/navigation_status",
                        msg_type="NavigationStatus",
                        throttle_rate=0,
                        queue_length=1
                    )
                    print("✓ 重新订阅成功")
                    print()
                else:
                    print("✗ 重连失败")
                    break
            
            # 短暂延迟
            time.sleep(0.5)
    
    except KeyboardInterrupt:
        print()
        print()
        print("="*70)
        print("测试被用户中断")
        print("="*70)
    
    except Exception as e:
        print()
        print(f"✗ 发生错误: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 清理：取消订阅
        print()
        print("正在清理...")
        try:
            robot_a.unsubscribe_topic("/navigation_status")
            print("✓ 已取消订阅")
        except:
            pass
        
        # 关闭连接
        try:
            robot_a.close()
            print("✓ 连接已关闭")
        except:
            pass
        
        print()
        print("="*70)
        print(f"测试结束 - 共收到 {message_count} 次状态变化")
        print("="*70)


def main():
    """主函数"""
    print()
    print("╔═══════════════════════════════════════════════════════════════════╗")
    print("║                                                                   ║")
    print("║          NavigationStatus Topic 测试工具                          ║")
    print("║                                                                   ║")
    print("╚═══════════════════════════════════════════════════════════════════╝")
    print()
    
    # 显示状态码说明
    print("NavigationStatus 状态码说明：")
    print("-"*70)
    for code, name in STATUS_NAMES.items():
        print(f"  {code} = {name}")
    print("-"*70)
    print()
    
    # 提示修改配置
    print("⚠️  注意：请先修改脚本中的机器人IP地址配置")
    print("   在 test_navigation_status.py 中修改：")
    print("   ROBOT_A_HOST = \"192.168.1.100\"  # 修改为实际IP")
    print()
    
    # 询问是否继续
    try:
        response = input("是否继续测试？(y/n): ").strip().lower()
        if response != 'y':
            print("测试已取消")
            sys.exit(0)
    except KeyboardInterrupt:
        print("\n测试已取消")
        sys.exit(0)
    
    print()
    
    # 运行测试
    test_navigation_status()


if __name__ == "__main__":
    main()

