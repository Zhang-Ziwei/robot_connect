"""
任务状态机管理模块
管理任务的执行状态和步骤追踪
"""

import threading
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


class TaskStep(Enum):
    """任务流程步骤定义"""
    IDLE = "空闲"
    GRAB_SCAN_GUN = "抓取扫描枪"
    CV_DETECTING = "视觉检测瓶子"
    CV_DETECTING_EMPTY = "视觉检测瓶子已抓完"
    CV_DETECTING_SUCCESS = "视觉检测瓶子成功"
    GRABBING_BOTTLE = "抓取瓶子"
    PUT_TO_SCAN_MACHINE = "放到扫描转盘"
    WAITING_ID_INPUT = "等待ID录入" # 可以开始扫码
    ID_INPUT_SUCCESS = "ID录入成功"
    PUTTING_TO_BACK = "放置到后部平台"
    TURNING_BACK_FRONT = "转回正面"

    PUTTING_DOWN = "放下所有瓶子"
    PUTTING_DOWN_BOTTLE = "放下瓶子"
    PUTTING_DOWN_TO_BOTTLE_OPENER_500_SUCCESS = "放下瓶子到500ml开瓶器成功" # 500ml可以开始开瓶盖/关瓶盖
    PUTTING_DOWN_TO_BOTTLE_OPENER_250_SUCCESS = "放下瓶子到250ml开瓶器成功" # 250ml可以开始开瓶盖/关瓶盖 
    PUTTING_DOWN_TO_BOTTLE_OPENER_250 = "放下瓶子到250ml开瓶器"
    PUTTING_DOWN_TO_BOTTLE_OPENER = "放下瓶子到开瓶器（250ml/500ml）" # 外部设备通知，同时控制两台开瓶器
    BOTTLE_OPENER_OPEN_SUCCESS = "开瓶器开瓶成功"
    GRABBING_FROM_BOTTLE_OPENER_500 = "从500ml开瓶器抓取瓶子"
    GRABBING_FROM_BOTTLE_OPENER_250 = "从250ml开瓶器抓取瓶子"
    POURING_WATER = "分液"
    PUTTING_TO_BOTTLE_OPENER_500 = "放置到500ml开瓶器"
    PUTTING_TO_BOTTLE_OPENER_250 = "放置到250ml开瓶器"
    PICK_FROM_BOTTLE_OPENER_500_TO_BACK_TEMP = "从500ml开瓶器抓取瓶子放到后部暂存区"
    PICK_FROM_BOTTLE_OPENER_250_TO_BACK_TEMP = "从250ml开瓶器抓取瓶子放到后部暂存区"

    # 导航状态
    NAVIGATING_TO_SCAN = "导航到扫描台"
    NAVIGATING_TO_WAITING_SPLIT_AREA_TRANSFER = "导航到分液台待分液区（转运任务点位）"
    NAVIGATING_TO_WAITING_SPLIT_AREA_SPLIT = "导航到分液台待分液区（分液任务点位）"
    NAVIGATING_TO_SPLIT_MACHINE_SPLIT = "导航到分液台（分液任务点位）"
    NAVIGATING_TO_EMPTY_BOTTLE_AREA_TRANSFER = "导航到空瓶区（转运任务点位）"
    NAVIGATING_TO_EMPTY_BOTTLE_AREA_SPLIT = "导航到空瓶区（分液任务点位）"
    NAVIGATING_TO_250ML_SPLIT_DONE_AREA_TRANSFER = "导航到250ml分液完成暂存区（转运任务点位）"
    NAVIGATING_TO_250ML_SPLIT_DONE_AREA_SPLIT = "导航到250ml分液完成暂存区（分液任务点位）"
    NAVIGATING_TO_500ML_SPLIT_DONE_AREA_TRANSFER = "导航到500ml分液完成暂存区（转运任务点位）"
    NAVIGATING_TO_500ML_SPLIT_DONE_AREA_SPLIT = "导航到500ml分液完成暂存区（分液任务点位）"
    NAVIGATING_TO_CHROMATOGRAPH = "导航到色谱仪"

    # 单个导航点位操作动作
    ACTION_SCAN_AND_STORE_BOTTLES = "扫码并放置到后部暂存区"
    ACTION_SCAN_AND_STORE_BOTTLES_WAITING_SPLIT_AREA = "在待分液区抓取瓶子放到后部暂存区"
    ACTION_POURING_WATER = "分液动作"
    ACTION_SCAN_AND_STORE_BOTTLES_SPLIT_DONE_500ML_AREA = "从后部暂存区抓取瓶子放到500ml分液完成暂存区"
    ACTION_SCAN_AND_STORE_BOTTLES_SPLIT_DONE_250ML_AREA = "从后部暂存区抓取瓶子放到250ml分液完成暂存区"
    
    # 分液双手操作状态
    GRABBING_FROM_BOTTLE_OPENER_500_AND_250 = "双手从开瓶器抓取瓶子"
    PUTTING_DOWN_TO_BOTTLE_OPENER_SUCCESS = "放下瓶子到开瓶器成功"
    PUTTING_TO_BOTTLE_OPENER_500_AND_250 = "双手把手上的瓶子放到目标开瓶器上"
    PUTTING_TO_BOTTLE_OPENER_500_AND_250_SUCCESS = "放下瓶子到500ml和250ml开瓶器成功"# 500ml和250ml可以开始开瓶盖/关瓶盖
    RAISING_FROM_BOTTLE_OPENER_500_AND_250 = "双手从开瓶器提起瓶子"
    PUTTING_DOWN_TO_BACK_TEMP_500_AND_250 = "双手把手上的瓶子放到后部暂存区"
    
    # 转移到色谱仪状态
    GRABBING_FROM_SPLIT_DONE_AREA = "从分液完成暂存区抓取瓶子"
    PUTTING_TO_CHROMATOGRAPH = "放置瓶子到色谱仪暂存位"
    
    # 分液完成暂存区状态
    PUTTING_DOWN_TO_250ML_SPLIT_DONE_AREA = "放置到250ml分液完成暂存区"
    PUTTING_DOWN_TO_500ML_SPLIT_DONE_AREA = "放置到500ml分液完成暂存区"

    COMPLETED = "完成"
    ERROR = "错误"


class TaskStatus(Enum):
    """任务状态"""
    NOT_STARTED = "未开始"
    RUNNING = "运行中"
    WAITING = "等待中"
    COMPLETED = "已完成"
    ERROR = "错误"
    CANCELLED = "已取消"


class TaskStateMachine:
    """任务状态机"""
    
    def __init__(self):
        self.task_id: Optional[str] = None
        self.robot_id: Optional[str] = None  # 执行任务的机器人ID
        self.status: TaskStatus = TaskStatus.NOT_STARTED
        self.current_step: TaskStep = TaskStep.IDLE
        self.completed_steps: List[Dict[str, Any]] = []
        self.error_message: Optional[str] = None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        
        # 扫描结果
        self.scanned_bottles: List[Dict[str, Any]] = []
        self.current_bottle_info: Optional[Dict[str, Any]] = None

        # 视觉检测结果
        self.cv_detect_result: Optional[bool] = None
        # 子项目自定义步骤描述（如 WAIC / AJL，不依赖 TaskStep 枚举）
        self._step_label: Optional[str] = None
        
        # 线程锁
        self.lock = threading.Lock()
        
        logger.info("状态机", "任务状态机已初始化")
    
    def start_task(self, task_id: str, robot_id: str = None):
        """
        开始新任务
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        with self.lock:
            self.task_id = task_id
            self.robot_id = robot_id
            self.status = TaskStatus.RUNNING
            self.current_step = TaskStep.IDLE
            self.completed_steps = []
            self.error_message = None
            self.start_time = time.time()
            self.end_time = None
            self.scanned_bottles = []
            self.current_bottle_info = None
            self.cv_detect_result = None
            self._step_label = None
            logger.info("状态机", f"任务开始: {task_id}, 机器人: {robot_id}")
    
    def is_busy(self) -> bool:
        """是否有任务正在运行（用于拒绝新命令）"""
        with self.lock:
            return self.status == TaskStatus.RUNNING
    
    def update_step_label(self, step_label: str, message: str = ""):
        """更新当前步骤（字符串标签，供子项目自定义步骤名使用）"""
        with self.lock:
            if self._step_label:
                self.completed_steps.append({
                    "step": self._step_label,
                    "step_name": self._step_label,
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                    "duration": time.time() - self.start_time if self.start_time else 0,
                })
            self._step_label = step_label
            self.status = TaskStatus.RUNNING
            logger.info("状态机", f"步骤更新: {step_label} - {message}")
    
    def update_step(self, step: TaskStep, message: str = ""):
        """更新当前步骤"""
        with self.lock:
            # 记录上一步完成
            if self.current_step != TaskStep.IDLE:
                self.completed_steps.append({
                    "step": self.current_step.name,
                    "step_name": self.current_step.value,
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                    "duration": time.time() - self.start_time if self.start_time else 0
                })
            
            # 更新当前步骤
            self.current_step = step
            
            # 更新状态
            if step in (
                TaskStep.WAITING_ID_INPUT,
                TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER,
                TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER_500_SUCCESS,
                TaskStep.PUTTING_TO_BOTTLE_OPENER_500_AND_250_SUCCESS,
            ):
                self.status = TaskStatus.WAITING
            elif step == TaskStep.COMPLETED:
                self.status = TaskStatus.COMPLETED
                self.end_time = time.time()
            elif step == TaskStep.ERROR:
                self.status = TaskStatus.ERROR
                self.end_time = time.time()
            else:
                self.status = TaskStatus.RUNNING
            
            logger.info("状态机", f"步骤更新: {step.value} - {message}")
    
    def set_waiting_id(self, bottle_info: Dict[str, Any]):
        """设置等待ID录入状态"""
        with self.lock:
            self.current_bottle_info = bottle_info
            self.update_step(TaskStep.WAITING_ID_INPUT, f"等待录入瓶子ID (类型: {bottle_info.get('type', 'unknown')})")
    
    def add_scanned_bottle(self, bottle_id: str, bottle_type: str, slot_index: int, task: str):
        """添加已扫描的瓶子"""
        with self.lock:
            self.scanned_bottles.append({
                "bottle_id": bottle_id,
                "type": bottle_type,
                "slot_index": slot_index,
                "task": task,
                "timestamp": datetime.now().isoformat()
            })
            logger.info("状态机", f"瓶子已扫描: {bottle_id}, 任务: {task}")
        """获取已扫描的瓶子"""
        with self.lock:
            for bottle in self.scanned_bottles:
                if bottle.get("bottle_id") == bottle_id:
                    return bottle
            return None
    
    def delete_scanned_bottle(self, bottle_id: str):
        """删除已扫描的瓶子"""
        with self.lock:
            self.scanned_bottles = [bottle for bottle in self.scanned_bottles if bottle.get("bottle_id") != bottle_id]
            logger.info("状态机", f"瓶子已删除: {bottle_id}")

    def set_error(self, error_msg: str):
        """设置错误状态"""
        with self.lock:
            self.error_message = error_msg
            self.status = TaskStatus.ERROR
            self.current_step = TaskStep.ERROR
            self.end_time = time.time()
            
            logger.error("状态机", f"任务错误: {error_msg}")
    
    def complete_task(self, success: bool = True, message: str = ""):
        """完成任务"""
        with self.lock:
            if success:
                self.status = TaskStatus.COMPLETED
                self.current_step = TaskStep.COMPLETED
            else:
                self.status = TaskStatus.ERROR
                self.current_step = TaskStep.ERROR
                self.error_message = message
            
            self.end_time = time.time()
            
            logger.info("状态机", f"任务完成: success={success}, message={message}")
    
    def get_state(self, query_task_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取任务状态
        
        参数:
            query_task_id: 要查询的任务ID，如果为None则返回当前任务状态
        
        返回:
            任务状态字典
        """
        with self.lock:
            # 如果指定了task_id但与当前任务不匹配，返回未找到
            if query_task_id and query_task_id != self.task_id:
                return {
                    "cmd_id": query_task_id,
                    "status": "未找到",
                    "message": f"任务ID不匹配或任务不存在: {query_task_id}",
                    "current_task_id": self.task_id
                }
            
            # 返回当前任务状态（或指定ID匹配的任务状态）
            duration = None
            if self.start_time:
                duration = (self.end_time or time.time()) - self.start_time
            
            return {
                "cmd_id": self.task_id,
                "task_id": self.task_id,
                "robot_id": self.robot_id,  # 执行任务的机器人ID
                "status": self.status.value,
                "current_step": {
                    "name": self._step_label or self.current_step.name,
                    "description": self._step_label or self.current_step.value,
                },
                "completed_steps": self.completed_steps.copy(),
                "scanned_bottles": self.scanned_bottles.copy(),
                "current_bottle_info": self.current_bottle_info.copy() if self.current_bottle_info else None,
                "error_message": self.error_message,
                "start_time": datetime.fromtimestamp(self.start_time).isoformat() if self.start_time else None,
                "end_time": datetime.fromtimestamp(self.end_time).isoformat() if self.end_time else None,
                "duration_seconds": round(duration, 2) if duration else None,
                "scanned_count": len(self.scanned_bottles)
            }
    
    def reset(self):
        """重置状态机"""
        with self.lock:
            self.task_id = None
            self.robot_id = None
            self.status = TaskStatus.NOT_STARTED
            self.current_step = TaskStep.IDLE
            self.completed_steps = []
            self.error_message = None
            self.start_time = None
            self.end_time = None
            self.scanned_bottles = []
            self.current_bottle_info = None
            self._step_label = None
            
            logger.info("状态机", "状态机已重置")


# 全局状态机实例
_task_state_machine = None

def get_task_state_machine() -> TaskStateMachine:
    """获取状态机实例（单例）"""
    global _task_state_machine
    if _task_state_machine is None:
        _task_state_machine = TaskStateMachine()
    return _task_state_machine


# ──────────────────────────────────────────────────────────────────────────────
# 多机器人并行任务状态机
# ──────────────────────────────────────────────────────────────────────────────

class ParallelTaskStateMachine:
    """
    多机器人并行任务状态机。

    适用于一个整体任务需要多台机器人同时执行子流程的场景：
    每台机器人独立追踪步骤进度，整体任务状态由所有子流程的结果汇总决定。

    设计要点：
      ┌─────────────────────────────────────────────────────────┐
      │  任务级状态 (task-level)                                  │
      │    NOT_STARTED → RUNNING → COMPLETED                    │
      │                          ↘ ERROR  (任意机器人报错即触发)  │
      │                                                         │
      │  机器人级状态 (per-robot)  ← 互相独立，线程安全           │
      │    每台机器人维护：current_step + completed_steps          │
      │    done 标志：子流程结束（成功/失败）后置 True              │
      │    全部 done 且无 error → 整体自动置 COMPLETED            │
      └─────────────────────────────────────────────────────────┘

    典型用法：

        # 初始化（robot_ids 由外部传入，不在状态机内硬编码）
        tsm = ParallelTaskStateMachine(["robot_a", "robot_b"])

        # 接受命令时
        tsm.start_task(cmd_id)

        # 各机器人后台线程（互不干扰）
        tsm.update_step("robot_a", step_enum_or_str, "导航到P2")
        tsm.update_step("robot_b", step_enum_or_str, "装配中")

        # 出错时（整体立即变 ERROR）
        tsm.set_error("导航失败", robot_id="robot_a")

        # 子流程结束时（全部 done 且无 error → COMPLETED）
        tsm.mark_robot_done("robot_a")

        # HTTP 查询
        state = tsm.get_state()
        # state["status"]                           → "运行中" / "已完成" / "错误"
        # state["robots"]["robot_a"]["current_step"]
        # state["robots"]["robot_b"]["done"]
    """

    def __init__(self, robot_ids: List[str]):
        self._robot_ids = list(robot_ids)
        self._lock = threading.Lock()
        self._init_fields()

    def _init_fields(self):
        self.task_id: Optional[str] = None
        self.status: TaskStatus = TaskStatus.NOT_STARTED
        self.error_message: Optional[str] = None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self._robot_current_step: Dict[str, str] = {rid: "空闲" for rid in self._robot_ids}
        self._robot_completed_steps: Dict[str, List[Dict[str, Any]]] = {
            rid: [] for rid in self._robot_ids
        }
        self._robot_done: Dict[str, bool] = {rid: False for rid in self._robot_ids}

    # ── 任务级接口 ────────────────────────────────────────────────────────────

    def is_busy(self) -> bool:
        """是否有任务正在运行（用于拒绝新命令）"""
        return self.status == TaskStatus.RUNNING

    def start_task(self, task_id: str):
        """启动新任务，重置全部状态"""
        with self._lock:
            self._init_fields()
            self.task_id = task_id
            self.status = TaskStatus.RUNNING
            self.start_time = time.time()
            logger.info("ParallelTaskStateMachine", f"任务启动: {task_id}，参与机器人: {self._robot_ids}")

    def set_error(self, error_msg: str, robot_id: Optional[str] = None):
        """
        将整体任务置为 ERROR。
        传入 robot_id 时，错误前缀包含机器人标识，便于排查。
        """
        with self._lock:
            prefix = f"[{robot_id}] " if robot_id else ""
            full_msg = prefix + error_msg
            self.error_message = full_msg
            self.status = TaskStatus.ERROR
            if self.end_time is None:
                self.end_time = time.time()
            logger.error("ParallelTaskStateMachine", f"任务错误: {full_msg}")
            print(f"任务错误: {full_msg}")

    def cancel_task(self):
        """
        取消当前任务（外部主动停止）。
        仅在任务 RUNNING 时生效，其他状态不覆盖。
        """
        with self._lock:
            if self.status == TaskStatus.RUNNING:
                self.status = TaskStatus.CANCELLED
                if self.end_time is None:
                    self.end_time = time.time()
                logger.info("ParallelTaskStateMachine", "任务已取消")

    def mark_robot_done(self, robot_id: str):
        """
        标记某台机器人的子流程已结束（成功或失败均须调用）。

        - 幂等：重复调用无副作用。
        - 当所有机器人都 done 且整体状态仍是 RUNNING 时，
          自动将整体任务置为 COMPLETED。
        """
        with self._lock:
            if robot_id not in self._robot_done:
                return
            if self._robot_done[robot_id]:
                return  # 已标记，幂等返回
            self._robot_done[robot_id] = True
            logger.info(
                "ParallelTaskStateMachine",
                f"[{robot_id}] 子流程结束，done={self._robot_done}",
            )
            if all(self._robot_done.values()) and self.status == TaskStatus.RUNNING:
                self.status = TaskStatus.COMPLETED
                self.end_time = time.time()
                logger.info("ParallelTaskStateMachine", "所有机器人完成 → 任务 COMPLETED")

    # ── 机器人步骤级接口 ──────────────────────────────────────────────────────

    def update_step(self, robot_id: str, step, message: str = ""):
        """
        更新指定机器人的当前步骤（线程安全，两台机器人并发调用不会覆盖彼此）。
        step 可以是 Enum 成员或普通字符串。
        """
        step_value = step.value if hasattr(step, "value") else str(step)
        with self._lock:
            prev = self._robot_current_step.get(robot_id, "空闲")
            if prev != "空闲":
                self._robot_completed_steps.setdefault(robot_id, []).append({
                    "step": prev,
                    "message": message,
                    "timestamp": datetime.now().isoformat(),
                    "elapsed_s": round(time.time() - (self.start_time or time.time()), 2),
                })
            self._robot_current_step[robot_id] = step_value
            logger.info("ParallelTaskStateMachine", f"[{robot_id}] → {step_value}  {message}")

    # ── 状态查询 ──────────────────────────────────────────────────────────────

    def get_state(self) -> Dict[str, Any]:
        """返回完整任务状态字典，供 HTTP API 直接响应"""
        with self._lock:
            duration = None
            if self.start_time:
                duration = round((self.end_time or time.time()) - self.start_time, 2)
            return {
                "task_id": self.task_id,
                "status": self.status.value,
                "error_message": self.error_message,
                "start_time": (
                    datetime.fromtimestamp(self.start_time).isoformat()
                    if self.start_time else None
                ),
                "end_time": (
                    datetime.fromtimestamp(self.end_time).isoformat()
                    if self.end_time else None
                ),
                "duration_seconds": duration,
                "robots": {
                    rid: {
                        "current_step": self._robot_current_step.get(rid, "空闲"),
                        "completed_steps": list(self._robot_completed_steps.get(rid, [])),
                        "done": self._robot_done.get(rid, False),
                    }
                    for rid in self._robot_ids
                },
            }

