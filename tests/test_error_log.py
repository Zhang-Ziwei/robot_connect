#!/usr/bin/env python3
"""
错误日志系统测试脚本
用于验证日志功能是否正常工作
"""

from error_logger import get_error_logger
import time

def test_basic_logging():
    """测试基本日志功能"""
    print("="*70)
    print("测试错误日志系统")
    print("="*70 + "\n")
    
    logger = get_error_logger()
    print(f"日志文件: {logger.get_log_file()}\n")
    
    print("1. 测试信息日志...")
    logger.info("测试模块", "这是一条测试信息")
    time.sleep(0.5)
    
    print("2. 测试警告日志...")
    logger.warning("测试模块", "这是一条测试警告")
    time.sleep(0.5)
    
    print("3. 测试错误日志...")
    logger.error("测试模块", "这是一条测试错误")
    time.sleep(0.5)
    
    print("4. 测试连接成功日志...")
    logger.connection_success("Test Robot", "192.168.1.100", "9091", 1)
    time.sleep(0.5)
    
    print("5. 测试连接失败日志...")
    logger.connection_failed("Test Robot", "192.168.1.100", "9091", "连接超时")
    time.sleep(0.5)
    
    print("6. 测试请求成功日志...")
    logger.request_success("Test Robot", "/test_service", "test_action")
    time.sleep(0.5)
    
    print("7. 测试请求失败日志...")
    logger.request_failed("Test Robot", "/test_service", "test_action", "超时")
    time.sleep(0.5)
    
    print("8. 测试异常日志...")
    try:
        # 故意引发异常
        result = 1 / 0
    except Exception as e:
        logger.exception_occurred("Test Robot", "测试操作", e)
    time.sleep(0.5)
    
    print("\n" + "="*70)
    print("✓ 测试完成！")
    print("="*70)
    print(f"\n请查看日志文件: {logger.get_log_file()}")
    print("\n使用以下命令查看日志:")
    print(f"  cat {logger.get_log_file()}")
    print(f"  tail -f {logger.get_log_file()}")
    print()

if __name__ == "__main__":
    test_basic_logging()

