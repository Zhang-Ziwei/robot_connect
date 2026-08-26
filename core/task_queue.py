"""
任务队列管理模块
管理HTTP命令的执行队列，确保任务顺序执行
"""

import threading
import queue
import time
from typing import Dict, Any, Optional, Callable
from enum import Enum
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"      # 等待执行
    RUNNING = "running"      # 正在执行
    COMPLETED = "completed"  # 执行完成
    FAILED = "failed"        # 执行失败


class Task:
    """任务对象"""
    
    def __init__(self, task_id: str, cmd_data: Dict, handler: Callable):
        self.task_id = task_id
        self.cmd_data = cmd_data
        self.cmd_type = cmd_data.get("cmd_type", "unknown")
        self.cmd_id = cmd_data.get("cmd_id", "unknown")
        self.handler = handler
        self.status = TaskStatus.PENDING
        self.result = None
        self.error = None
        self.created_time = time.time()
        self.start_time = None
        self.end_time = None
    
    def execute(self):
        """执行任务"""
        self.status = TaskStatus.RUNNING
        self.start_time = time.time()
        
        logger.info("任务队列", f"开始执行任务: {self.task_id} ({self.cmd_type})")
        print(f"\n{'='*70}")
        print(f"📋 执行任务: {self.task_id}")
        print(f"   命令类型: {self.cmd_type}")
        print(f"   命令ID: {self.cmd_id}")
        print(f"{'='*70}\n")
        
        try:
            # 调用命令处理器
            self.result = self.handler(self.cmd_data)
            
            # 检查结果
            if self.result and self.result.get("success", False):
                self.status = TaskStatus.COMPLETED
                logger.info("任务队列", f"任务执行成功: {self.task_id}")
            else:
                self.status = TaskStatus.FAILED
                self.error = self.result.get("message", "Unknown error")
                logger.error("任务队列", f"任务执行失败: {self.task_id} - {self.error}")
        
        except Exception as e:
            self.status = TaskStatus.FAILED
            self.error = str(e)
            self.result = {
                "cmd_id": self.cmd_id,
                "success": False,
                "message": f"任务执行异常: {str(e)}"
            }
            logger.exception_occurred("任务队列", f"执行任务{self.task_id}", e)
        
        finally:
            self.end_time = time.time()
            duration = self.end_time - self.start_time
            
            print(f"\n{'='*70}")
            print(f"✓ 任务完成: {self.task_id}")
            print(f"   状态: {self.status.value}")
            print(f"   耗时: {duration:.2f}秒")
            print(f"{'='*70}\n")
    
    def get_info(self) -> Dict:
        """获取任务信息"""
        info = {
            "task_id": self.task_id,
            "cmd_type": self.cmd_type,
            "cmd_id": self.cmd_id,
            "status": self.status.value,
            "created_time": self.created_time
        }
        
        if self.start_time:
            info["start_time"] = self.start_time
        
        if self.end_time:
            info["end_time"] = self.end_time
            info["duration"] = self.end_time - self.start_time
        
        if self.result:
            info["result"] = self.result
        
        if self.error:
            info["error"] = self.error
        
        return info


class TaskQueue:
    """任务队列管理器"""
    
    def __init__(self):
        self.task_queue = queue.Queue()
        self.tasks = {}  # task_id -> Task
        self.worker_thread = None
        self.running = False
        self.current_task = None
        self.task_counter = 0
        self.lock = threading.Lock()
        
        logger.info("任务队列", "任务队列管理器初始化")
    
    def start(self):
        """启动任务队列处理线程"""
        if self.running:
            logger.warning("任务队列", "任务队列已在运行")
            return
        
        self.running = True
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
        
        logger.info("任务队列", "任务队列处理线程已启动")
        print("✓ 任务队列已启动")
    
    def stop(self):
        """停止任务队列"""
        logger.info("任务队列", "正在停止任务队列...")
        self.running = False
        
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
        
        logger.info("任务队列", "任务队列已停止")
        print("✓ 任务队列已停止")
    
    def submit_task(self, cmd_data: Dict, handler: Callable) -> str:
        """
        提交任务到队列
        
        参数:
            cmd_data: 命令数据
            handler: 命令处理函数
        
        返回:
            task_id: 任务ID
        """
        with self.lock:
            self.task_counter += 1
            task_id = f"TASK_{self.task_counter:06d}"
        
        task = Task(task_id, cmd_data, handler)
        
        with self.lock:
            self.tasks[task_id] = task
        
        self.task_queue.put(task)
        
        # 获取队列长度
        queue_size = self.task_queue.qsize()
        
        logger.info("任务队列", 
                   f"任务已提交: {task_id} ({task.cmd_type}) - 队列长度: {queue_size}")
        
        print(f"\n{'─'*70}")
        print(f"📥 任务已加入队列")
        print(f"   任务ID: {task_id}")
        print(f"   命令类型: {task.cmd_type}")
        print(f"   命令ID: {task.cmd_id}")
        print(f"   队列位置: {queue_size}")
        print(f"{'─'*70}\n")
        
        return task_id
    
    def _worker(self):
        """工作线程 - 处理队列中的任务"""
        logger.info("任务队列", "工作线程开始运行")
        
        while self.running:
            try:
                # 从队列获取任务，超时1秒
                task = self.task_queue.get(timeout=1)
                
                # 设置当前任务
                with self.lock:
                    self.current_task = task
                
                # 执行任务
                task.execute()
                
                # 清除当前任务
                with self.lock:
                    self.current_task = None
                
                # 标记任务完成
                self.task_queue.task_done()
                
            except queue.Empty:
                # 队列为空，继续等待
                continue
            
            except Exception as e:
                logger.exception_occurred("任务队列", "工作线程处理任务", e)
                with self.lock:
                    self.current_task = None
        
        logger.info("任务队列", "工作线程已退出")
    
    def get_task_status(self, task_id: str) -> Optional[Dict]:
        """获取任务状态"""
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                return task.get_info()
        return None
    
    def get_all_tasks(self) -> Dict:
        """获取所有任务状态"""
        with self.lock:
            tasks_info = {
                "total_tasks": len(self.tasks),
                "queue_size": self.task_queue.qsize(),
                "current_task": self.current_task.task_id if self.current_task else None,
                "tasks": {}
            }
            
            for task_id, task in self.tasks.items():
                tasks_info["tasks"][task_id] = task.get_info()
        
        return tasks_info
    
    def get_queue_status(self) -> Dict:
        """获取队列状态"""
        with self.lock:
            # 统计各状态任务数
            status_count = {
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0
            }
            
            for task in self.tasks.values():
                status_count[task.status.value] += 1
            
            return {
                "running": self.running,
                "queue_size": self.task_queue.qsize(),
                "current_task": self.current_task.task_id if self.current_task else None,
                "total_tasks": len(self.tasks),
                "status_count": status_count
            }
    
    def wait_for_completion(self, timeout: Optional[float] = None):
        """等待所有任务完成"""
        logger.info("任务队列", "等待所有任务完成...")
        self.task_queue.join()
        logger.info("任务队列", "所有任务已完成")


# 全局任务队列实例
_task_queue = None

def get_task_queue():
    """获取任务队列实例（单例）"""
    global _task_queue
    if _task_queue is None:
        _task_queue = TaskQueue()
    return _task_queue

