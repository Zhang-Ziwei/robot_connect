#!/usr/bin/env python3
"""
HTTP服务器诊断工具
用来诊断http消息无法收到的问题，直接运行即可
如果端口占用，使用sudo ss -tulpn | grep :<port>查看占用进程
"""

import socket
import subprocess
import sys
from constants import HTTP_SERVER_PORT

print("="*70)
print("HTTP服务器诊断工具")
print("="*70)

# 1. 检查本机IP
print("\n【1. 检查本机IP】")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    local_ip = s.getsockname()[0]
    s.close()
    print(f"✓ 本机IP: {local_ip}")
except Exception as e:
    print(f"✗ 获取IP失败: {e}")
    local_ip = None

# 2. 检查端口是否被占用
print(f"\n【2. 检查{HTTP_SERVER_PORT}端口状态】")
try:
    result = subprocess.run(['lsof', '-i', f':{HTTP_SERVER_PORT}'], 
                          capture_output=True, text=True)
    if result.stdout:
        print(f"{HTTP_SERVER_PORT}端口正在使用:")
        print(result.stdout)
    else:
        print(f"✗ {HTTP_SERVER_PORT}端口未被占用（需要先启动main.py）")
except Exception as e:
    print(f"无法检查端口: {e}")

# 3. 使用sudo检查
print(f"\n【3. 使用sudo检查{HTTP_SERVER_PORT}端口】")
try:
    result = subprocess.run(['sudo', 'lsof', '-i', f':{HTTP_SERVER_PORT}'], 
                          capture_output=True, text=True)
    if result.stdout:
        print(f"{HTTP_SERVER_PORT}端口正在使用:")
        print(result.stdout)
    else:
        print(f"✗ {HTTP_SERVER_PORT}端口未被占用")
except Exception as e:
    print(f"无法检查: {e}")

# 4. 检查netstat
print("\n【4. 检查网络监听状态】")
try:
    result = subprocess.run(['sudo', 'netstat', '-tulpn'], 
                          capture_output=True, text=True)
    lines = [line for line in result.stdout.split('\n') if str(HTTP_SERVER_PORT) in line]
    if lines:
        print(f"{HTTP_SERVER_PORT}端口监听状态:")
        for line in lines:
            print(line)
    else:
        print(f"✗ 未找到{HTTP_SERVER_PORT}端口监听")
except Exception as e:
    print(f"无法检查: {e}")

# 5. 检查防火墙
print("\n【5. 检查防火墙状态】")
try:
    result = subprocess.run(['sudo', 'ufw', 'status'], 
                          capture_output=True, text=True)
    print(result.stdout)
    if str(HTTP_SERVER_PORT) not in result.stdout and 'inactive' not in result.stdout.lower():
        print(f"\n⚠ 建议执行: sudo ufw allow {HTTP_SERVER_PORT}")
except Exception as e:
    print(f"无法检查防火墙: {e}")

# 6. 测试本地连接
print("\n【6. 测试本地连接】")
try:
    import requests
    response = requests.get(f"http://localhost:{HTTP_SERVER_PORT}/", timeout=2)
    print(f"✓ 本地连接成功")
    print(f"  响应: {response.text}")
except requests.exceptions.ConnectionError:
    print(f"✗ 无法连接到localhost:{HTTP_SERVER_PORT}")
    print("  请确认main.py是否正在运行")
except Exception as e:
    print(f"✗ 连接测试失败: {e}")

# 7. 测试网络接口连接
if local_ip:
    print(f"\n【7. 测试网络接口连接 {local_ip}:{HTTP_SERVER_PORT}】")
    try:
        import requests
        response = requests.get(f"http://{local_ip}:{HTTP_SERVER_PORT}/", timeout=2)
        print(f"✓ 网络接口连接成功")
        print(f"  响应: {response.text}")
    except Exception as e:
        print(f"✗ 连接失败: {e}")

# 8. 检查是否有其他进程监听8081
print("\n【8. 检查所有Python进程】")
try:
    result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
    lines = [line for line in result.stdout.split('\n') 
             if 'python' in line.lower() and 'main.py' in line]
    if lines:
        print("发现main.py进程:")
        for line in lines:
            print(line)
    else:
        print("✗ 未找到main.py进程")
except Exception as e:
    print(f"无法检查进程: {e}")

print("\n" + "="*70)
print("诊断建议:")
print("="*70)
print("1. 确保已运行 python main.py 并选择模式1")
print(f"2. 如果{HTTP_SERVER_PORT}被其他程序占用，先关闭那个程序")
print(f"3. 如果防火墙阻止，运行: sudo ufw allow {HTTP_SERVER_PORT}")
print("4. 确认对方电脑使用正确的IP地址")
if local_ip:
    print(f"5. 对方应该使用: curl -X POST http://{local_ip}:{HTTP_SERVER_PORT} ...")
print("="*70)

