#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简单的PLC客户端测试 - 用于验证消息接收功能
"""

import time
from pymodbus.client import ModbusTcpClient

print("=" * 60)
print("简单PLC客户端测试")
print("=" * 60)

# 连接到PLC服务器
print("\n连接到 localhost:1502...")
client = ModbusTcpClient('127.0.0.1', port=1502, timeout=3)

try:
    if not client.connect():
        print("✗ 无法连接到PLC服务器")
        print("  请确保 main.py 正在运行")
        exit(1)
    
    print("✓ 已连接到PLC服务器\n")
    
    # 等待一下让连接稳定
    time.sleep(1)
    
    # 测试1: 写入保持寄存器
    print("测试1: 写入保持寄存器 0 = 1")
    result = client.write_register(0, 1)
    if result.isError():
        print(f"  ✗ 写入失败: {result}")
    else:
        print("  ✓ 写入成功")
    time.sleep(1)
    
    print("\n测试2: 写入保持寄存器 0 = 2")
    result = client.write_register(0, 2)
    if result.isError():
        print(f"  ✗ 写入失败: {result}")
    else:
        print("  ✓ 写入成功")
    time.sleep(1)
    
    print("\n测试3: 写入保持寄存器 0 = 3")
    result = client.write_register(0, 3)
    if result.isError():
        print(f"  ✗ 写入失败: {result}")
    else:
        print("  ✓ 写入成功")
    time.sleep(1)
    
    # 测试4: 写入线圈
    print("\n测试4: 写入线圈 1 = True")
    result = client.write_coil(1, True)
    if result.isError():
        print(f"  ✗ 写入失败: {result}")
    else:
        print("  ✓ 写入成功")
    time.sleep(1)
    
    print("\n测试5: 写入线圈 1 = False")
    result = client.write_coil(1, False)
    if result.isError():
        print(f"  ✗ 写入失败: {result}")
    else:
        print("  ✓ 写入成功")
    time.sleep(1)
    
    print("\n✓ 所有测试完成")
    print("\n请查看服务器端输出，应该能看到:")
    print("  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 0 → 1")
    print("  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 1 → 2")
    print("  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 2 → 3")
    print("  📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: False → True")
    print("  📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: True → False")
    
except Exception as e:
    print(f"\n✗ 错误: {e}")
    import traceback
    traceback.print_exc()

finally:
    client.close()
    print("\n客户端已断开")
    print("=" * 60)

