#!/usr/bin/env python3
"""
自动重连功能测试脚本

这个脚本展示了如何使用RobotController的自动重连功能。
你可以在机器人断电/重启的情况下运行此脚本来观察重连行为。
"""

import time
from robot_controller import RobotController
from constants import RobotType

def test_infinite_retry():
    """测试无限重试模式"""
    print("\n" + "="*70)
    print("测试1: 无限重试模式")
    print("="*70)
    print("说明：程序会一直尝试连接，直到成功或手动中断(Ctrl+C)")
    print()
    
    robot = RobotController(
        "192.168.217.100",
        "9091",
        RobotType.ROBOT_A,
        max_retry_attempts=None,  # 无限重试
        retry_interval=5           # 每5秒重试
    )
    
    print("开始连接...")
    if robot.connect():
        print("\n✓ 连接成功！")
        robot.close()
    else:
        print("\n✗ 连接失败（这在无限重试模式下不应该发生）")

def test_limited_retry():
    """测试有限重试模式"""
    print("\n" + "="*70)
    print("测试2: 有限重试模式")
    print("="*70)
    print("说明：程序最多重试3次，每次间隔2秒")
    print()
    
    robot = RobotController(
        "192.168.217.100",
        "9091",
        RobotType.ROBOT_A,
        max_retry_attempts=3,  # 最多重试3次
        retry_interval=2        # 每2秒重试
    )
    
    print("开始连接...")
    if robot.connect():
        print("\n✓ 连接成功！")
        robot.close()
    else:
        print("\n✗ 达到最大重试次数，连接失败")

def test_auto_reconnect_on_disconnect():
    """测试连接断开后的自动重连"""
    print("\n" + "="*70)
    print("测试3: 连接断开后自动重连")
    print("="*70)
    print("说明：连接成功后，如果发送请求失败，会自动重连")
    print()
    
    robot = RobotController(
        "192.168.217.100",
        "9091",
        RobotType.ROBOT_A,
        max_retry_attempts=None,
        retry_interval=3
    )
    
    print("开始连接...")
    if not robot.connect():
        print("✗ 初始连接失败")
        return
    
    print("\n✓ 初始连接成功！")
    print("\n现在尝试发送请求...")
    print("（如果机器人断开，会自动尝试重连）")
    
    # 尝试发送多个请求
    for i in range(3):
        print(f"\n--- 发送第 {i+1} 个请求 ---")
        success = robot.send_service_request(
            "/control_service",
            "test_action"
        )
        
        if success:
            print(f"✓ 第 {i+1} 个请求成功")
        else:
            print(f"✗ 第 {i+1} 个请求失败")
        
        time.sleep(2)
    
    robot.close()

def test_quick_retry():
    """测试快速重试模式"""
    print("\n" + "="*70)
    print("测试4: 快速重试模式")
    print("="*70)
    print("说明：快速重试10次，每次间隔1秒")
    print()
    
    robot = RobotController(
        "192.168.217.100",
        "9091",
        RobotType.ROBOT_A,
        max_retry_attempts=10,  # 重试10次
        retry_interval=1         # 每1秒重试
    )
    
    print("开始连接...")
    start_time = time.time()
    
    if robot.connect():
        elapsed = time.time() - start_time
        print(f"\n✓ 连接成功！耗时: {elapsed:.1f}秒")
        robot.close()
    else:
        elapsed = time.time() - start_time
        print(f"\n✗ 连接失败。总耗时: {elapsed:.1f}秒")

def main():
    """主函数：选择要运行的测试"""
    print("\n" + "="*70)
    print("机器人自动重连功能测试")
    print("="*70)
    print("\n可用测试：")
    print("1. 无限重试模式（推荐用于生产环境）")
    print("2. 有限重试模式（推荐用于测试）")
    print("3. 连接断开后自动重连（需要机器人在线）")
    print("4. 快速重试模式")
    print("5. 运行所有测试")
    print()
    
    try:
        choice = input("请选择测试 (1-5) [默认: 2]: ").strip()
        if not choice:
            choice = "2"
        
        if choice == "1":
            test_infinite_retry()
        elif choice == "2":
            test_limited_retry()
        elif choice == "3":
            test_auto_reconnect_on_disconnect()
        elif choice == "4":
            test_quick_retry()
        elif choice == "5":
            print("\n注意：测试1会无限重试，请在机器人连接成功后手动中断")
            input("按Enter继续...")
            
            test_limited_retry()
            input("\n按Enter继续下一个测试...")
            
            test_quick_retry()
            input("\n按Enter继续下一个测试...")
            
            print("\n跳过测试3（需要机器人实际在线）")
            print("你可以单独运行测试3来体验自动重连功能")
        else:
            print("无效选择")
    
    except KeyboardInterrupt:
        print("\n\n✗ 测试被用户中断")
    except Exception as e:
        print(f"\n✗ 测试出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

