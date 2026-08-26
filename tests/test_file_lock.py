#!/usr/bin/env python3
"""
文件锁功能测试脚本
用于验证文件锁是否正常工作
"""

import os
import sys
import time
import subprocess
from file_lock import FileLock, ensure_single_instance

def test_basic_lock():
    """测试1：基本锁功能"""
    print("="*70)
    print("测试1: 基本文件锁功能")
    print("="*70)
    
    lock = FileLock("test_basic.lock")
    
    # 第一次获取锁
    print("\n1. 尝试获取锁...")
    if lock.acquire():
        print("✓ 成功获取锁")
        
        # 检查锁文件
        if os.path.exists("test_basic.lock"):
            print("✓ 锁文件已创建")
            with open("test_basic.lock", 'r') as f:
                pid = f.read()
                print(f"✓ 锁文件内容（进程ID）: {pid}")
        
        # 释放锁
        print("\n2. 释放锁...")
        lock.release()
        print("✓ 锁已释放")
        
        # 检查锁文件是否删除
        if not os.path.exists("test_basic.lock"):
            print("✓ 锁文件已删除")
        
        print("\n✓ 测试1通过\n")
        return True
    else:
        print("✗ 获取锁失败")
        print("✗ 测试1失败\n")
        return False

def test_double_lock():
    """测试2：防止重复获取锁"""
    print("="*70)
    print("测试2: 防止重复获取锁")
    print("="*70)
    
    lock1 = FileLock("test_double.lock")
    lock2 = FileLock("test_double.lock")
    
    print("\n1. 第一个实例获取锁...")
    if lock1.acquire():
        print("✓ 第一个实例成功获取锁")
        
        print("\n2. 第二个实例尝试获取同一个锁...")
        if not lock2.acquire():
            print("✓ 第二个实例无法获取锁（符合预期）")
            
            # 获取正在运行的PID
            running_pid = lock2.get_running_pid()
            if running_pid:
                print(f"✓ 检测到正在运行的进程ID: {running_pid}")
            
            # 释放第一个实例的锁
            print("\n3. 第一个实例释放锁...")
            lock1.release()
            print("✓ 锁已释放")
            
            # 现在第二个实例应该可以获取锁
            print("\n4. 第二个实例再次尝试获取锁...")
            if lock2.acquire():
                print("✓ 第二个实例成功获取锁")
                lock2.release()
                print("✓ 第二个实例释放锁")
                
                print("\n✓ 测试2通过\n")
                return True
            else:
                print("✗ 第二个实例仍无法获取锁")
        else:
            print("✗ 第二个实例不应该能获取锁")
            lock2.release()
    else:
        print("✗ 第一个实例获取锁失败")
    
    print("✗ 测试2失败\n")
    return False

def test_with_statement():
    """测试3：with语句支持"""
    print("="*70)
    print("测试3: with语句支持")
    print("="*70)
    
    print("\n使用with语句获取锁...")
    try:
        with FileLock("test_with.lock") as lock:
            print("✓ 成功进入with块")
            print("✓ 锁已自动获取")
            
            # 检查锁文件
            if os.path.exists("test_with.lock"):
                print("✓ 锁文件存在")
        
        # 退出with块后，锁应该自动释放
        if not os.path.exists("test_with.lock"):
            print("✓ 退出with块后，锁已自动释放")
            print("\n✓ 测试3通过\n")
            return True
        else:
            print("✗ 锁文件未删除")
    except RuntimeError as e:
        print(f"✗ 发生错误: {e}")
    
    print("✗ 测试3失败\n")
    return False

def test_ensure_single_instance():
    """测试4：ensure_single_instance函数"""
    print("="*70)
    print("测试4: ensure_single_instance函数")
    print("="*70)
    
    print("\n1. 第一次调用 ensure_single_instance...")
    lock1 = ensure_single_instance("test_single.lock")
    if lock1:
        print("✓ 成功获取锁")
        
        print("\n2. 第二次调用 ensure_single_instance（应该失败）...")
        lock2 = ensure_single_instance("test_single.lock")
        if not lock2:
            print("✓ 第二次调用正确返回None")
            
            # 释放锁
            print("\n3. 释放锁...")
            lock1.release()
            print("✓ 锁已释放")
            
            print("\n✓ 测试4通过\n")
            return True
        else:
            print("✗ 第二次调用不应该成功")
            lock2.release()
    else:
        print("✗ 第一次调用失败")
    
    print("✗ 测试4失败\n")
    return False

def test_cleanup():
    """清理测试文件"""
    print("="*70)
    print("清理测试文件")
    print("="*70)
    
    test_files = [
        "test_basic.lock",
        "test_double.lock",
        "test_with.lock",
        "test_single.lock"
    ]
    
    for filename in test_files:
        if os.path.exists(filename):
            try:
                os.remove(filename)
                print(f"✓ 已删除: {filename}")
            except Exception as e:
                print(f"⚠ 删除失败: {filename} - {e}")
    
    print()

def main():
    """主测试函数"""
    print("\n" + "="*70)
    print("文件锁功能测试程序")
    print("="*70 + "\n")
    
    results = []
    
    try:
        # 运行测试
        results.append(("基本锁功能", test_basic_lock()))
        results.append(("防止重复获取锁", test_double_lock()))
        results.append(("with语句支持", test_with_statement()))
        results.append(("ensure_single_instance", test_ensure_single_instance()))
        
        # 清理
        test_cleanup()
        
        # 显示结果
        print("="*70)
        print("测试结果汇总")
        print("="*70)
        
        for test_name, result in results:
            status = "✓ 通过" if result else "✗ 失败"
            print(f"{test_name}: {status}")
        
        # 总结
        passed = sum(1 for _, r in results if r)
        total = len(results)
        
        print("\n" + "="*70)
        if passed == total:
            print(f"✓ 所有测试通过！({passed}/{total})")
            print("文件锁功能正常工作")
        else:
            print(f"⚠ 部分测试失败 ({passed}/{total} 通过)")
            print("请检查文件锁实现")
        print("="*70 + "\n")
        
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        test_cleanup()
    except Exception as e:
        print(f"\n✗ 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        test_cleanup()

if __name__ == "__main__":
    main()

