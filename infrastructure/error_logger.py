"""
简洁的错误日志模块
只记录错误和关键信息，不影响现有代码运行
"""

import os
import traceback
from datetime import datetime
from pathlib import Path

class ErrorLogger:
    """错误日志记录器 - 单例模式"""
    
    _instance = None
    _log_file = None
    _log_dir = "logs"
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ErrorLogger, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        """初始化日志文件"""
        # 创建日志目录
        Path(self._log_dir).mkdir(exist_ok=True)
        
        # 生成日志文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._log_file = os.path.join(self._log_dir, f"error_log_{timestamp}.txt")
        
        # 写入日志头
        self._write_header()
    
    def _write_header(self):
        """写入日志文件头"""
        with open(self._log_file, 'w', encoding='utf-8') as f:
            f.write("="*80 + "\n")
            f.write(f"机器人控制系统错误日志\n")
            f.write(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*80 + "\n\n")
    
    def _write_log(self, level, robot_name, message, exc=None):
        """写入日志（写完后 fsync，防止设备断电/重启后文件尾部出现空字节）"""
        try:
            with open(self._log_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"[{timestamp}] [{level}] {robot_name}: {message}\n")
                
                # 如果有异常信息，记录堆栈
                if exc:
                    f.write("异常详情:\n")
                    f.write(traceback.format_exc())
                    f.write("\n")

                if level in ("ERROR", "WARNING"):
                    f.write("-"*80 + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"写入日志失败: {e}")
    
    def error(self, robot_name, message, exc=None):
        """记录错误"""
        self._write_log("ERROR", robot_name, message, exc)
    
    def warning(self, robot_name, message):
        """记录警告"""
        self._write_log("WARNING", robot_name, message)
    
    def info(self, robot_name, message):
        """记录信息"""
        self._write_log("INFO", robot_name, message)
    
    def connection_failed(self, robot_name, host, port, reason):
        """记录连接失败"""
        message = f"连接失败 - {host}:{port} - 原因: {reason}"
        self._write_log("ERROR", robot_name, message)
    
    def connection_success(self, robot_name, host, port, attempt=1):
        """记录连接成功"""
        if attempt > 1:
            message = f"连接成功 - {host}:{port} (第{attempt}次尝试)"
        else:
            message = f"连接成功 - {host}:{port}"
        self._write_log("INFO", robot_name, message)
    
    def request_failed(self, robot_name, service, action, reason):
        """记录请求失败"""
        message = f"请求失败 - service={service}, action={action} - 原因: {reason}"
        self._write_log("ERROR", robot_name, message)
    
    def request_success(self, robot_name, service, action):
        """记录请求成功"""
        message = f"请求成功 - service={service}, action={action}"
        self._write_log("INFO", robot_name, message)
    
    def exception_occurred(self, robot_name, operation, exc):
        """记录异常"""
        message = f"操作异常 - {operation}"
        self._write_log("ERROR", robot_name, message, exc)
    
    def get_log_file(self):
        """获取日志文件路径"""
        return self._log_file


# 全局日志记录器实例
_error_logger = None

def get_error_logger():
    """获取错误日志记录器实例"""
    global _error_logger
    if _error_logger is None:
        _error_logger = ErrorLogger()
    return _error_logger

