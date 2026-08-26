"""
文件锁模块 - 防止程序同时运行多次
支持Linux系统
"""

import os
import sys
import fcntl
import atexit

class FileLock:
    """文件锁类 - 确保程序只运行一个实例"""
    
    def __init__(self, lock_file="program.lock"):
        """
        初始化文件锁
        
        参数:
            lock_file: 锁文件路径（默认在当前目录）
        """
        self.lock_file = lock_file
        self.lock_fd = None
    
    def acquire(self):
        """
        获取锁
        
        返回:
            True: 成功获取锁
            False: 锁已被占用（程序已在运行）
        """
        try:
            # 打开或创建锁文件
            self.lock_fd = open(self.lock_file, 'w')
            
            # 尝试获取排他锁（非阻塞）
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            
            # 写入当前进程ID
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            
            # 注册退出时自动释放锁
            atexit.register(self.release)
            
            return True
            
        except IOError:
            # 锁已被占用
            if self.lock_fd:
                self.lock_fd.close()
                self.lock_fd = None
            return False
        except Exception as e:
            print(f"获取文件锁时出错: {e}")
            if self.lock_fd:
                self.lock_fd.close()
                self.lock_fd = None
            return False
    
    def release(self):
        """释放锁"""
        if self.lock_fd:
            try:
                # 释放锁
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
                self.lock_fd.close()
                self.lock_fd = None
                
                # 删除锁文件
                if os.path.exists(self.lock_file):
                    os.remove(self.lock_file)
            except Exception as e:
                print(f"释放文件锁时出错: {e}")
    
    def get_running_pid(self):
        """
        获取正在运行的程序的进程ID
        
        返回:
            进程ID (int) 或 None
        """
        try:
            if os.path.exists(self.lock_file):
                with open(self.lock_file, 'r') as f:
                    pid = f.read().strip()
                    return int(pid) if pid else None
        except:
            return None
        return None
    
    def is_running(self):
        """
        检查程序是否正在运行
        
        返回:
            True: 程序正在运行
            False: 程序未运行
        """
        return not self.acquire()
    
    def __enter__(self):
        """支持with语句"""
        if not self.acquire():
            raise RuntimeError("程序已在运行中")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """支持with语句"""
        self.release()
        return False


def ensure_single_instance(lock_file="program.lock"):
    """
    确保程序只运行一个实例的装饰器/函数
    
    使用方法:
        if not ensure_single_instance():
            sys.exit(1)
    
    参数:
        lock_file: 锁文件路径
    
    返回:
        FileLock对象（如果成功）或 None（如果失败）
    """
    lock = FileLock(lock_file)
    
    if lock.acquire():
        return lock
    else:
        running_pid = lock.get_running_pid()
        print("="*70)
        print("❌ 错误：程序已经在运行中！")
        print("="*70)
        if running_pid:
            print(f"正在运行的进程ID: {running_pid}")
        print("\n请检查:")
        print("  1. 是否已经在另一个终端窗口运行了此程序")
        print("  2. 如果确认程序未运行，可能是上次异常退出")
        print(f"     解决方法: 删除锁文件 'rm {lock_file}'")
        print("="*70)
        return None

