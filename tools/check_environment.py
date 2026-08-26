#!/usr/bin/env python3
"""
环境检查脚本
在运行模拟测试前，检查所有必要的依赖和配置
"""

import sys
import os
import subprocess

def print_header(title):
    """打印标题"""
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70)

def check_item(name, status, message=""):
    """打印检查项"""
    symbol = "✓" if status else "✗"
    status_text = "通过" if status else "失败"
    print(f"{symbol} {name}: {status_text}")
    if message:
        print(f"  {message}")

def main():
    print_header("环境检查工具")
    print("\n正在检查测试环境...")
    
    all_passed = True
    
    # 1. Python 版本检查
    print_header("1. Python 环境")
    py_version = sys.version_info
    py_ok = py_version.major == 3 and py_version.minor >= 9
    check_item("Python 版本", py_ok, 
               f"当前: {py_version.major}.{py_version.minor}.{py_version.micro} (需要 >= 3.9)")
    all_passed = all_passed and py_ok
    
    # 检查是否在conda环境
    in_conda = 'conda' in sys.prefix.lower() or 'CONDA_DEFAULT_ENV' in os.environ
    check_item("Conda 环境", in_conda,
               f"当前环境: {os.environ.get('CONDA_DEFAULT_ENV', '未知')}")
    
    # 2. 目录检查
    print_header("2. 工作目录")
    correct_dir = os.path.basename(os.getcwd()) == 'robot_connect'
    check_item("工作目录", correct_dir,
               f"当前目录: {os.getcwd()}")
    all_passed = all_passed and correct_dir
    
    # 3. 必要文件检查
    print_header("3. 测试文件")
    required_files = [
        'mock_robot_controller.py',
        'test_mock_mode.py',
        'cmd_handler.py',
        'http_server.py',
        'bottle_manager.py',
        'task_optimizer.py',
        'constants.py',
        'error_logger.py'
    ]
    
    all_files_exist = True
    for filename in required_files:
        exists = os.path.exists(filename)
        check_item(filename, exists)
        all_files_exist = all_files_exist and exists
    
    all_passed = all_passed and all_files_exist
    
    # 4. 测试命令文件检查
    print_header("4. 测试命令文件")
    test_commands = [
        'test_commands/bottle_get_command.json',
        'test_commands/pickup_command.json',
        'test_commands/put_to_command.json'
    ]
    
    all_test_files_exist = True
    for filename in test_commands:
        exists = os.path.exists(filename)
        check_item(filename, exists)
        all_test_files_exist = all_test_files_exist and exists
    
    all_passed = all_passed and all_test_files_exist
    
    # 5. 依赖包检查
    print_header("5. Python 依赖包")
    
    def check_import(module_name, package_name=None):
        if package_name is None:
            package_name = module_name
        try:
            __import__(module_name)
            check_item(package_name, True, "已安装")
            return True
        except ImportError:
            check_item(package_name, False, "未安装")
            return False
    
    deps_ok = True
    deps_ok = check_import('asyncio') and deps_ok
    deps_ok = check_import('websockets') and deps_ok
    deps_ok = check_import('json') and deps_ok
    deps_ok = check_import('logging') and deps_ok
    
    all_passed = all_passed and deps_ok
    
    # 6. 端口检查
    print_header("6. 端口检查")
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('localhost', 8080))
        sock.close()
        
        port_available = result != 0
        check_item("端口 8080", port_available,
                   "可用" if port_available else "已被占用 - 需要先关闭占用进程")
        all_passed = all_passed and port_available
    except Exception as e:
        check_item("端口 8080", False, f"检查失败: {e}")
        all_passed = False
    
    # 7. 模块导入测试
    print_header("7. 模块导入测试")
    
    try:
        from mock_robot_controller import MockRobotController
        check_item("mock_robot_controller", True)
    except Exception as e:
        check_item("mock_robot_controller", False, str(e))
        all_passed = False
    
    try:
        from constants import RobotType
        check_item("constants", True)
    except Exception as e:
        check_item("constants", False, str(e))
        all_passed = False
    
    try:
        from programs.TJSH.cmd_handler import init_cmd_handler
        check_item("cmd_handler", True)
    except Exception as e:
        check_item("cmd_handler", False, str(e))
        all_passed = False
    
    try:
        from http_server import get_http_server
        check_item("http_server", True)
    except Exception as e:
        check_item("http_server", False, str(e))
        all_passed = False
    
    # 最终结果
    print_header("检查结果")
    if all_passed:
        print("\n✅ 所有检查通过！环境配置正确。")
        print("\n你现在可以运行:")
        print("  python test_mock_mode.py")
        print("\n查看使用说明:")
        print("  cat 运行说明.md")
        print("  cat 模拟测试快速开始.md")
    else:
        print("\n❌ 部分检查失败，请解决上述问题。")
        print("\n常见解决方法:")
        print("  1. 确保激活了正确的conda环境:")
        print("     conda activate robot_connect")
        print("  2. 确保在正确的目录:")
        print("     cd /home/zhangziwei/Code/robot_connect")
        print("  3. 如果端口被占用:")
        print("     lsof -i :8080")
        print("     kill -9 <PID>")
        print("  4. 查看详细说明:")
        print("     cat 运行说明.md")
    
    print("\n" + "="*70 + "\n")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())

