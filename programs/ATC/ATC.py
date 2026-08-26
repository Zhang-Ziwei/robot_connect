"""
ATC（零件转移）任务处理器

独立类，不继承、不修改旧项目任何文件。
仅导入以下稳定的底层接口，与旧项目业务逻辑解耦：

    ┌─────────────────────────────────────────────────────┐
    │  导入层级（由稳定到易变）                             │
    │                                                     │
    │  标准库（threading / uuid / time）       — 最稳定    │
    │  infrastructure.*（constants/logger）    — 稳定      │
    │  hardware.*（robot_controller/nav）      — 较稳定    │
    │  core.task_state_machine                 — 较稳定    │
    │  programs.ATC.constants（本项目专属）    — 自己维护  │
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

# ── ATC 专属常量（只改这里，不动旧项目）────────────────────────────────────────
from programs.ATC.constants import (
    ATCPose, ATCNavTolerance, ATCService, ATCTask, ATCStep, ATCTimeout,
    BoxSlotState, NavigationPose,
)
from programs.ATC.plc_modbus import ConveyorController

logger = get_error_logger()

# 空箱子状态持久化文件（相对于工作目录）
_ATC_STATE_FILE = os.path.join(os.path.dirname(__file__), "atc_box_state.json")


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


class ATCHandler:
    """
    ATC 零件转移任务处理器（独立类）。

    使用方式（在 main.py 或路由层中注册）：

        atc = ATCHandler(robots={"robot_a": robot_a}, task_state_machine=tsm)

        # HTTP 命令路由
        "TRANS_COMPONENT" → atc.handle_trans_component(cmd_data)
        "ATC_NEXT_STEP"   → atc.handle_next_step(cmd_data)
    """

    def __init__(self, robots: dict, conveyor: Optional["ConveyorController"] = None):
        self.robots = robots
        # 多机器人并行状态机：任务级 + 每台机器人独立步骤追踪
        self.task_state_machine = ParallelTaskStateMachine(list(robots.keys()))

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
        # MANUAL_RESET_COMPLETED / PROCESS_PAUSED / PROCESS_RESUMED / PROCESS_ENDED
        # 只在流程激活后才生效，防止误触发。
        self._process_active: bool = False
        # MANUAL_RESET_COMPLETED 置位后，robot_a 分拣线程才导航到 P1
        self._manual_reset_event: threading.Event = threading.Event()

        # ── 传送带控制器 ──────────────────────────────────────────────────────
        # 外部可传入 None（测试时跳过真实 PLC）或自定义实例（mock / 覆盖配置）；
        # 正常启动时不传，由 ATCHandler 自己负责创建，调用方无需感知 ConveyorController。
        self._conveyor: ConveyorController = conveyor if conveyor is not None else ConveyorController()

        # P1 区域槽位暂存器（存放零件A的空箱子）
        self._p1_tracker = SlotTracker(
            name="P1",
            slots=["P1"],
            state_file=os.path.join(os.path.dirname(__file__), "atc_p1_slots.json"),
            default_state=BoxSlotState.EMPTY,
        )

        # P3 区域槽位暂存器（存放零件B的空箱子）
        self._p3_tracker = SlotTracker(
            name="P3",
            slots=["P3_1", "P3_2"],
            state_file=os.path.join(os.path.dirname(__file__), "atc_p3_slots.json"),
            default_state=BoxSlotState.EMPTY,
        )
        # P4 只有一个点位，直接用 SlotTracker 管理单槽状态
        self._p4_tracker = SlotTracker(
            name="P4",
            slots=["P4"],
            state_file=os.path.join(os.path.dirname(__file__), "atc_p4_slots.json"),
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

        logger.info("ATC", f"TRANS_COMPONENT 任务启动: cmd_id={cmd_id}, robot={robot_id}")

        # 3. 初始化状态机（任务级 + 两台机器人步骤追踪同时启动）
        self.task_state_machine.start_task(cmd_id)

        # 预设 P1 为 FULL，线程启动后立即跳过等待直接开始分拣
        #self._p1_tracker.set_state("P1", BoxSlotState.FULL)

        # 4. 后台线程执行任务
        t = threading.Thread(
            target=self._execute_trans_component_async,
            args=(cmd_id, robot_id_trans),
            daemon=True,
            name=f"ATC-{cmd_id}",
        )
        t.start()

        # 5. 后台线程执行装配流程
        t = threading.Thread(
            target=self._execute_assemble_async,
            args=(cmd_id, robot_id_assemble),
            daemon=True,
            name=f"ATC-{cmd_id}",
        )
        t.start()
        
        return make_success_response(
            "ATC 零件转移任务已启动",
            cmd_id=cmd_id,
            robot_id=robot_id,
            note="使用 GET_TASK_STATE 命令查询任务状态",
        )

    def handle_next_step(self, cmd_data: Dict) -> Dict:
        """
        处理 ATC_NEXT_STEP 命令。
        由外部（人工确认后）通过 HTTP 调用，触发等待中的流程继续执行。
        """
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params", {})

        if not self.task_state_machine.is_busy():
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                "当前没有正在运行的 ATC 任务",
                cmd_id=cmd_id,
            )

        logger.info("ATC", f"收到 NEXT_STEP 信号 (cmd_id={cmd_id})")
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
        重置流程控制标志，P1 初始为 EMPTY（等待 MANUAL_RESET_COMPLETED 触发传送带送料）。
        两台机器人以 continuous=True 模式循环执行，直到收到 PROCESS_ENDED。
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

        # 验证机器人存在
        if self._get_robot(robot_id_trans) is None:
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {robot_id_trans} 不存在",
                cmd_id=cmd_id,
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

        # 初始化 P3 槽位状态（P1 由外部控制：handle_trans_component 预设 FULL，
        # handle_process_begins 预设 EMPTY 等待 MANUAL_RESET_COMPLETED）
        self._p3_tracker.set_state("P3_1", BoxSlotState.EMPTY)
        self._p3_tracker.set_state("P3_2", BoxSlotState.NO_BOX)
        self._p4_tracker.set_state("P4", BoxSlotState.EMPTY)

        # 启动两台机器人的后台线程
        threading.Thread(
            target=self._execute_trans_component_async,
            args=(cmd_id, robot_id_trans),
            daemon=True,
            name=f"ATC-Trans-{cmd_id}",
        ).start()
        threading.Thread(
            target=self._execute_assemble_async,
            args=(cmd_id, robot_id_assemble),
            daemon=True,
            name=f"ATC-Assemble-{cmd_id}",
        ).start()

        logger.info("ATC", f"PROCESS_BEGINS: 连续流程已启动 (cmd_id={cmd_id})")
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
        logger.info("ATC", f"PROCESS_PAUSED: 流程将在下一动作完成后暂停 (cmd_id={cmd_id})")
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
        logger.info("ATC", f"PROCESS_RESUMED: 流程已恢复 (cmd_id={cmd_id})")
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
        logger.info("ATC", f"PROCESS_ENDED: 流程已结束 (cmd_id={cmd_id})")
        return make_success_response("流程已结束", cmd_id=cmd_id)

    def handle_manual_reset_completed(self, cmd_data: Dict) -> Dict:
        """
        MANUAL_RESET_COMPLETED：人工复位完成。
        1. 通知 robot_a 分拣线程可导航到 P1；
        2. 后台线程启动传送带正转 → 完成后将 P1 置为 FULL（有物料）。
        仅在 PROCESS_BEGINS 之后生效。
        """
        cmd_id = cmd_data.get("cmd_id")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED, "流程尚未启动，请先发送 PROCESS_BEGINS", cmd_id=cmd_id
            )

        self._manual_reset_event.set()

        def _on_manual_reset():
            # 1. PLC 操作：传送带正转
            #conveyor_done = self._run_conveyor_forward()
            conveyor_done = True
            # 2. 业务逻辑：PLC 确认完成后，才将 P1 设为 FULL（物料已就绪）
            if conveyor_done:
                self._p1_tracker.set_state("P1", BoxSlotState.FULL)
                logger.info("ATC", "人工复位流程完成：传送带正转成功，P1 已设置为 FULL")
            else:
                logger.error("ATC", "传送带正转失败，P1 状态未更新，请检查 PLC 连接")

        threading.Thread(
            target=_on_manual_reset,
            daemon=True,
            name="ATC-ManualReset",
        ).start()

        return make_success_response(
            "已接收人工复位完成信号，传送带正转启动中，完成后 P1 将置为 FULL",
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
            logger.info("ATC", f"装配流程启动 (task={task_id}, robot={robot_id})")

            while True:
                # ── 等待 P4 有满箱子 ──────────────────────────────────────────────
                logger.info("ATC", f"[{robot_id}] 等待 P4 有满箱子（robot_a 将满箱搬到 P4 后触发）...")
                while self._p4_tracker.get_state("P4") != BoxSlotState.FULL:
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(1)
                logger.info("ATC", f"[{robot_id}] P4 满箱就绪，开始装配")

                # 1. 执行装配，直到P4料箱拿完
                self.task_state_machine.update_step(robot_id, ATCStep.ASSEMBLY, "装配中")
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.ASSEMBLY,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "装配", robot_id):
                    return
                
                # 更新P4槽位状态为EMPTY
                self._p4_tracker.set_state("P4", BoxSlotState.EMPTY)

                # 2. 继续装配，直到装配完成
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.CONTINUE_ASSEMBLY,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "继续装配", robot_id):
                    return

                logger.info("ATC", f"[{robot_id}] 装配完成，等待下一周期...")

        except Exception as e:
            logger.exception_occurred("ATC", f"装配流程异常 (task={task_id})", e)
            self.task_state_machine.set_error(f"装配异常: {e}", robot_id)
        finally:
            self.task_state_machine.mark_robot_done(robot_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 机器人流程：分拣搬运流程
    # ──────────────────────────────────────────────────────────────────────────

    def _execute_trans_component_async(self, task_id: str, robot_id: str = "robot_a"):
        """
        后台线程：执行 ATC 零件转移完整流程（robot_a）。
        无论成功/失败，finally 中均调用 mark_robot_done 确保状态机能正确结束。
        """
        robot = self._get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return
        
        try:
            logger.info("ATC", f"分拣搬运流程启动 (task={task_id}, robot={robot_id})")

            # ── 分拣流程开始 ───────────────────────────────────────────────────────────


            while True:
                '''p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.EMPTY)
                #self._navigate(robot, NavigationPose.P1_v, "P1_v点位", robot_id)
                self._navigate(robot, p3_slot+"_mid", "P1点位", robot_id)
                input("111111111111111111")'''
                # 参数：箱子里有2个零件，或者一个零件
                component_two = False
                # ── T1：分拣零件A和B ────────────────────────────────────────────────────
                # 1. 等待 MANUAL_RESET_COMPLETED，收到后再导航到 P1
                logger.info("ATC", f"[{robot_id}] 等待 MANUAL_RESET_COMPLETED 指令...")
                self.task_state_machine.update_step(
                    robot_id, ATCStep.WAITING_MANUAL_RESET, "等待人工复位完成信号",
                )
                while not self._manual_reset_event.is_set() and self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(0.5)
                self._manual_reset_event.clear()
                # 导航到P1点位
                self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P1, "导航到P1点位")
                if is_robot_at_pose(robot, NavigationPose.P1):
                    logger.info("ATC", f"[{robot_id}] 机器人已在P1点位，跳过导航")
                else:
                    if not self._navigate(robot, NavigationPose.P1_mid, "P1点位", robot_id):
                        return

                # ── P1上有物料时，会触发流程再次开始 ──────────────────────────────────────────────────
                logger.info("ATC", f"[{robot_id}] 等待 P1 物料就绪（MANUAL_RESET_COMPLETED 触发传送带送料）...")
                deadline = time.time() + ATCTimeout.P1_WAIT
                while self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                    if not self._check_flow_control(robot_id):
                        return
                    if time.time() > deadline:
                        msg = f"等待 P1 物料超时（{ATCTimeout.P1_WAIT}s）"
                        logger.error("ATC", f"[{robot_id}] {msg}")
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    time.sleep(1)
                logger.info("ATC", f"[{robot_id}] P1 物料已就绪，开始分拣")

                # 2. 执行P1点位抓取零件A操作
                self.task_state_machine.update_step(robot_id, ATCStep.PICK_UP_COMPONENT_A_AT_P1, "抓取零件A在P1点位")
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.PICK_UP_COMPONENT_A,
                    area=ATCPose.P1,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "抓取零件A在P1点位", robot_id):
                    return
                #input("22222222222222222")

                # 3. 导航到P2点位
                self.task_state_machine.update_step(
                    robot_id, ATCStep.NAVIGATING_TO_P2,
                    "导航到P2点位",
                )
                if not self._navigate(robot, NavigationPose.P2, "P2点位", robot_id):
                    return
                #input("33333333333333333")
                
                # 4. 执行P2点位抓取零件B操作
                self.task_state_machine.update_step(robot_id, ATCStep.PICK_UP_COMPONENT_B_AT_P2, "双手抓取零件B在P2点位")
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.PICK_UP_COMPONENT_B,
                    area=ATCPose.P2,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, "双手抓取零件B在P2点位", robot_id):
                    return
                #input("44444444444444444")

                # PLC 操作：传送带反转
                if self._p1_tracker.get_state("P1") == BoxSlotState.HALF:
                    self._trigger_conveyor_reverse()
                elif component_two == False:
                    self._trigger_conveyor_reverse()

                # ── P3：根据槽位状态选择空箱子 ───────────────────────────
                logger.info("ATC", f"P3 槽位状态:\n{self._p3_tracker.get_status_display()}")

                # 找到有空箱子（EMPTY）的槽位（先判空再取属性，防 AttributeError）
                p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.EMPTY)
                if p3_slot is None:
                    msg = "P3 区域没有空箱子槽位: " + self._p3_tracker.get_status_display()
                    logger.error("ATC", msg)
                    self.task_state_machine.set_error(msg, robot_id)
                    return

                # nav_pose  → NavigationPose 属性名（字符串），_navigate 内部做查找
                target_pose = getattr(ATCPose, p3_slot)    # "point_3-1" / "point_3-2"
                nav_pose = p3_slot  # 如 "P3_1"；需要中间点时用 nav_pose+"_mid"
                logger.info("ATC", f"选择 {p3_slot}（状态: {self._p3_tracker.get_state(p3_slot).value}）")

                # 5. 导航到P3点位（经中间点）
                self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P3, "导航到P3点位")
                if not self._navigate(robot, nav_pose + "_mid", "P3点位", robot_id):
                    return
                #input("55555555555555555")

                # 6. 执行放置动作：放下零件A和B到空箱子
                self.task_state_machine.update_step(
                    robot_id, ATCStep.PUT_DOWN_COMPONENT_A_AND_B_AT_P3, f"放下零件A和B到{p3_slot}"
                )
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.PUT_DOWN_COMPONENT_A,
                    area=target_pose,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, f"放下零件A到{p3_slot}", robot_id):
                    return

                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK,
                    task=ATCTask.PUT_DOWN_COMPONENT_B,
                    area=target_pose,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                )
                if not self._check_result(result, f"放下零件B到{p3_slot}", robot_id):
                    return
                #input("66666666666666666")

                # 更新槽位状态
                self._p3_tracker.set_state(p3_slot, BoxSlotState.FULL)
                if component_two:
                    if self._p1_tracker.get_state("P1") == BoxSlotState.HALF:
                        self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)
                    else:
                        self._p1_tracker.set_state("P1", BoxSlotState.HALF)
                    logger.info("ATC", f"{p3_slot} 状态 → {BoxSlotState.FULL.value}")
                else:
                    self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)


                # ── 分拣流程结束 ─────────────────────────────────────────


                # ── 搬箱子流程开始 ───────────────────────────────────────────────────────────

                # 7. 导航到P4点位
                self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P4, "导航到P4点位")
                if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                    return
                #input("77777777777777777")
                # 如果P4槽位是空箱子，则开始搬箱子流程
                # 等待 P4 有满箱子可搬运（MANUAL_RESET_COMPLETED 会将 P4 标记为 EMPTY，
                # 当 robot_a 把满箱子搬到 P4 后，P4 变为 FULL；这里等待 P4 变为 EMPTY 的
                # 含义是：等待 robot_b 完成装配并取走（或人工取走）P4 的满箱子）
                while self._p4_tracker.get_state("P4") != BoxSlotState.EMPTY:
                    if not self._check_flow_control(robot_id):
                        return
                    time.sleep(2)

                # 搬箱子流程
                # ──T3：把空箱子从P4点位搬到P3无箱子点位,把满箱子从P3满箱子点位搬到P4点位───────────────────────────────────────────
                # 获取槽位状态为NO_BOX的槽位
                p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.NO_BOX)
                if p3_slot is None:
                    msg = "P3 区域没有无箱子槽位: " + self._p3_tracker.get_status_display()
                    logger.error("ATC", msg)
                    self.task_state_machine.set_error(msg, robot_id)
                    return
                target_pose = getattr(ATCPose, p3_slot)
                nav_pose = p3_slot

                # 8. 执行动作：把箱子从当前点位搬到P3无箱子点位
                self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P4搬到P3空槽")
                '''result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PICK_BOX_TO_SP,
                    area=target_pose,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                    extra_params={"current_pose": ATCPose.P4},
                )
                if not self._check_result(result, "把空箱子从P4搬到P3空槽", robot_id):
                    return'''

                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PICK_BOX,
                    area=ATCPose.P4,
                    maxtime=ATCTimeout.ROBOT_ACTION
                )
                if not self._check_result(result, "搬起空箱子", robot_id):
                    return

                if not self._navigate(robot, p3_slot, "P3空点位", robot_id):
                    return
                
                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PUT_BOX,
                    area=target_pose,
                    maxtime=ATCTimeout.ROBOT_ACTION
                )
                if not self._check_result(result, "放下空箱子", robot_id):
                    return

                #input("88888888888888888")

                # 更新P4槽位状态为NO_BOX，P3槽位状态为EMPTY
                self._p4_tracker.set_state("P4", BoxSlotState.NO_BOX)
                self._p3_tracker.set_state(p3_slot, BoxSlotState.EMPTY)

                # 获取P3槽位状态为FULL的槽位
                p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.FULL)
                if p3_slot is None:
                    msg = "P3 区域没有满箱子槽位: " + self._p3_tracker.get_status_display()
                    logger.error("ATC", msg)
                    self.task_state_machine.set_error(msg, robot_id)
                    return
                target_pose = getattr(ATCPose, p3_slot)
                nav_pose = p3_slot

                # 9. 导航到P3满箱子点位
                self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P3, "导航到P3满箱子点位")
                if not self._navigate(robot, nav_pose, "P3满箱子点位", robot_id):
                    return
                #input("99999999999999999")

                # 10. 执行动作：把满箱子从P3满箱子点位搬到P4点位
                self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P4, "把满箱子从P3搬到P4")
                '''result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PICK_BOX_TO_SP,
                    area=ATCPose.P4,
                    maxtime=ATCTimeout.ROBOT_ACTION,
                    extra_params={"current_pose": target_pose},
                )
                if not self._check_result(result, "把满箱子从P3搬到P4", robot_id):
                    return'''

                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PICK_BOX,
                    area=target_pose,
                    maxtime=ATCTimeout.ROBOT_ACTION
                )
                if not self._check_result(result, "搬起满箱子", robot_id):
                    return

                if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                    return

                result = robot.send_service_request_task(
                    ATCService.ROBOT_TASK_GEELY,
                    task=ATCTask.PUT_BOX,
                    area=ATCPose.P4,
                    maxtime=ATCTimeout.ROBOT_ACTION
                )
                if not self._check_result(result, "放下满箱子", robot_id):
                    return
                #input("101010101010101010")

                # 更新P3槽位状态为NO_BOX，P4槽位状态为FULL
                self._p3_tracker.set_state(p3_slot, BoxSlotState.NO_BOX)
                self._p4_tracker.set_state("P4", BoxSlotState.FULL)

                # ── 搬箱子流程结束 ───────────────────────────────────────────────────────────

                # 11. 导航到home点位
                self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_HOME, "导航到home点位")
                if not self._navigate(robot, NavigationPose.home, "home点位", robot_id):
                    return
                #input("111111111111111111")

                logger.info("ATC", f"[{robot_id}] 一个分拣搬运周期完成，等待下一周期...")

        except Exception as e:
            logger.exception_occurred("ATC", f"分拣搬运流程异常 (task={task_id})", e)
            self.task_state_machine.set_error(f"执行异常: {e}", robot_id)
        finally:
            self.task_state_machine.mark_robot_done(robot_id)

    def _execute_trans_component_async_1(self, task_id: str, robot_id: str = "robot_a"):
            """
            后台线程：执行 ATC 零件转移完整流程（robot_a）。
            无论成功/失败，finally 中均调用 mark_robot_done 确保状态机能正确结束。
            """
            robot = self._get_robot(robot_id)
            if robot is None:
                self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
                self.task_state_machine.mark_robot_done(robot_id)
                return

            try:
                logger.info("ATC", f"分拣搬运流程启动 (task={task_id}, robot={robot_id})")

                # ── 分拣流程开始 ───────────────────────────────────────────────────────────


                while True:
                    # 参数：箱子里有2个零件，或者一个零件
                    component_two = True
                    # ── T1：分拣零件A和B ────────────────────────────────────────────────────
                    # 1. 等待 MANUAL_RESET_COMPLETED，收到后再导航到 P1
                    logger.info("ATC", f"[{robot_id}] 等待 MANUAL_RESET_COMPLETED 指令...")
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.WAITING_MANUAL_RESET, "等待人工复位完成信号",
                    )
                    while not self._manual_reset_event.is_set() and self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                        if not self._check_flow_control(robot_id):
                            return
                        time.sleep(0.5)
                    self._manual_reset_event.clear()
                    # 导航到P1点位
                    self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P1, "导航到P1点位")
                    if is_robot_at_pose(robot, NavigationPose.P1):
                        logger.info("ATC", f"[{robot_id}] 机器人已在P1点位，跳过导航")
                    else:
                        if not self._navigate(robot, NavigationPose.P1_mid, "P1点位", robot_id):
                            return

                    # ── P1上有物料时，会触发流程再次开始 ──────────────────────────────────────────────────
                    logger.info("ATC", f"[{robot_id}] 等待 P1 物料就绪（MANUAL_RESET_COMPLETED 触发传送带送料）...")
                    deadline = time.time() + ATCTimeout.P1_WAIT
                    while self._p1_tracker.get_state("P1") == BoxSlotState.EMPTY:
                        if not self._check_flow_control(robot_id):
                            return
                        if time.time() > deadline:
                            msg = f"等待 P1 物料超时（{ATCTimeout.P1_WAIT}s）"
                            logger.error("ATC", f"[{robot_id}] {msg}")
                            self.task_state_machine.set_error(msg, robot_id)
                            return
                        time.sleep(1)
                    logger.info("ATC", f"[{robot_id}] P1 物料已就绪，开始分拣")

                    # 2. 执行动作：把箱子从当前点位搬到P3-1点位
                    self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P1搬到P3空槽")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK_GEELY,
                        task=ATCTask.PICK_BOX_TO_SP,
                        area=ATCPose.P3_1,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                        extra_params={"current_pose": ATCPose.P1},
                    )
                    if not self._check_result(result, "把空箱子从P4搬到P3空槽", robot_id):
                        return
                    
                    # 3. 导航到P2点位
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.NAVIGATING_TO_P2,
                        "导航到P2点位",
                    )
                    if not self._navigate(robot, NavigationPose.P2_mid, "P2点位", robot_id):
                        return

                    # 4. 执行动作：把箱子从当前点位搬到P3-2点位
                    self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P2搬到P3空槽")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK_GEELY,
                        task=ATCTask.PICK_BOX_TO_SP,
                        area=ATCPose.P3_2,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                        extra_params={"current_pose": ATCPose.P2},
                    )
                    if not self._check_result(result, "把空箱子从P4搬到P3空槽", robot_id):
                        return

                    # 5. 导航到P4点位
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.NAVIGATING_TO_P2,
                        "导航到P2点位",
                    )
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return

                    # 6. 执行动作：把箱子从当前点位搬到P1点位
                    self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P4搬到P1")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK_GEELY,
                        task=ATCTask.PICK_BOX_TO_SP,
                        area=ATCPose.P3_2,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                        extra_params={"current_pose": ATCPose.P2},
                    )
                    if not self._check_result(result, "把空箱子从P4搬到P3空槽", robot_id):
                        return

                    # 5. 导航到P3-1点位
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.NAVIGATING_TO_P3,
                        "导航到P3-1点位",
                    )
                    if not self._navigate(robot, NavigationPose.P3_1_mid, "P3-1点位", robot_id):
                        return








                    # 3. 导航到P2点位
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.NAVIGATING_TO_P2,
                        "导航到P2点位",
                    )
                    if not self._navigate(robot, NavigationPose.P2, "P2点位", robot_id):
                        return
                    #input("33333333333333333")
                    
                    # 4. 执行P2点位抓取零件B操作
                    self.task_state_machine.update_step(robot_id, ATCStep.PICK_UP_COMPONENT_B_AT_P2, "双手抓取零件B在P2点位")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK,
                        task=ATCTask.PICK_UP_COMPONENT_B,
                        area=ATCPose.P2,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                    )
                    if not self._check_result(result, "双手抓取零件B在P2点位", robot_id):
                        return
                    #input("44444444444444444")

                    # PLC 操作：传送带反转
                    if self._p1_tracker.get_state("P1") == BoxSlotState.HALF:
                        self._trigger_conveyor_reverse()
                    elif component_two == False:
                        self._trigger_conveyor_reverse()

                    # ── P3：根据槽位状态选择空箱子 ───────────────────────────
                    logger.info("ATC", f"P3 槽位状态:\n{self._p3_tracker.get_status_display()}")

                    # 找到有空箱子（EMPTY）的槽位（先判空再取属性，防 AttributeError）
                    p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.EMPTY)
                    if p3_slot is None:
                        msg = "P3 区域没有空箱子槽位: " + self._p3_tracker.get_status_display()
                        logger.error("ATC", msg)
                        self.task_state_machine.set_error(msg, robot_id)
                        return

                    # nav_pose  → NavigationPose 属性名（字符串），_navigate 内部做查找
                    target_pose = getattr(ATCPose, p3_slot)    # "point_3-1" / "point_3-2"
                    nav_pose = p3_slot  # 如 "P3_1"；需要中间点时用 nav_pose+"_mid"
                    logger.info("ATC", f"选择 {p3_slot}（状态: {self._p3_tracker.get_state(p3_slot).value}）")

                    # 5. 导航到P3点位（经中间点）
                    self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P3, "导航到P3点位")
                    if not self._navigate(robot, nav_pose + "_mid", "P3点位", robot_id):
                        return
                    #input("55555555555555555")

                    # 6. 执行放置动作：放下零件A和B到空箱子
                    self.task_state_machine.update_step(
                        robot_id, ATCStep.PUT_DOWN_COMPONENT_A_AND_B_AT_P3, f"放下零件A和B到{p3_slot}"
                    )
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK,
                        task=ATCTask.PUT_DOWN_COMPONENT_A,
                        area=target_pose,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                    )
                    if not self._check_result(result, f"放下零件A到{p3_slot}", robot_id):
                        return

                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK,
                        task=ATCTask.PUT_DOWN_COMPONENT_B,
                        area=target_pose,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                    )
                    if not self._check_result(result, f"放下零件B到{p3_slot}", robot_id):
                        return
                    #input("66666666666666666")

                    # 更新槽位状态
                    self._p3_tracker.set_state(p3_slot, BoxSlotState.FULL)
                    if component_two:
                        if self._p1_tracker.get_state("P1") == BoxSlotState.HALF:
                            self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)
                        else:
                            self._p1_tracker.set_state("P1", BoxSlotState.HALF)
                        logger.info("ATC", f"{p3_slot} 状态 → {BoxSlotState.FULL.value}")
                    else:
                        self._p1_tracker.set_state("P1", BoxSlotState.EMPTY)


                    # ── 分拣流程结束 ─────────────────────────────────────────


                    # ── 搬箱子流程开始 ───────────────────────────────────────────────────────────

                    # 7. 导航到P4点位
                    self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P4, "导航到P4点位")
                    if not self._navigate(robot, NavigationPose.P4, "P4点位", robot_id):
                        return
                    #input("77777777777777777")
                    # 如果P4槽位是空箱子，则开始搬箱子流程
                    # 等待 P4 有满箱子可搬运（MANUAL_RESET_COMPLETED 会将 P4 标记为 EMPTY，
                    # 当 robot_a 把满箱子搬到 P4 后，P4 变为 FULL；这里等待 P4 变为 EMPTY 的
                    # 含义是：等待 robot_b 完成装配并取走（或人工取走）P4 的满箱子）
                    while self._p4_tracker.get_state("P4") != BoxSlotState.EMPTY:
                        if not self._check_flow_control(robot_id):
                            return
                        time.sleep(2)

                    # 搬箱子流程
                    # ──T3：把空箱子从P4点位搬到P3无箱子点位,把满箱子从P3满箱子点位搬到P4点位───────────────────────────────────────────
                    # 获取槽位状态为NO_BOX的槽位
                    p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.NO_BOX)
                    if p3_slot is None:
                        msg = "P3 区域没有无箱子槽位: " + self._p3_tracker.get_status_display()
                        logger.error("ATC", msg)
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    target_pose = getattr(ATCPose, p3_slot)
                    nav_pose = p3_slot

                    # 8. 执行动作：把箱子从当前点位搬到P3无箱子点位
                    self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P3, "把空箱子从P4搬到P3空槽")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK_GEELY,
                        task=ATCTask.PICK_BOX_TO_SP,
                        area=target_pose,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                        extra_params={"current_pose": ATCPose.P4},
                    )
                    if not self._check_result(result, "把空箱子从P4搬到P3空槽", robot_id):
                        return
                    #input("88888888888888888")

                    # 更新P4槽位状态为NO_BOX，P3槽位状态为EMPTY
                    self._p4_tracker.set_state("P4", BoxSlotState.NO_BOX)
                    self._p3_tracker.set_state(p3_slot, BoxSlotState.EMPTY)

                    # 获取P3槽位状态为FULL的槽位
                    p3_slot = self._p3_tracker.find_slot_by_state(BoxSlotState.FULL)
                    if p3_slot is None:
                        msg = "P3 区域没有满箱子槽位: " + self._p3_tracker.get_status_display()
                        logger.error("ATC", msg)
                        self.task_state_machine.set_error(msg, robot_id)
                        return
                    target_pose = getattr(ATCPose, p3_slot)
                    nav_pose = p3_slot

                    # 9. 导航到P3满箱子点位
                    self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_P3, "导航到P3满箱子点位")
                    if not self._navigate(robot, nav_pose, "P3满箱子点位", robot_id):
                        return
                    #input("99999999999999999")

                    # 10. 执行动作：把满箱子从P3满箱子点位搬到P4点位
                    self.task_state_machine.update_step(robot_id, ATCStep.ACTION_PUT_BOX_AT_P4, "把满箱子从P3搬到P4")
                    result = robot.send_service_request_task(
                        ATCService.ROBOT_TASK_GEELY,
                        task=ATCTask.PICK_BOX_TO_SP,
                        area=ATCPose.P4,
                        maxtime=ATCTimeout.ROBOT_ACTION,
                        extra_params={"current_pose": target_pose},
                    )
                    if not self._check_result(result, "把满箱子从P3搬到P4", robot_id):
                        return
                    #input("101010101010101010")

                    # 更新P3槽位状态为NO_BOX，P4槽位状态为FULL
                    self._p3_tracker.set_state(p3_slot, BoxSlotState.NO_BOX)
                    self._p4_tracker.set_state("P4", BoxSlotState.FULL)

                    # ── 搬箱子流程结束 ───────────────────────────────────────────────────────────

                    # 11. 导航到home点位
                    self.task_state_machine.update_step(robot_id, ATCStep.NAVIGATING_TO_HOME, "导航到home点位")
                    if not self._navigate(robot, NavigationPose.home, "home点位", robot_id):
                        return
                    #input("111111111111111111")

                    logger.info("ATC", f"[{robot_id}] 一个分拣搬运周期完成，等待下一周期...")

            except Exception as e:
                logger.exception_occurred("ATC", f"分拣搬运流程异常 (task={task_id})", e)
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
            logger.info("ATC", f"[{robot_id}] 流程已结束，停止执行")
            return False
        if not self._pause_event.is_set():
            logger.info("ATC", f"[{robot_id}] 流程已暂停，等待恢复...")
            self._pause_event.wait()   # 阻塞直到 PROCESS_RESUMED 或 PROCESS_ENDED
            if self._stop_event.is_set():
                logger.info("ATC", f"[{robot_id}] 暂停期间收到结束信号，停止执行")
                return False
        return True

    def _run_conveyor_forward(self) -> bool:
        """
        PLC 操作层：发送传送带正转指令并等待完成。
        只负责与 PLC 通信，不涉及任何业务状态更新（如 P2 槽位）。
        返回 True 表示传送带正转正常完成，False 表示指令失败或超时。
        """
        logger.info("ATC", "传送带正转启动...")
        ok = self._conveyor.forward()
        if not ok:
            logger.error("ATC", "传送带正转指令发送失败")
            return False
        done = self._conveyor.wait_done(timeout=ATCTimeout.CONVEYOR)
        if not done:
            logger.error("ATC", f"传送带正转超时（{ATCTimeout.CONVEYOR}s）")
            return False
        logger.info("ATC", "传送带正转 PLC 操作完成")
        return True

    def _trigger_conveyor_reverse(self):
        """触发传送带反转（分拣流程结束后调用，在后台执行）"""
        def _reverse():
            logger.info("ATC", "分拣流程结束，触发传送带反转...")
            ok = self._conveyor.reverse()
            if not ok:
                logger.error("ATC", "传送带反转指令发送失败")
                return
            done = self._conveyor.wait_done(timeout=ATCTimeout.CONVEYOR)
            if done:
                logger.info("ATC", "传送带反转完成，已归零停止")
            else:
                logger.error("ATC", f"传送带反转超时（{ATCTimeout.CONVEYOR}s）")

        threading.Thread(target=_reverse, daemon=True, name="ATC-ConveyorReverse").start()

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
                logger.error("ATC", f"[{robot_id}] {msg}")
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
            distance_tolerance=ATCNavTolerance.DISTANCE,
            heading_tolerance=ATCNavTolerance.HEADING,
            translation_enable=True,
            translation_heading=ATCNavTolerance.TRANSLATION_HEADING,
        )

        def _on_feedback(fb):
            print(f"  [ATC 导航][{robot_id}] {label}: {fb.state.name}")

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
                logger.error("ATC", f"[{robot_id}] {msg}")
                self.task_state_machine.set_error(msg, robot_id)
                return False

            if attempt < max_attempts:
                logger.warning(
                    "ATC",
                    f"[{robot_id}] 导航到{label} state={result.state.name}，"
                    f"第 {attempt}/{max_attempts} 次失败，1s 后重新导航...",
                )
                if not self._check_flow_control(robot_id):
                    return False
                time.sleep(1.0)
                continue

            msg = f"导航到{label}失败({result.state.name})，已重试 {max_attempts} 次"
            logger.error("ATC", f"[{robot_id}] {msg}")
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
            logger.error("ATC", f"[{robot_id}] {msg}")
            print(f"[{robot_id}] {msg}")
            self.task_state_machine.set_error(msg, robot_id)
            return False
        # 动作成功 → 检查流程控制状态（暂停/停止）
        return self._check_flow_control(robot_id)
