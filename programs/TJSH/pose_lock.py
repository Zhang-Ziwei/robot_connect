"""
点位互斥锁模块
用于防止两台机器人在相邻点位同时操作时发生干扰

冲突点位对（使用 NavigationPose 属性名作为 key）:
- WAITING_SPLIT_AREA_TRANSFER <-> WAITING_SPLIT_AREA_SPLIT (待分液区)
- SPLIT_DONE_250ML_AREA_TRANSFER <-> SPLIT_DONE_250ML_AREA_SPLIT (250ml分液完成暂存区)
- SPLIT_DONE_500ML_AREA_TRANSFER <-> SPLIT_DONE_500ML_AREA_SPLIT (500ml分液完成暂存区)

当一台机器人在转运点位执行操作时，另一台机器人在对应的分液点位会被阻塞等待。

用法：acquire/release 传入 NavigationPose 属性名字符串，例如 "WAITING_SPLIT_AREA_TRANSFER"。
"""

import threading
import time
from typing import Dict, Optional, Set
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


# 冲突点位映射（使用属性名字符串，避免列表不可哈希问题）：转运点位名 -> 分液点位名
CONFLICTING_POSES: Dict[str, str] = {
    "WAITING_SPLIT_AREA_TRANSFER":    "WAITING_SPLIT_AREA_SPLIT",
    "SPLIT_DONE_250ML_AREA_TRANSFER": "SPLIT_DONE_250ML_AREA_SPLIT",
    "SPLIT_DONE_500ML_AREA_TRANSFER": "SPLIT_DONE_500ML_AREA_SPLIT",
}

# 反向映射：分液点位 -> 转运点位
CONFLICTING_POSES_REVERSE = {v: k for k, v in CONFLICTING_POSES.items()}

# 所有受保护的点位（转运+分液）
ALL_PROTECTED_POSES = set(CONFLICTING_POSES.keys()) | set(CONFLICTING_POSES.values())


class PoseLock:
    """
    点位互斥锁
    
    使用方法:
        pose_lock = get_pose_lock()
        
        # 方式1: 使用上下文管理器（推荐）
        with pose_lock.acquire_pose(robot_id, pose_name):
            # 执行操作
            pass
        
        # 方式2: 手动获取和释放
        pose_lock.acquire(robot_id, pose_name)
        try:
            # 执行操作
            pass
        finally:
            pose_lock.release(robot_id, pose_name)
    """
    
    def __init__(self):
        self._lock = threading.Lock()
        # 当前被占用的点位: {pose_name: robot_id}
        self._occupied_poses: Dict[str, str] = {}
        # 每个点位的条件变量
        self._conditions: Dict[str, threading.Condition] = {}
        # 等待中的机器人: {pose_name: [robot_ids]}
        self._waiting_robots: Dict[str, list] = {}
    
    def _get_condition(self, pose_name: str) -> threading.Condition:
        """获取点位对应的条件变量"""
        if pose_name not in self._conditions:
            self._conditions[pose_name] = threading.Condition(self._lock)
        return self._conditions[pose_name]
    
    def _get_conflicting_pose(self, pose_name: str) -> Optional[str]:
        """获取与指定点位冲突的点位"""
        if pose_name in CONFLICTING_POSES:
            return CONFLICTING_POSES[pose_name]
        elif pose_name in CONFLICTING_POSES_REVERSE:
            return CONFLICTING_POSES_REVERSE[pose_name]
        return None
    
    def acquire(self, robot_id: str, pose_name: str, timeout: float = None) -> bool:
        """
        获取点位锁
        
        Args:
            robot_id: 机器人ID
            pose_name: 点位名称
            timeout: 超时时间（秒），None表示无限等待
        
        Returns:
            bool: 是否成功获取锁
        """
        if pose_name not in ALL_PROTECTED_POSES:
            # 不在保护列表中的点位，直接返回成功
            return True
        
        conflicting_pose = self._get_conflicting_pose(pose_name)
        
        with self._lock:
            start_time = time.time()
            
            # 检查冲突点位是否被占用
            while conflicting_pose and conflicting_pose in self._occupied_poses:
                occupying_robot = self._occupied_poses[conflicting_pose]
                if occupying_robot == robot_id:
                    # 同一个机器人，不冲突
                    break
                
                logger.info("点位互斥锁", 
                           f"{robot_id} 等待进入 {pose_name}，"
                           f"冲突点位 {conflicting_pose} 被 {occupying_robot} 占用")
                print(f"⏳ {robot_id} 等待: {pose_name} (冲突: {occupying_robot} 在 {conflicting_pose})")
                
                # 记录等待状态
                if pose_name not in self._waiting_robots:
                    self._waiting_robots[pose_name] = []
                if robot_id not in self._waiting_robots[pose_name]:
                    self._waiting_robots[pose_name].append(robot_id)
                
                # 等待冲突点位释放
                condition = self._get_condition(conflicting_pose)
                
                if timeout is not None:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        logger.warning("点位互斥锁", f"{robot_id} 获取 {pose_name} 超时")
                        return False
                    condition.wait(timeout=remaining)
                else:
                    condition.wait()
            
            # 清除等待状态
            if pose_name in self._waiting_robots and robot_id in self._waiting_robots[pose_name]:
                self._waiting_robots[pose_name].remove(robot_id)
            
            # 占用点位
            self._occupied_poses[pose_name] = robot_id
            logger.info("点位互斥锁", f"{robot_id} 已进入点位 {pose_name}")
            print(f"🔒 {robot_id} 占用点位: {pose_name}")
            return True
    
    def release(self, robot_id: str, pose_name: str):
        """
        释放点位锁
        
        Args:
            robot_id: 机器人ID
            pose_name: 点位名称
        """
        if pose_name not in ALL_PROTECTED_POSES:
            return
        
        with self._lock:
            if pose_name in self._occupied_poses:
                if self._occupied_poses[pose_name] == robot_id:
                    del self._occupied_poses[pose_name]
                    logger.info("点位互斥锁", f"{robot_id} 已离开点位 {pose_name}")
                    print(f"🔓 {robot_id} 释放点位: {pose_name}")
                    
                    # 通知等待的机器人
                    condition = self._get_condition(pose_name)
                    condition.notify_all()
                else:
                    logger.warning("点位互斥锁", 
                                  f"{robot_id} 尝试释放 {pose_name}，"
                                  f"但该点位被 {self._occupied_poses[pose_name]} 占用")
    
    def is_occupied(self, pose_name: str) -> bool:
        """检查点位是否被占用"""
        with self._lock:
            return pose_name in self._occupied_poses
    
    def get_occupying_robot(self, pose_name: str) -> Optional[str]:
        """获取占用点位的机器人ID"""
        with self._lock:
            return self._occupied_poses.get(pose_name)
    
    def get_status(self) -> Dict:
        """获取当前状态"""
        with self._lock:
            return {
                "occupied_poses": self._occupied_poses.copy(),
                "waiting_robots": {k: list(v) for k, v in self._waiting_robots.items() if v}
            }
    
    def acquire_pose(self, robot_id: str, pose_name: str, timeout: float = None):
        """
        上下文管理器方式获取点位锁
        
        使用方法:
            with pose_lock.acquire_pose(robot_id, pose_name):
                # 执行操作
                pass
        """
        return PoseLockContext(self, robot_id, pose_name, timeout)


class PoseLockContext:
    """点位锁上下文管理器"""
    
    def __init__(self, lock: PoseLock, robot_id: str, pose_name: str, timeout: float = None):
        self.lock = lock
        self.robot_id = robot_id
        self.pose_name = pose_name
        self.timeout = timeout
        self.acquired = False
    
    def __enter__(self):
        self.acquired = self.lock.acquire(self.robot_id, self.pose_name, self.timeout)
        if not self.acquired:
            raise TimeoutError(f"{self.robot_id} 获取点位 {self.pose_name} 超时")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            self.lock.release(self.robot_id, self.pose_name)
        return False


# 单例模式
_pose_lock: Optional[PoseLock] = None


def get_pose_lock() -> PoseLock:
    """获取点位锁单例"""
    global _pose_lock
    if _pose_lock is None:
        _pose_lock = PoseLock()
    return _pose_lock


def reset_pose_lock():
    """重置点位锁单例"""
    global _pose_lock
    _pose_lock = None

