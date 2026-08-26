"""
WRC（演示装配）任务处理器

独立类，不继承、不修改旧项目任何文件。
仅导入以下稳定的底层接口，与旧项目业务逻辑解耦：

    ┌─────────────────────────────────────────────────────┐
    │  导入层级（由稳定到易变）                             │
    │                                                     │
    │  标准库（threading / uuid / time）       — 最稳定    │
    │  infrastructure.*（constants/logger）    — 稳定      │
    │  hardware.*（robot_controller/nav）      — 较稳定    │
    │  core.task_state_machine                 — 较稳定    │
    │  programs.WRC.constants（本项目专属）    — 自己维护  │
    │                                                     │
    │  ✗ 不导入 cmd_handler / core/robot_actions           │
    └─────────────────────────────────────────────────────┘

流程图说明（见 whiteboard 图）：
    接收任务 → 检查机器人 → 初始化状态机
    → 导航T1 → T1动作
    → 导航T2 → T2动作
    → 导航T3 → T3动作
    → 导航T4 → T4动作 ──► 发布等待消息 + 等待人工介入
    → 导航T5 → T5动作
    → 等待 NEXT_STEP 命令（外部 HTTP 触发）
    → 导航T6 → T6动作
    → 导航T7 → 完成动作
    → 流程结束
"""

import json
import os
import threading
import time
import uuid
from typing import Dict, Optional, Any

# ── 稳定底层接口 ──────────────────────────────────────────────────────────────
from infrastructure.constants import ErrorCode, NavigationState, make_error_response, make_success_response
from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    send_navigation_action,
    build_navigation_goal,
    wait_for_topic_message,
    get_robot_odom,
    is_robot_at_pose,
)
from core.task_state_machine import ParallelTaskStateMachine

# ── WRC 专属常量（只改这里，不动旧项目）────────────────────────────────────────
from hardware.task_utils import send_task_action
from programs.WRC.constants import (
    WRCPose, WRCNavTolerance, WRCAtPoseTolerance, WRCService, WRCTask, WRCStep, WRCTimeout,
    WRC_TASK_ACTION_SPEC, BoxSlotState, NavigationPose,
)

logger = get_error_logger()

# 空箱子状态持久化文件（相对于工作目录）
_WRC_STATE_FILE = os.path.join(os.path.dirname(__file__), "wrc_box_state.json")


class SlotTracker:
    """
    通用槽位状态暂存器。

    管理一组物理槽位，每个槽位独立记录 BoxSlotState（空箱子 / 放了一半 / 放满了 / 没箱子）。
    状态持久化到 JSON 文件，重启后不会丢失进度。

    典型用法（P3 区域 2 个槽位）：

        tracker = SlotTracker("P3", ["P3_1", "P3_2"], "/path/atc_p3.json")

        # 找可用槽位（有空箱子）
        slot = tracker.find_slot_by_state(BoxSlotState.EMPTY)

        # 放一半后更新状态
        tracker.set_state(slot, BoxSlotState.HALF)

        # 放满后更新状态
        tracker.set_state(slot, BoxSlotState.FULL)

        # 人工取走箱子后重置
        tracker.set_state(slot, BoxSlotState.NO_BOX)

        # 运维手动批量重置
        tracker.reset_all(BoxSlotState.EMPTY)
    """

    def __init__(
        self,
        name: str,
        slots: list,
        state_file: str,
        default_state: BoxSlotState = BoxSlotState.EMPTY,
    ):
        self._name = name
        self._slots = slots
        self._file = state_file
        self._default = default_state
        self._lock = threading.Lock()
        self._states: Dict[str, str] = self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _default_states(self) -> Dict[str, str]:
        return {slot: self._default.value for slot in self._slots}

    def _load(self) -> Dict[str, str]:
        try:
            if os.path.exists(self._file):
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 验证所有槽位都存在且值合法
                valid_values = {s.value for s in BoxSlotState}
                if all(
                    slot in data and data[slot] in valid_values
                    for slot in self._slots
                ):
                    logger.info(f"SlotTracker[{self._name}]", f"已加载状态: {data}")
                    return data
        except Exception as e:
            logger.error(f"SlotTracker[{self._name}]", f"加载状态文件失败，使用默认值: {e}")
        states = self._default_states()
        logger.info(f"SlotTracker[{self._name}]", f"初始化默认状态: {states}")
        return states

    def _save(self):
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._states, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"SlotTracker[{self._name}]", f"保存状态文件失败: {e}")

    # ── 查询接口 ──────────────────────────────────────────────────────────────

    def get_state(self, slot: str) -> BoxSlotState:
        """获取指定槽位的当前状态"""
        with self._lock:
            raw = self._states.get(slot)
            if raw is None:
                raise ValueError(f"[{self._name}] 未知槽位: {slot}，可选: {self._slots}")
            return BoxSlotState(raw)

    def find_slot_by_state(self, state: BoxSlotState) -> Optional[str]:
        """
        按顺序找到第一个匹配 state 的槽位名；找不到返回 None。

        示例：
            slot = tracker.find_slot_by_state(BoxSlotState.EMPTY)
            if slot is None:
                # 没有空箱子可用，报错或等待
        """
        with self._lock:
            for slot in self._slots:
                if self._states.get(slot) == state.value:
                    return slot
            return None

    def find_slots_by_state(self, state: BoxSlotState) -> list:
        """返回所有匹配 state 的槽位名列表"""
        with self._lock:
            return [s for s in self._slots if self._states.get(s) == state.value]

    def get_all_states(self) -> Dict[str, BoxSlotState]:
        """返回所有槽位的当前状态字典 {slot_name: BoxSlotState}"""
        with self._lock:
            return {slot: BoxSlotState(v) for slot, v in self._states.items()}

    # ── 更新接口 ──────────────────────────────────────────────────────────────

    def set_state(self, slot: str, state: BoxSlotState):
        """更新指定槽位的状态并持久化"""
        if slot not in self._slots:
            raise ValueError(f"[{self._name}] 未知槽位: {slot}，可选: {self._slots}")
        with self._lock:
            old = self._states.get(slot, "?")
            self._states[slot] = state.value
            self._save()
            logger.info(
                f"SlotTracker[{self._name}]",
                f"槽位 {slot}: {old} → {state.value}",
            )
            print(f"[{self._name}] 槽位 {slot}: {old} → {state.value}")

    def reset_all(self, state: BoxSlotState = BoxSlotState.EMPTY):
        """将所有槽位重置为指定状态（运维 / 测试用）"""
        with self._lock:
            for slot in self._slots:
                self._states[slot] = state.value
            self._save()
            logger.info(
                f"SlotTracker[{self._name}]",
                f"所有槽位已重置为: {state.value}",
            )

    # ── 调试显示 ──────────────────────────────────────────────────────────────

    def get_status_display(self) -> str:
        """格式化的状态一览，便于 print / 日志输出"""
        lines = [f"[{self._name}] 槽位状态:"]
        with self._lock:
            for slot in self._slots:
                lines.append(f"  {slot}: {self._states.get(slot, '?')}")
        return "\n".join(lines)


class WRCHandler:
    """
    WRC 演示装配任务处理器（独立类）。

    使用方式（在 main.py 或路由层中注册）：

        wrc = WRCHandler(robots={"robot_a": robot_a}, task_state_machine=tsm)

        # HTTP 命令路由
        "TRANS_COMPONENT" → atc.handle_trans_component(cmd_data)
        "WRC_NEXT_STEP"   → atc.handle_next_step(cmd_data)
    """

    def __init__(self, robots: dict):
        self.robots = robots or {}
        # 连续流程状态机只跟踪 robot_a / robot_b；
        # robot_c 仅在 MANUAL_RESET 时执行拆垛，不参与 mark_robot_done 闭环。
        flow_ids = [rid for rid in ("robot_a", "robot_b") if rid in self.robots]
        if not flow_ids:
            flow_ids = list(self.robots.keys())
        self.task_state_machine = ParallelTaskStateMachine(flow_ids)

        # 等待人工介入的信号（NEXT_STEP 命令触发）
        self._next_step_event: threading.Event = threading.Event()
        self._next_step_data: Optional[Dict] = None

        # ── 流程控制 ──────────────────────────────────────────────────────────
        # _pause_event：set=正常运行，clear=暂停中（每个最小颗粒度动作后检查）
        self._pause_event: threading.Event = threading.Event()
        self._pause_event.set()
        # _stop_event：set=流程已结束，clear=正常
        self._stop_event: threading.Event = threading.Event()
        # _process_active：PROCESS_BEGINS 后置 True，PROCESS_ENDED 后置 False。
        self._process_active: bool = False
        # MANUAL_RESET_COMPLETED 置位后，robot_c 拆垛、robot_a 开始分拣
        self._manual_reset_event: threading.Event = threading.Event()

        # P1 区域槽位暂存器
        self._p1_tracker = SlotTracker(
            name="P1",
            slots=["P1"],
            state_file=os.path.join(os.path.dirname(__file__), "wrc_p1_slots.json"),
            default_state=BoxSlotState.EMPTY,
        )

        # P3 区域槽位暂存器（P3_1 / P3_2）
        self._p3_tracker = SlotTracker(
            name="P3",
            slots=["P3_1", "P3_2"],
            state_file=os.path.join(os.path.dirname(__file__), "wrc_p3_slots.json"),
            default_state=BoxSlotState.EMPTY,
        )
        # P4 单槽
        self._p4_tracker = SlotTracker(
            name="P4",
            slots=["P4"],
            state_file=os.path.join(os.path.dirname(__file__), "wrc_p4_slots.json"),
            default_state=BoxSlotState.EMPTY,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 公开命令入口
    # ──────────────────────────────────────────────────────────────────────────

    def handle_trans_component(self, cmd_data: Dict) -> Dict:
        """
        处理 TRANS_COMPONENT 命令（异步模式）。
        立即返回 task_id，后台线程执行实际流程。
        """
        cmd_id  = cmd_data.get("cmd_id")
        params  = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")
        # 机器人分配功能，这里会指定机器人
        robot_id_trans = "robot_a"
        robot_id_assemble = "robot_b"


        # 1. 验证机器人存在
        robot = self._get_robot(robot_id)
        if robot is None:
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"指定的机器人 {robot_id} 不存在",
                cmd_id=cmd_id,
                available_robots=list[Any](self.robots.keys()),
            )

        # 2. 检查机器人是否正忙
        if self.task_state_machine.is_busy():
            state = self.task_state_machine.get_state()
            robot_steps = {
                rid: info["current_step"]
                for rid, info in state.get("robots", {}).items()
            }
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "当前有任务正在执行，无法接受新任务",
                cmd_id=cmd_id,
                current_task_id=state.get("task_id"),
                current_status=state.get("status"),
                robot_steps=robot_steps,
            )

        logger.info("WRC", f"TRANS_COMPONENT 任务启动: cmd_id={cmd_id}, robot={robot_id}")

        # 3. 初始化状态机（任务级 + 两台机器人步骤追踪同时启动）
        self.task_state_machine.start_task(cmd_id)

        # 预设 P1 为 FULL，线程启动后立即跳过等待直接开始分拣
        #self._p1_tracker.set_state("P1", BoxSlotState.FULL)

        # 4. 后台线程执行任务
        t = threading.Thread(
            target=self._execute_trans_component_async2,
            args=(cmd_id, robot_id_trans),
            daemon=True,
            name=f"WRC-{cmd_id}",
        )
        t.start()

        # 5. 后台线程执行装配流程
        t = threading.Thread(
            target=self._execute_assemble_async,
            args=(cmd_id, robot_id_assemble),
            daemon=True,
            name=f"WRC-{cmd_id}",
        )
        t.start()
        
        return make_success_response(
            "WRC 演示任务已启动",
            cmd_id=cmd_id,
            robot_id=robot_id,
            note="使用 GET_TASK_STATE 命令查询任务状态",
        )

    def handle_next_step(self, cmd_data: Dict) -> Dict:
        """
        处理 WRC_NEXT_STEP 命令。
        由外部（人工确认后）通过 HTTP 调用，触发等待中的流程继续执行。
        """
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params", {})

        if not self.task_state_machine.is_busy():
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                "当前没有正在运行的 WRC 任务",
                cmd_id=cmd_id,
            )

        logger.info("WRC", f"收到 NEXT_STEP 信号 (cmd_id={cmd_id})")
        self._next_step_data = params
        self._next_step_event.set()

        return make_success_response(
            "NEXT_STEP 信号已接收，任务继续执行",
            cmd_id=cmd_id,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 流程控制命令入口（PROCESS_BEGINS / PAUSED / RESUMED / ENDED / MANUAL_RESET）
    # ──────────────────────────────────────────────────────────────────────────

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_BEGINS：启动全流程（连续模式）。
        重置流程控制标志，P1 初始为 EMPTY
        （等待 MANUAL_RESET_COMPLETED → robot_c pick_box_to_sp 放料）。
        robot_a / robot_b 持续循环，直到收到 PROCESS_ENDED。
        """
        cmd_id = cmd_data.get("cmd_id")
        robot_id_trans    = "robot_a"
        robot_id_assemble = "robot_b"

        if self.task_state_machine.is_busy():
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "流程已在运行中，请先发送 PROCESS_ENDED 结束当前流程",
                cmd_id=cmd_id,
            )

        # 验证分拣 / 装配机器人存在
        if self._get_robot(robot_id_trans) is None:
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {robot_id_trans} 不存在",
                cmd_id=cmd_id,
            )
        if self._get_robot("robot_c") is None:
            logger.warning(
                "WRC",
                "robot_c 未配置：MANUAL_RESET 时将跳过拆垛并直接置 P1=FULL",
            )

        # 重置流程控制标志，激活流程
        self._stop_event.clear()
        self._pause_event.set()
        self._manual_reset_event.clear()
        self._process_active = True

        # 连续模式：P1 初始为 EMPTY，等待 MANUAL_RESET_COMPLETED 后送料
        self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)

        # 初始化状态机
        self.task_state_machine.start_task(cmd_id)

        # 初始化槽位状态：
        # - P3_1 / P3_2 初始都有空箱（EMPTY）
        # - P4 初始无箱（NO_BOX），首轮放满后可直接把满箱搬到 P4
        # （P1 由外部控制：handle_trans_component 预设 FULL，
        #  handle_process_begins 预设 EMPTY 等待 MANUAL_RESET_COMPLETED）
        self._p3_tracker.set_state("P3_1", BoxSlotState.EMPTY)
        self._p3_tracker.set_state("P3_2", BoxSlotState.EMPTY)
        self._p4_tracker.set_state("P4", BoxSlotState.NO_BOX)

        # 启动两台机器人的后台线程
        threading.Thread(
            target=self._execute_trans_component_async2,
            args=(cmd_id, robot_id_trans),
            daemon=True,
            name=f"WRC-Trans-{cmd_id}",
        ).start()
        threading.Thread(
            target=self._execute_assemble_async,
            args=(cmd_id, robot_id_assemble),
            daemon=True,
            name=f"WRC-Assemble-{cmd_id}",
        ).start()

        logger.info("WRC", f"PROCESS_BEGINS: 连续流程已启动 (cmd_id={cmd_id})")
        return make_success_response("已接收PROCESS_BEGINS命令，流程开始工作", cmd_id=cmd_id)

    def handle_process_paused(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_PAUSED：暂停流程。
        机器人完成当前最小颗粒度动作（send_service_request_task 返回）后暂停。
        仅在 PROCESS_BEGINS 之后生效。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED, "流程尚未启动，请先发送 PROCESS_BEGINS", cmd_id=cmd_id
            )
        self._pause_event.clear()   # clear = 暂停
        logger.info("WRC", f"PROCESS_PAUSED: 流程将在下一动作完成后暂停 (cmd_id={cmd_id})")
        return make_success_response("流程已暂停，将在当前动作完成后生效", cmd_id=cmd_id)

    def handle_process_resumed(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_RESUMED：恢复暂停的流程。
        仅在 PROCESS_BEGINS 之后生效。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED, "流程尚未启动，请先发送 PROCESS_BEGINS", cmd_id=cmd_id
            )
        self._pause_event.set()     # set = 继续运行
        logger.info("WRC", f"PROCESS_RESUMED: 流程已恢复 (cmd_id={cmd_id})")
        return make_success_response("流程已恢复", cmd_id=cmd_id)

    def handle_process_ended(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_ENDED：结束流程，后续步骤不再执行。
        同时解除暂停（防止线程永久阻塞）并取消任务状态机。
        仅在 PROCESS_BEGINS 之后生效。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED, "流程尚未启动，请先发送 PROCESS_BEGINS", cmd_id=cmd_id
            )
        self._stop_event.set()
        self._pause_event.set()         # 解除暂停，让阻塞的 _check_flow_control 继续并检测到 stop
        self._next_step_event.set()     # 解除 NEXT_STEP 等待
        self._process_active = False    # 流程结束，重置激活标志
        self._manual_reset_event.clear()
        self.task_state_machine.cancel_task()
        logger.info("WRC", f"PROCESS_ENDED: 流程已结束 (cmd_id={cmd_id})")
        return make_success_response("流程已结束", cmd_id=cmd_id)

    def handle_manual_reset_completed(self, cmd_data: Dict) -> Dict:
        """
        MANUAL_RESET_COMPLETED：人工复位 / 补料完成。
        同步阻塞调用 robot_c 的 pick_box_to_sp（无 area，一次向 P1、P2 各放一箱），
        完成后将 P1 置 FULL（一箱两套零件），唤醒 robot_a，再返回 HTTP 结果。
        仅在 PROCESS_BEGINS 之后生效。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED, "流程尚未启动，请先发送 PROCESS_BEGINS", cmd_id=cmd_id
            )

        robot_id = "robot_c"
        robot = self._get_robot(robot_id)
        if robot is None:
            logger.error("WRC", "人工复位：robot_c 不存在，仍将 P1 置 FULL 以不阻塞流程")
            self._p1_tracker.set_state("P1", BoxSlotState.FULL)
            self._manual_reset_event.set()
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                "机器人 robot_c 不存在",
                cmd_id=cmd_id,
            )

        # 同步阻塞：等 pick_box_to_sp 返回后再继续
        ok = self._place_box_to_sp(robot, robot_id)
        if ok:
            self._p1_tracker.set_state("P1", BoxSlotState.FULL)
            self._manual_reset_event.set()
            logger.info("WRC", "人工复位流程完成：robot_c 已向 P1/P2 放箱，P1 已设置为 FULL")
            return make_success_response(
                "robot_c pick_box_to_sp 完成，P1/P2 料箱已放置",
                cmd_id=cmd_id,
            )

        logger.error("WRC", "robot_c pick_box_to_sp 失败，P1 保持原状态，不唤醒分拣")
        return make_error_response(
            ErrorCode.ROBOT_ACTION_FAILED,
            "robot_c pick_box_to_sp 失败",
            cmd_id=cmd_id,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # 机器人流程：装配流程
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_assemble_async(self, task_id: str, robot_id: str = "robot_b"):
        """
        后台线程：执行装配流程（robot_b），持续循环直到收到 PROCESS_ENDED。
        每轮开始前等待 P4 有满箱子（由 robot_a 搬来），装配完成后进入下一轮。
        无论成功/失败，finally 中均调用 mark_robot_done 确保状态机能正确结束。
        """
        robot = self._get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return

        try:
            logger.info("WRC", f"装配流程启动 (task={task_id}, robot={robot_id})")

            while True:
                # ── 等待 P4 有满箱子 ──────────────────────────────────────────────
                logger.info("WRC", f"[{robot_id}] 等待 P4 有满箱子（robot_a 将满箱搬到 P4 后触发）...")
                while self._p4_tracker.get_state("P4") != BoxSlotState.FULL:
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(1)
                logger.info("WRC", f"[{robot_id}] P4 满箱就绪，开始装配")

                # 1. 执行装配，直到P4料箱拿完
                self.task_state_machine.update_step(robot_id, WRCStep.ASSEMBLY, "装配中")
                result = robot.send_service_request_task(
                    WRCService.ROBOT_TASK,
                    task=WRCTask.ASSEMBLY,
                    maxtime=WRCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "装配", robot_id):
                    return
                
                # 更新P4槽位状态为EMPTY
                self._p4_tracker.set_state("P4", BoxSlotState.EMPTY)

                # 2. 继续装配，直到装配完成
                result = robot.send_service_request_task(
                    WRCService.ROBOT_TASK,
                    task=WRCTask.CONTINUE_ASSEMBLY,
                    maxtime=WRCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "继续装配", robot_id):
                    return

                logger.info("WRC", f"[{robot_id}] 装配完成，等待下一周期...")

        except Exception as e:
            logger.exception_occurred("WRC", f"装配流程异常 (task={task_id})", e)
            self.task_state_machine.set_error(f"装配异常: {e}", robot_id)
        finally:
            self.task_state_machine.mark_robot_done(robot_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 机器人流程：分拣搬运流程
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_trans_component_async(self, task_id: str, robot_id: str = "robot_a"):
        """
        后台线程：WRC 演示流程（robot_a / WA2）。

        流程（对照流程图）：
          1. 等待人工复位（robot_c 拆垛后 P1→FULL，一箱含两套零件）
          2. 确认/导航到 P1，等待物料就绪（FULL 或 HALF）
          3. Service 在 P1 抓取零件 A
          4. 选择 P3 空料箱 n，导航到 P3，放下零件 A（槽位→HALF）
             同时消耗一套零件：P1 FULL→HALF，或 HALF→EMPTY
          5. 导航到 P2，Service 抓取零件 B
          6. 再导航回同一 P3 槽位 n，放下零件 B（槽位→FULL）
          7. 在 P3 直接搬满箱到 P4（按 P4 状态分支）：
               P4 无箱：满箱直接从 P3-n 搬到 P4，再按零件余量回点
               P4 为空：pick_up_box/put_down_box 交换空满箱，再按零件余量回点
               P4 已满：留在 P3-n 前等待装配腾空（P4→EMPTY），再执行换箱，再回点
               回点规则：P1 仍为 HALF → 回 P1 继续第二套；P1 已 EMPTY（两套抓完）→ 回 home 等补料
               其它：回环继续分拣
        """
        robot = self._get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return

        try:
            logger.info("WRC", f"分拣搬运流程启动 (task={task_id}, robot={robot_id})")

            while True:
                # ── 1. 等待 MANUAL_RESET_COMPLETED（robot_c 拆垛后 P1→FULL）──
                logger.info("WRC", f"[{robot_id}] 等待 MANUAL_RESET_COMPLETED / 料箱就绪...")
                self.task_state_machine.update_step(
                    robot_id, WRCStep.WAITING_MANUAL_RESET, "等待人工复位完成信号",
                )
                while (
                    not self._manual_reset_event.is_set()
                    and self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY
                ):
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(0.5)
                self._manual_reset_event.clear()

                # ── 2. 确认在 P1，否则导航；等待 P1 物料就绪 ─────────────────
                self.task_state_machine.update_step(
                    robot_id, WRCStep.NAVIGATING_TO_P1, "导航到P1点位",
                )
                if is_robot_at_pose(
                    robot, getattr(NavigationPose, "P1"),
                    WRCAtPoseTolerance.DISTANCE, WRCAtPoseTolerance.HEADING,
                    timeout=WRCAtPoseTolerance.TIMEOUT,
                ):
                    logger.info("WRC", f"[{robot_id}] 已在P1，跳过导航")
                else:
                    if not self._navigate(robot, NavigationPose.P1, "P1点位", robot_id):
                        return


                # 等待 P1 物料就绪（FULL=两套零件，HALF=剩一套；EMPTY 则等待补料）
                logger.info("WRC", f"[{robot_id}] 等待 P1 物料就绪...")
                deadline = time.time() + WRCTimeout.P1_WAIT
                while self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    if not self._check_flow_control(robot_id):
                        return
                    if time.time() > deadline:
                        msg = f"等待 P1 物料超时（{WRCTimeout.P1_WAIT}s）"
                        logger.error("WRC", f"[{robot_id}] {msg}")
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    time.sleep(1)
                logger.info(
                    "WRC",
                    f"[{robot_id}] P1 物料已就绪（{self._p1_tracker.get_state('P1').value}），"
                    f"开始抓取零件A",
                )

                # ── 3. Service：P1 抓取零件 A（原 Action 保留注解）────────────
                self.task_state_machine.update_step(
                    robot_id, WRCStep.PICK_UP_COMPONENT_A_AT_P1, "抓取零件A在P1点位",
                )
                # if not self._send_component_action(
                #     robot, WRCTask.PICK_UP_COMPONENT_A, WRCPose.P1,
                #     "抓取零件A在P1点位", robot_id,
                # ):
                #     return
                if not self._send_component_service(
                    robot, WRCTask.PICK_UP_COMPONENT_A, WRCPose.P1,
                    "抓取零件A在P1点位", robot_id,
                ):
                    return
                
                # ── 4. 选择 P3 空料箱 n，导航到 P3，放下零件 A ────────────────
                #     （先放 A，再去 P2 取 B；A/B 放入同一空箱槽位 n）
                logger.info("WRC", f"P3 槽位状态:\n{self._p3_tracker.get_status_display()}")
                p3_n = self._p3_tracker.find_slot_by_state(BoxSlotState.EMPTY)
                if p3_n is None:
                    msg = "P3 区域没有空箱子槽位: " + self._p3_tracker.get_status_display()
                    logger.error("WRC", msg)
                    self.task_state_machine.set_error(msg, robot_id)
                    return
                area_n = getattr(WRCPose, p3_n)
                logger.info("WRC", f"选择空料箱 {p3_n}（area={area_n}）放置零件A")

                self.task_state_machine.update_step(
                    robot_id, WRCStep.NAVIGATING_TO_P3, f"导航到{p3_n}放置零件A",
                )
                if not self._navigate(robot, p3_n + "_mid", f"{p3_n}点位", robot_id):
                    return

                self.task_state_machine.update_step(
                    robot_id, WRCStep.PUT_DOWN_COMPONENT_A_AT_P3, f"放下零件A到{p3_n}",
                )
                # if not self._send_component_action(
                #     robot, WRCTask.PUT_DOWN_COMPONENT_A, area_n,
                #     f"放下零件A到{p3_n}", robot_id,
                # ):
                #     return
                if not self._send_component_service(
                    robot, WRCTask.PUT_DOWN_COMPONENT_A, area_n,
                    f"放下零件A到{p3_n}", robot_id,
                ):
                    return

                self._p3_tracker.set_state(p3_n, BoxSlotState.HALF)
                # 一箱两套零件：FULL→HALF（首套抓完），HALF→EMPTY（第二套抓完）
                p1_before = self._p1_tracker.get_state("P1")
                if p1_before == BoxSlotState.FULL:
                    self._p1_tracker.set_state("P1", BoxSlotState.HALF)
                    logger.info("WRC", f"{p3_n} → HALF，P1 FULL → HALF（剩一套零件）")
                elif p1_before == BoxSlotState.HALF:
                    self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)
                    logger.info("WRC", f"{p3_n} → HALF，P1 HALF → EMPTY（两套已抓完）")
                else:
                    self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)
                    logger.warning(
                        "WRC",
                        f"{p3_n} → HALF，P1 原状态={p1_before.value}，降级置 EMPTY",
                    )

                # ── 5. 导航到 P2，Service 抓取零件 B（原 Action 保留注解）────
                self.task_state_machine.update_step(
                    robot_id, WRCStep.NAVIGATING_TO_P2, "导航到P2点位抓取零件B",
                )
                if not self._navigate(robot, NavigationPose.P2, "P2点位", robot_id):
                    return

                self.task_state_machine.update_step(
                    robot_id, WRCStep.PICK_UP_COMPONENT_B_AT_P2, "抓取零件B在P2点位",
                )
                # if not self._send_component_action(
                #     robot, WRCTask.PICK_UP_COMPONENT_B, WRCPose.P2,
                #     "抓取零件B在P2点位", robot_id,
                # ):
                #     return
                if not self._send_component_service(
                    robot, WRCTask.PICK_UP_COMPONENT_B, WRCPose.P2,
                    "抓取零件B在P2点位", robot_id,
                ):
                    return

                # ── 6. 再导航回同一 P3 槽位 n，放下零件 B ─────────────────────
                self.task_state_machine.update_step(
                    robot_id, WRCStep.NAVIGATING_TO_P3, f"导航到{p3_n}放置零件B",
                )
                if not self._navigate(robot, p3_n + "_mid", f"{p3_n}点位", robot_id):
                    return

                self.task_state_machine.update_step(
                    robot_id, WRCStep.PUT_DOWN_COMPONENT_B_AT_P3, f"放下零件B到{p3_n}",
                )
                # if not self._send_component_action(
                #     robot, WRCTask.PUT_DOWN_COMPONENT_B, area_n,
                #     f"放下零件B到{p3_n}", robot_id,
                # ):
                #     return
                if not self._send_component_service(
                    robot, WRCTask.PUT_DOWN_COMPONENT_B, area_n,
                    f"放下零件B到{p3_n}", robot_id,
                ):
                    return

                self._p3_tracker.set_state(p3_n, BoxSlotState.FULL)
                logger.info("WRC", f"{p3_n} → FULL（零件A+B已放齐）")

                # ── 7. 在 P3 按 P4 状态搬满箱（已在 p3_n 前，无需先去 P4）────
                p4_state = self._p4_tracker.get_state("P4")
                logger.info("WRC", f"[{robot_id}] P4 状态={p4_state.value}")

                if p4_state == BoxSlotState.FULL:
                    # P4 已满：留在 P3-n 前等待 robot_b 装配完成（P4→EMPTY），再换箱
                    # 不能回 P1 开新周期，否则 P3-n 满箱会积压无人搬运
                    logger.info(
                        "WRC",
                        f"[{robot_id}] P4 已满，在 {p3_n} 前等待装配腾空...",
                    )
                    self.task_state_machine.update_step(
                        robot_id, WRCStep.WAITING_NEXT_STEP,
                        f"在{p3_n}前等待P4装配腾空",
                    )
                    while self._p4_tracker.get_state("P4") == BoxSlotState.FULL:
                        if not self._check_flow_control(robot_id):
                            return
                        time.sleep(1)
                    p4_state = self._p4_tracker.get_state("P4")
                    logger.info(
                        "WRC",
                        f"[{robot_id}] P4 已腾空，状态={p4_state.value}，继续搬箱",
                    )

                if p4_state == BoxSlotState.NO_BOX:
                    # P4 无箱：首轮可直接把满箱从 P3-n 搬到 P4
                    self.task_state_machine.update_step(
                        robot_id, WRCStep.ACTION_PUT_BOX_AT_P4, "把满箱子从P3搬到P4",
                    )
                    if is_robot_at_pose(
                        robot, getattr(NavigationPose, p3_n),
                        WRCAtPoseTolerance.DISTANCE, WRCAtPoseTolerance.HEADING,
                        timeout=WRCAtPoseTolerance.TIMEOUT,
                    ):
                        logger.info("WRC", f"[{robot_id}] 已在{p3_n}，跳过导航")
                    else:
                        if not self._navigate(robot, p3_n, f"{p3_n}满箱子点位", robot_id):
                            return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PICK_UP_BOX, area_n,
                        "搬起满箱子", robot_id,
                    ):
                        return
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PUT_DOWN_BOX, WRCPose.P4,
                        "放下满箱子", robot_id,
                    ):
                        return
                    self._p3_tracker.set_state(p3_n, BoxSlotState.NO_BOX)
                    self._p4_tracker.set_state("P4", BoxSlotState.FULL)
                    logger.info("WRC", f"首轮搬箱完成：{p3_n}→NO_BOX，P4→FULL")

                    if not self._return_after_box_cycle(robot, robot_id):
                        return

                elif p4_state == BoxSlotState.EMPTY:
                    # P4 有空箱：空箱→P3-k，满箱 P3-n→P4
                    p3_k = self._p3_tracker.find_slot_by_state(BoxSlotState.NO_BOX)
                    if p3_k is None:
                        msg = "P3 区域没有无箱子槽位，无法接收P4空箱: " + self._p3_tracker.get_status_display()
                        logger.error("WRC", msg)
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    area_k = getattr(WRCPose, p3_k)

                    self.task_state_machine.update_step(
                        robot_id, WRCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P4搬到P3空槽",
                    )
                    if not self._navigate(robot, NavigationPose.P4, "P4点位取空箱", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PICK_UP_BOX, WRCPose.P4,
                        "搬起空箱子", robot_id,
                    ):
                        return
                    if not self._navigate(robot, p3_k, f"{p3_k}空点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PUT_DOWN_BOX, area_k,
                        "放下空箱子", robot_id,
                    ):
                        return
                    self._p4_tracker.set_state("P4", BoxSlotState.NO_BOX)
                    self._p3_tracker.set_state(p3_k, BoxSlotState.EMPTY)

                    self.task_state_machine.update_step(
                        robot_id, WRCStep.ACTION_PUT_BOX_AT_P4, "把满箱子从P3搬到P4",
                    )
                    if not self._navigate(robot, p3_n, f"{p3_n}满箱子点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PICK_UP_BOX, area_n,
                        "搬起满箱子", robot_id,
                    ):
                        return
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PUT_DOWN_BOX, WRCPose.P4,
                        "放下满箱子", robot_id,
                    ):
                        return
                    self._p3_tracker.set_state(p3_n, BoxSlotState.NO_BOX)
                    self._p4_tracker.set_state("P4", BoxSlotState.FULL)
                    logger.info("WRC", f"搬箱完成：{p3_n}→NO_BOX，P4→FULL")

                    if not self._return_after_box_cycle(robot, robot_id):
                        return

                else:
                    # 非预期状态：跳过搬箱，继续分拣循环
                    logger.info(
                        "WRC",
                        f"[{robot_id}] P4 状态={p4_state.value}，跳过搬箱，继续分拣循环",
                    )

                logger.info("WRC", f"[{robot_id}] 一个分拣搬运周期完成，等待下一周期...")

        except Exception as e:
            logger.exception_occurred("WRC", f"分拣搬运流程异常 (task={task_id})", e)
            self.task_state_machine.set_error(f"执行异常: {e}", robot_id)
        finally:
            self.task_state_machine.mark_robot_done(robot_id)


    # ──────────────────────────────────────────────────────────────────────────
    # 机器人流程：分拣搬运流程
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_trans_component_async2(self, task_id: str, robot_id: str = "robot_a"):
        """
        后台线程：WRC 演示流程（robot_a / WA2）。

        流程（对照流程图）：
          1. 等待人工复位（robot_c 拆垛后 P1→FULL，一箱含两套零件）
          2. 确认/导航到 P1，等待物料就绪（FULL 或 HALF）
          3. Service 在 P1 抓取零件 A
          4. 选择 P3 空料箱 n，导航到 P3，放下零件 A（槽位→HALF）
             同时消耗一套零件：P1 FULL→HALF，或 HALF→EMPTY
          5. 导航到 P2，Service 抓取零件 B
          6. 再导航回同一 P3 槽位 n，放下零件 B（槽位→FULL）
          7. 在 P3 直接搬满箱到 P4（按 P4 状态分支）：
               P4 无箱：满箱直接从 P3-n 搬到 P4，再按零件余量回点
               P4 为空：pick_up_box/put_down_box 交换空满箱，再按零件余量回点
               P4 已满：留在 P3-n 前等待装配腾空（P4→EMPTY），再执行换箱，再回点
               回点规则：P1 仍为 HALF → 回 P1 继续第二套；P1 已 EMPTY（两套抓完）→ 回 home 等补料
               其它：回环继续分拣
        """
        robot = self._get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return

        try:
            logger.info("WRC", f"分拣搬运流程启动 (task={task_id}, robot={robot_id})")

            while True:
                # ── 1. 等待 MANUAL_RESET_COMPLETED（robot_c 拆垛后 P1→FULL）──
                logger.info("WRC", f"[{robot_id}] 等待 MANUAL_RESET_COMPLETED / 料箱就绪...")
                self.task_state_machine.update_step(
                    robot_id, WRCStep.WAITING_MANUAL_RESET, "等待人工复位完成信号",
                )
                while (
                    not self._manual_reset_event.is_set()
                    and self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY
                ):
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(0.5)
                self._manual_reset_event.clear()

                # 等待 P1 物料就绪（FULL=两套零件，HALF=剩一套；EMPTY 则等待补料）
                logger.info("WRC", f"[{robot_id}] 等待 P1 物料就绪...")
                deadline = time.time() + WRCTimeout.P1_WAIT
                while self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    if not self._check_flow_control(robot_id):
                        return
                    if time.time() > deadline:
                        msg = f"等待 P1 物料超时（{WRCTimeout.P1_WAIT}s）"
                        logger.error("WRC", f"[{robot_id}] {msg}")
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    time.sleep(1)
                logger.info(
                    "WRC",
                    f"[{robot_id}] P1 物料已就绪（{self._p1_tracker.get_state('P1').value}），"
                    f"开始抓取零件A",
                )

                # p4点位已满判断
                if self._p4_tracker.get_state("P4") == BoxSlotState.FULL:
                    # P4 已满：留在 P3-n 前等待 robot_b 装配完成（P4→EMPTY），再换箱
                    while self._p4_tracker.get_state("P4") == BoxSlotState.FULL:
                        if not self._check_flow_control(robot_id):
                            return
                        time.sleep(2)

                # 如果P1状态为FULL，则执行搬箱子动作
                if self._p1_tracker.get_state("P1") == BoxSlotState.FULL:
                    if not self._navigate(robot, NavigationPose.P3_2, "P3_2点位", robot_id):
                        return
                    if not self._send_box_service(
                            robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PICK_UP_BOX, WRCPose.P3_2,
                            "抓取空箱子在P3_2点位", robot_id,
                        ):
                        return
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PUT_DOWN_BOX, WRCPose.P4,
                        "放下空箱子在P4点位", robot_id,
                    ):
                        return
                    self._p3_tracker.set_state("P3_2", BoxSlotState.NO_BOX)
                    self._p4_tracker.set_state("P4", BoxSlotState.EMPTY)

                if not self._navigate(robot, NavigationPose.P1, "P1点位", robot_id):
                    return
                if not self._send_component_service(
                    robot, WRCTask.PICK_UP_COMPONENT_A, WRCPose.P1,
                    "抓取零件A在P1点位", robot_id,
                ):
                    return
                if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                    return
                if not self._send_component_service(
                    robot, WRCTask.PUT_DOWN_COMPONENT_A, WRCPose.P4,
                    "放下零件A在P4点位", robot_id,
                ):
                    return
                if not self._navigate(robot, NavigationPose.P2, "P2点位", robot_id):
                    return
                if not self._send_component_service(
                    robot, WRCTask.PICK_UP_COMPONENT_B, WRCPose.P2,
                    "抓取零件B在P2点位", robot_id,
                ):
                    return
                if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                    return
                if not self._send_component_service(
                    robot, WRCTask.PUT_DOWN_COMPONENT_B, WRCPose.P4,
                    "放下零件B在P4点位", robot_id,
                ):
                    return
                # 设置P4状态为FULL
                self._p4_tracker.set_state("P4", BoxSlotState.FULL)
                # 一箱两套零件：FULL→HALF（首套抓完），HALF→EMPTY（第二套抓完）
                p1_before = self._p1_tracker.get_state("P1")
                if p1_before == BoxSlotState.FULL:
                    self._p1_tracker.set_state("P1", BoxSlotState.HALF)
                elif p1_before == BoxSlotState.HALF:
                    self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)
                else:
                    self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)


                # 如果P1状态变为empty，则等待P4状态变为empty后，执行搬箱子动作
                if self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    while self._p4_tracker.get_state("P4") == BoxSlotState.FULL:
                        time.sleep(1)
                if self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PICK_UP_BOX, WRCPose.P4,
                        "搬起满箱子", robot_id,
                    ):
                        return
                    if not self._navigate(robot, NavigationPose.P3_2, "P3_2点位", robot_id):
                        return
                    if not self._send_box_service(
                        robot, WRCService.ROBOT_TASK_GEELY, WRCTask.PUT_DOWN_BOX, WRCPose.P3_2,
                        "放下满箱子", robot_id,
                    ):
                        return
                    if not self._navigate(robot, NavigationPose.home, "home点位", robot_id):
                        return
                

                logger.info("WRC", f"[{robot_id}] 一个分拣搬运周期完成，等待下一周期...")

        except Exception as e:
            logger.exception_occurred("WRC", f"分拣搬运流程异常 (task={task_id})", e)
            self.task_state_machine.set_error(f"执行异常: {e}", robot_id)
        finally:
            self.task_state_machine.mark_robot_done(robot_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 私有工具方法
    # ──────────────────────────────────────────────────────────────────────────

    def _get_robot(self, robot_id: str):
        return self.robots.get(robot_id)


    def _check_flow_control(self, robot_id: str) -> bool:
        """
        在每个最小调度颗粒度动作完成后调用（已整合进 _check_result / _navigate）：
        - 流程已结束（_stop_event set） → 返回 False，调用方 return 退出
        - 流程暂停（_pause_event clear） → 阻塞等待，直到恢复或结束
        - 正常 → 返回 True，继续执行
        """
        if self._stop_event.is_set():
            logger.info("WRC", f"[{robot_id}] 流程已结束，停止执行")
            return False
        if not self._pause_event.is_set():
            logger.info("WRC", f"[{robot_id}] 流程已暂停，等待恢复...")
            self._pause_event.wait()   # 阻塞直到 PROCESS_RESUMED 或 PROCESS_ENDED
            if self._stop_event.is_set():
                logger.info("WRC", f"[{robot_id}] 暂停期间收到结束信号，停止执行")
                return False
        return True

    def _return_after_box_cycle(self, robot, robot_id: str) -> bool:
        """
        搬箱周期结束后的回点：
        - P1 已 EMPTY（第二套零件也抓完，P1/P2 料箱已空）→ 回 home 等待补料
        - P1 仍为 HALF（还剩一套）→ 回 P1 继续下一轮分拣
        """
        if self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
            logger.info("WRC", f"[{robot_id}] P1/P2 零件已空，导航回 home 等待补料")
            self.task_state_machine.update_step(
                robot_id, WRCStep.NAVIGATING_TO_HOME, "导航到home点位",
            )
            return self._navigate(robot, NavigationPose.home, "home点位", robot_id)

        logger.info("WRC", f"[{robot_id}] P1 仍有零件，导航回 P1 继续分拣")
        self.task_state_machine.update_step(
            robot_id, WRCStep.NAVIGATING_TO_P1, "导航到P1点位",
        )
        return self._navigate(robot, NavigationPose.P1, "P1点位", robot_id)

    def _place_box_to_sp(self, robot, robot_id: str) -> bool:
        """
        robot_c 拆垛 Service：一次调用向 P1、P2 各放置一箱零件。
        接口不再传 area。
        """
        label = "向P1与P2放置料箱"
        self.task_state_machine.update_step(
            robot_id, WRCStep.PLACING_BOX_AT_P1_P2, label,
        )
        result = robot.send_service_request_task(
            WRCService.ROBOT_TASK,
            task=WRCTask.PICK_BOX_TO_SP,
            maxtime=WRCTimeout.ROBOT_ACTION,
        )
        if not result:
            err = getattr(result, "error_msg", "") or "unknown"
            logger.error("WRC", f"[{robot_id}] {label}失败: {err}")
            return False
        logger.info("WRC", f"[{robot_id}] {label}成功")
        return True

    def _send_component_action(
        self, robot, task: str, area: str, label: str, robot_id: str,
    ) -> bool:
        """零件抓放走 Action（/robot_task/*）。保留备用，当前流程改用 Service。"""
        result = send_task_action(
            robot,
            task=task,
            area=area,
            spec=WRC_TASK_ACTION_SPEC,
            timeout=WRCTimeout.ROBOT_ACTION,
        )
        return self._check_result(result, label, robot_id)

    def _send_component_service(
        self, robot, task: str, area: str, label: str, robot_id: str,
    ) -> bool:
        """零件抓放走 Service：pick_up_component_* / put_down_component_*。"""
        result = robot.send_service_request_task(
            WRCService.ROBOT_TASK,
            task=task,
            area=area,
            maxtime=WRCTimeout.ROBOT_ACTION,
        )
        return self._check_result(result, label, robot_id)


    def _send_box_service(
        self, robot, service: str, task: str, area: str, label: str, robot_id: str,
    ) -> bool:
        """搬箱子走 Service：pick_up_box / put_down_box。"""
        result = robot.send_service_request_task(
            service,
            task=task,
            area=area,
            maxtime=WRCTimeout.ROBOT_ACTION,
        )
        return self._check_result(result, label, robot_id)


    def _navigate(self, robot, pose, label: str, robot_id: str) -> bool:
        """
        导航到目标点，失败时更新状态机并返回 False。
        若结果为 NavigationState.FAILED(7) 或 ABORTED(9)（可重试的暂态错误，
        如到点精度超差），最多重试 5 次；其它失败状态（CANCELLED / ERROR / CANCELING）
        不重试，直接返回 False。
        成功后调用 _check_flow_control：若流程被暂停则阻塞等待；若结束则返回 False。
        """
        # 字符串：NavigationPose 属性名，如 "P3_1" 或 nav_pose+"_mid" → "P3_1_mid"
        if isinstance(pose, str):
            resolved = getattr(NavigationPose, pose, None)
            if resolved is None:
                msg = f"导航到{label}失败: NavigationPose 无属性 '{pose}'"
                logger.error("WRC", f"[{robot_id}] {msg}")
                self.task_state_machine.set_error(msg, robot_id)
                return False
            pose = resolved

        if isinstance(pose, list) and pose and isinstance(pose[0], (list, tuple)):
            waypoints = pose
        elif isinstance(pose, (list, tuple)) and len(pose) == 7 and isinstance(pose[0], (int, float)):
            waypoints = [pose]
        else:
            waypoints = [pose]

        goal = build_navigation_goal(
            waypoints,
            distance_tolerance=WRCNavTolerance.DISTANCE,
            heading_tolerance=WRCNavTolerance.HEADING,
            translation_enable=True,
            translation_heading=WRCNavTolerance.TRANSLATION_HEADING,
        )

        def _on_feedback(fb):
            print(f"  [WRC 导航][{robot_id}] {label}: {fb.state.name}")

        # FAILED(7)：导航失败（含到点精度超差）；ABORTED(9)：actionlib 异常中止
        # 这两种属于可重试的暂态错误，其它状态（CANCELLED / ERROR / CANCELING）不重试
        _RETRYABLE_STATES = {NavigationState.FAILED, NavigationState.ABORTED}
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            result = send_navigation_action(
                robot,
                goal,
                feedback_callback=_on_feedback,
                retry_on_disconnect=True,
            )
            if result.succeeded:
                return self._check_flow_control(robot_id)

            if result.state not in _RETRYABLE_STATES:
                msg = f"导航到{label}失败: state={result.state.name}(value={result.state.value})"
                logger.error("WRC", f"[{robot_id}] {msg}")
                self.task_state_machine.set_error(msg, robot_id)
                return False

            if attempt < max_attempts:
                logger.warning(
                    "WRC",
                    f"[{robot_id}] 导航到{label} state={result.state.name}，"
                    f"第 {attempt}/{max_attempts} 次失败，1s 后重新导航...",
                )
                if not self._check_flow_control(robot_id):
                    return False
                time.sleep(1.0)
                continue

            msg = f"导航到{label}失败({result.state.name})，已重试 {max_attempts} 次"
            logger.error("WRC", f"[{robot_id}] {msg}")
            self.task_state_machine.set_error(msg, robot_id)
            return False

        return False

    def _check_result(self, result, label: str, robot_id: str) -> bool:
        """
        检查 send_service_request_task 返回值，失败时更新状态机并返回 False。
        成功后调用 _check_flow_control：若流程被暂停则阻塞等待；若结束则返回 False。
        这是暂停/停止的最小颗粒度检查点：每次 service call 返回后生效。
        """
        if not result:
            msg = f"{label}失败: {result.error_msg}"
            logger.error("WRC", f"[{robot_id}] {msg}")
            print(f"[{robot_id}] {msg}")
            self.task_state_machine.set_error(msg, robot_id)
            return False
        # 动作成功 → 检查流程控制状态（暂停/停止）
        return self._check_flow_control(robot_id)
