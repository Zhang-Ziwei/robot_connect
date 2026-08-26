#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PLC客户端模拟器
用于测试PLC服务器的消息接收和显示功能
"""

import time
from pymodbus.client import ModbusTcpClient
from constants import PLCHoldingRegisters, PLCCoils

def simulate_plc_client():
    """模拟PLC客户端发送消息"""
    print("=" * 60)
    print("PLC客户端模拟器")
    print("=" * 60)
    
    # 连接到PLC服务器
    print("\n1. 连接到PLC服务器 (localhost:1502)...")
    client = ModbusTcpClient('127.0.0.1', port=1502)
    
    if not client.connect():
        print("✗ 无法连接到PLC服务器，请确保服务器已启动")
        return
    
    print("✓ 已连接到PLC服务器\n")
    
    try:
        # 测试1: 写入线圈
        print("2. 测试写入线圈...")
        print("-" * 60)
        
        print("  写入线圈 0 = True (模拟开盖启动)")
        client.write_coil(PLCCoils.OPEN_START, True)
        time.sleep(0.5)
        
        print("  写入线圈 1 = True (模拟开盖完成)")
        client.write_coil(PLCCoils.OPEN_FINISH, True)
        time.sleep(0.5)
        
        # 测试2: 写入保持寄存器
        print("\n3. 测试写入保持寄存器...")
        print("-" * 60)
        
        print("  写入寄存器 0 (开盖模块状态) = 1")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 1)
        time.sleep(0.5)
        
        print("  写入寄存器 0 (开盖模块状态) = 2")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 2)
        time.sleep(0.5)
        
        print("  写入寄存器 0 (开盖模块状态) = 3")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 3)
        time.sleep(0.5)
        
        # 测试3: 批量写入
        print("\n4. 测试批量写入保持寄存器...")
        print("-" * 60)
        
        print("  批量写入所有模块状态为 1")
        client.write_registers(0, [1, 1, 1, 1])
        time.sleep(0.5)
        
        print("  批量写入所有模块状态为 2")
        client.write_registers(0, [2, 2, 2, 2])
        time.sleep(0.5)
        
        # 测试4: 读取数据
        print("\n5. 测试读取数据...")
        print("-" * 60)
        
        print("  读取线圈 0-3:")
        result = client.read_coils(0, 4)
        if not result.isError():
            print(f"    结果: {result.bits[:4]}")
        
        print("  读取保持寄存器 0-3:")
        result = client.read_holding_registers(0, 4)
        if not result.isError():
            print(f"    结果: {result.registers}")
        
        print("\n6. 模拟完整流程...")
        print("-" * 60)
        
        # 模拟开盖流程
        print("  [开盖流程] 设置状态为 1 (准备就绪)")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 1)
        time.sleep(0.3)
        
        print("  [开盖流程] 写入线圈启动开盖")
        client.write_coil(PLCCoils.OPEN_START, True)
        time.sleep(0.3)
        
        print("  [开盖流程] 设置状态为 2 (工作中)")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 2)
        time.sleep(0.3)
        
        print("  [开盖流程] 设置状态为 3 (工作完成)")
        client.write_register(PLCHoldingRegisters.OPEN_LID_STATE, 3)
        time.sleep(0.3)
        
        print("\n✓ 所有测试完成！")
        print("  请查看PLC服务器端的输出，应该能看到所有的客户端消息变化")
        
    except Exception as e:
        print(f"\n✗ 测试出错: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        client.close()
        print("\n客户端已断开连接")
        print("=" * 60)

if __name__ == "__main__":
    print("\n⚠ 请确保PLC服务器已经启动 (运行 main.py)")
    input("按 Enter 键开始测试...")
    simulate_plc_client()

