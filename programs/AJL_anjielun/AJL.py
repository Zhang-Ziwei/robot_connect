"""
AJL 安捷伦色谱仪放样任务处理器

独立类，不继承、不修改旧项目任何文件。
仅导入以下稳定的底层接口：

    ┌─────────────────────────────────────────────────────┐
    │  导入层级（由稳定到易变）                             │
    │                                                     │
    │  标准库（threading / time）              — 最稳定    │
    │  infrastructure.*（constants/logger）    — 稳定      │
    │  hardware.*（robot_controller/nav）      — 较稳定    │
    │  core.task_state_machine                 — 较稳定    │
    │  programs.AJL_anjielun.*（本项目专属）   — 自己维护  │
    │                                                     │
    │  ✗ 不导入 cmd_handler / core/robot_actions           │
    └─────────────────────────────────────────────────────┘

改动说明（2026-06-12）：
  ① 登录一次即可：AJLHandler 激活后仅执行一次 step1_login，
     后续每次放样任务不重复登录（_logged_in 标志位）。
  ② Step4 根据 statusCode 智能决策：
     -23/-24 → 等待 30s 重试（最多 10 次）
     -25     → 等待 60s 重试（最多 20 次）
     -26/-27/-28 → 立即终止报错（需人工干预）
  ③ 并行执行：放下色谱盘后，导航返回 与 API步骤6→7→8→9
     在两个独立线程中同步执行，互不阻塞，最大化时间利用率。

改动说明（2026-06-12 流程控制）：
  ④ 新增流程控制接口，与 ATC 保持一致：
     PROCESS_BEGINS  → handle_process_begins  （启动放样流程）
     PROCESS_PAUSED  → handle_process_paused  （流程暂停）
     PROCESS_RESUMED → handle_process_resumed （流程恢复）
     PROCESS_ENDED   → handle_process_ended   （流程终止）
     流程在每个关键步骤后检查暂停/停止信号，保证最小颗粒度动作完成后再响应。
"""

import threading
import time
from typing import Dict, List, Optional

from infrastructure.constants import (
    ErrorCode,
    NavigationState,
    make_error_response,
    make_success_response,
)
from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    send_navigation_action,
    build_navigation_goal,
    is_robot_at_pose,
)
from hardware.robot_controller import RobotController
from core.task_state_machine import ParallelTaskStateMachine

from programs.AJL_anjielun.constants import (
    AJLNavigationPose,
    AJLNavTolerance,
    AJLService,
    AJLTask,
    AJLArea,
    AJLStep,
    AJLTimeout,
    AJLApiConfig,
    AJLStep4Policy,
)
from programs.AJL_anjielun.chromatograph_client import ChromatographClient

logger = get_error_logger()

# 默认机器人 ID（单机器人项目）
_DEFAULT_ROBOT_ID = "robot_a"


class AJLHandler:
    """
    AJL 安捷伦色谱仪放样任务处理器。

    cmd_handler 注册方式：
        "AJL_START_TASK" → ajl.handle_start_task(cmd_data)
        "AJL_STOP_TASK"  → ajl.handle_stop_task(cmd_data)

    参数:
        robots:     { robot_id: RobotController } 字典
        client:     ChromatographClient 实例（不传则自动创建）
    """

    def __init__(
        self,
        robots: dict,
        client: Optional[ChromatographClient] = None,
    ):
        self.robots = robots
        self.task_state_machine = ParallelTaskStateMachine(list(robots.keys()))

        # 色谱仪 HTTP 客户端：不传则自动创建（正常部署无需外部感知）
        self._client: ChromatographClient = (
            client if client is not None else ChromatographClient()
        )

        # 流程控制信号
        # _stop_event：set = 收到 PROCESS_ENDED，流程在下一检查点退出
        # _pause_event：set = 运行中；clear = 暂停（PROCESS_PAUSED），流程阻塞等待
        self._stop_event: threading.Event = threading.Event()
        self._pause_event: threading.Event = threading.Event()
        self._pause_event.set()  # 初始为运行态

        # PROCESS_BEGINS 后置为 True，PROCESS_ENDED 后置为 False
        self._process_active: bool = False

        # ── ① 登录一次标志 ────────────────────────────────────────────────────
        # AJL 系统激活后只需登录一次，后续每次放样无需重复登录。
        # 此标志在本进程生命周期内持久，不随单次任务重置。
        self._logged_in: bool = False
        self._login_lock: threading.Lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────────
    # 公开 HTTP 命令入口
    # ──────────────────────────────────────────────────────────────────────────

    def handle_start_task(self, cmd_data: Dict) -> Dict:
        """
        AJL_START_TASK：启动放样流程（异步，立即返回 HTTP 响应）。
        流程在后台线程执行，包含机器人导航、动作及 API 调用。

        cmd_data 字段（均可选，省略时使用默认值）：
            vial_barcode: str  — 样品条码（默认自动生成时间戳+序号）
            robot_id: str      — 机器人 ID（默认 "robot_a"）
        """
        return self.handle_process_begins(cmd_data)

    def handle_stop_task(self, cmd_data: Dict) -> Dict:
        """AJL_STOP_TASK：向后兼容别名，等同于 PROCESS_ENDED。"""
        return self.handle_process_ended(cmd_data)

    # ──────────────────────────────────────────────────────────────────────────
    # ④ 流程控制命令（与 ATC 接口保持一致）
    # ──────────────────────────────────────────────────────────────────────────

    def handle_process_begins(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_BEGINS：启动 AJL 放样全流程（异步，立即返回）。

        cmd_data 字段（均可选）：
            vial_barcode: str  — 样品条码（默认自动生成时间戳+序号）
            robot_id: str      — 机器人 ID（默认 "robot_a"）
        """
        cmd_id = cmd_data.get("cmd_id", "")
        robot_id = cmd_data.get("robot_id", _DEFAULT_ROBOT_ID)

        robot = self.robots.get(robot_id)
        if robot is None:
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {robot_id} 不存在",
                cmd_id=cmd_id,
            )

        if self.task_state_machine.is_busy():
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                "AJL 放样任务正在执行中，请等待完成后再发送",
                cmd_id=cmd_id,
            )

        # 重置流程控制信号，激活流程
        self._stop_event.clear()
        self._pause_event.set()
        self._process_active = True
        self.task_state_machine.start_task(cmd_id)

        vial_barcode = cmd_data.get("vial_barcode") or self._generate_vial_barcode()

        threading.Thread(
            target=self._run_task,
            args=(cmd_id, robot_id, robot, vial_barcode),
            daemon=True,
            name=f"AJL-Task-{cmd_id}",
        ).start()

        logger.info("AJL", f"流程已启动: cmd_id={cmd_id} robot={robot_id} vial={vial_barcode}")
        return make_success_response(
            f"已接收 PROCESS_BEGINS 命令，AJL 放样流程开始，vial={vial_barcode}",
            cmd_id=cmd_id,
        )

    def handle_process_paused(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_PAUSED：暂停放样流程。
        机器人会完成当前最小颗粒度动作后再暂停。
        """
        cmd_id = cmd_data.get("cmd_id", "")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED,
                "流程尚未启动，请先发送 PROCESS_BEGINS",
                cmd_id=cmd_id,
            )
        self._pause_event.clear()
        logger.info("AJL", f"收到暂停指令，流程将在当前动作完成后暂停 (cmd_id={cmd_id})")
        return make_success_response("已接收 PROCESS_PAUSED 命令，流程暂停中", cmd_id=cmd_id)

    def handle_process_resumed(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_RESUMED：恢复暂停的放样流程。
        """
        cmd_id = cmd_data.get("cmd_id", "")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED,
                "流程尚未启动，请先发送 PROCESS_BEGINS",
                cmd_id=cmd_id,
            )
        self._pause_event.set()
        logger.info("AJL", f"收到恢复指令，流程继续执行 (cmd_id={cmd_id})")
        return make_success_response("已接收 PROCESS_RESUMED 命令，流程已恢复", cmd_id=cmd_id)

    def handle_process_ended(self, cmd_data: Dict) -> Dict:
        """
        PROCESS_ENDED：终止放样流程，流程在当前步骤完成后退出。
        """
        cmd_id = cmd_data.get("cmd_id", "")
        if not self._process_active:
            return make_error_response(
                ErrorCode.TASK_NOT_STARTED,
                "流程尚未启动，请先发送 PROCESS_BEGINS",
                cmd_id=cmd_id,
            )
        self._stop_event.set()
        # 若流程正在暂停中，需要解除阻塞，让流程能检测到停止信号
        self._pause_event.set()
        self._process_active = False
        logger.info("AJL", f"收到终止指令，流程将在当前步骤完成后退出 (cmd_id={cmd_id})")
        return make_success_response("已接收 PROCESS_ENDED 命令，流程终止中", cmd_id=cmd_id)

    # ──────────────────────────────────────────────────────────────────────────
    # 主流程（后台线程）
    # ──────────────────────────────────────────────────────────────────────────

    def _run_task(
        self,
        task_id: str,
        robot_id: str,
        robot: RobotController,
        vial_barcode: str,
    ):
        """
        完整放样流程，在独立后台线程中执行。
        每个关键步骤后检查 _stop_event，收到停止信号时干净退出。
        """
        try:
            # ── 1. 三段式导航到抓取点位 ────────────────────────────────────────────
            self.task_state_machine.update_step(robot_id, AJLStep.NAVIGATING_TO_PICKUP, "导航到色谱盘抓取点位")
            if not self._navigate(robot, AJLNavigationPose.DISC_PICKUP_PRE_1, "色谱盘抓取点位前1", robot_id):
                return
            if not self._navigate(robot, AJLNavigationPose.DISC_PICKUP_PRE_2, "色谱盘抓取点位前2", robot_id):
                return
            if not self._navigate(robot, AJLNavigationPose.DISC_PICKUP, "色谱盘抓取点位后", robot_id):
                return

            self.task_state_machine.update_step(robot_id, AJLStep.NAVIGATING_TO_HOME, "导航回 home 点位")
            if not self._navigate(robot, AJLNavigationPose.GO_HOME_0, "home 点位前0", robot_id):
                return
            if not self._navigate(robot, AJLNavigationPose.GO_HOME_1, "home 点位前1", robot_id):
                return
            if not self._navigate(robot, AJLNavigationPose.GO_HOME_2, "home 点位前2", robot_id):
                return
            if not self._navigate(robot, AJLNavigationPose.HOME, "home 点位", robot_id):
                return
            input("11111111111111")

            # ── 2. 并行：抓起色谱盘 ‖ API步骤1→5 ──────────────────────────────
            # ④ 并行执行：抓盘动作与 API 登录/同步/查询/确认/申请 同步进行，
            #   互不阻塞，等双方均完成后再继续放盘。
            ok, place_vials = self._run_parallel_pickup_and_api(robot_id, robot, vial_barcode)
            if not ok:
                return

            if not self._check_flow_control(robot_id):
                self._on_stopped(robot_id)
                return

            # ── 3. 放下色谱盘（机器人动作）─────────────────────────────────
            self.task_state_machine.update_step(robot_id, AJLStep.PLACING_DISC, "放下色谱盘到仪器")
            result = robot.send_service_request_task(
                AJLService.ROBOT_TASK,
                task=AJLTask.PLACE_DISC,
                area=AJLArea.INSTRUMENT,
                maxtime=AJLTimeout.ROBOT_ACTION,
            )
            if not self._check_result(result, "放下色谱盘", robot_id):
                return

            # ── 4. 并行：导航返回 ‖ API步骤6→7→8→9 ──────────────────────────
            # ③ 并行执行：放下色谱盘后，机器人开始导航返回抓取点，
            #   同时 API 线程依次执行放样完成通知→启动分析→轮询RunId→等待完成。
            if not self._run_parallel_return_and_api(task_id, robot_id, robot, place_vials):
                return

            # ── 完成 ─────────────────────────────────────────────────────────
            self.task_state_machine.update_step(robot_id, AJLStep.COMPLETED, "放样任务完成")
            self.task_state_machine.mark_robot_done(robot_id)
            self._process_active = False
            vial_codes = [v.get("vialBarcode", "") for v in place_vials]
            logger.info(
                "AJL",
                f"[{robot_id}] 放样任务完成 sample={vial_barcode} vials={vial_codes}",
            )

        except Exception as e:
            logger.error("AJL", f"[{robot_id}] 任务异常: {e}")
            self.task_state_machine.set_error(f"任务异常: {e}", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            self._process_active = False

    # ──────────────────────────────────────────────────────────────────────────
    # ① 登录一次辅助方法
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_logged_in(self, robot_id: str) -> bool:
        """
        确保已登录 AAC 系统。
        若已登录则直接返回 True（跳过网络请求）；
        若尚未登录则执行 step1_login，成功后设置 _logged_in=True。
        线程安全（多任务并发场景）。
        """
        with self._login_lock:
            if self._logged_in:
                logger.info("AJL", f"[{robot_id}] 已登录 AAC，跳过 step1_login")
                return True

            self.task_state_machine.update_step(robot_id, AJLStep.API_LOGIN, "登录AAC系统")
            ok, msg = self._client.step1_login()
            if not ok:
                self._on_api_error("登录AAC", msg, robot_id)
                return False

            self._logged_in = True
            logger.info("AJL", f"[{robot_id}] AAC 登录成功")
            return True

    # ──────────────────────────────────────────────────────────────────────────
    # ② Step4 带 statusCode 策略的重试
    # ──────────────────────────────────────────────────────────────────────────

    def _step4_ready_to_place_with_retry(self, robot_id: str) -> bool:
        """
        执行 step4_ready_to_place，根据 API statusCode 决策：
          WAIT_AND_RETRY        → 等 {RETRY_WAIT_SECONDS}s 后重试，最多 {MAX_RETRIES} 次
          WAIT_LONGER_AND_RETRY → 等 {WAIT_LONGER_SECONDS}s 后重试，最多 {MAX_RETRIES_LONGER} 次
          TERMINAL_ERROR        → 立即终止，上报错误
          其他未知错误码        → 立即终止，上报错误

        注意：本方法只设置错误状态，不调用 mark_robot_done，
        由调用方（_run_task 或并行线程汇合点）负责最终终结任务。
        """
        policy = AJLStep4Policy

        attempt = 0
        while True:
            self.task_state_machine.update_step(
                robot_id,
                AJLStep.API_READY_TO_PLACE,
                f"确认仪器可放样{f'（第{attempt+1}次）' if attempt > 0 else ''}",
            )
            ok, msg, api_code = self._client.step4_ready_to_place()

            if ok:
                return True

            logger.warning("AJL", f"[{robot_id}] step4 失败: statusCode={api_code} msg={msg}")

            # ─── 立即终止 ───────────────────────────────────────────────────
            if api_code in policy.TERMINAL_ERROR:
                desc = {
                    -26: "错误锁定（需人工处理）",
                    -27: "仪器已被禁用（需管理员开启）",
                    -28: "模块硬件错误（需人工检修）",
                }.get(api_code, f"终止错误(code={api_code})")
                self._set_api_error(f"确认仪器可放样[{desc}]", msg, robot_id)
                return False

            # ─── 短等待重试 ─────────────────────────────────────────────────
            if api_code in policy.WAIT_AND_RETRY:
                attempt += 1
                if attempt > policy.MAX_RETRIES:
                    self._set_api_error(
                        f"确认仪器可放样：已等待重试 {policy.MAX_RETRIES} 次仍失败",
                        msg, robot_id,
                    )
                    return False
                desc = {-23: "仪器暂时不可用", -24: "有暂停任务"}.get(api_code, "")
                logger.info(
                    "AJL",
                    f"[{robot_id}] step4 [{desc}]，{policy.RETRY_WAIT_SECONDS}s 后重试"
                    f"（{attempt}/{policy.MAX_RETRIES}）",
                )
                time.sleep(policy.RETRY_WAIT_SECONDS)
                continue

            # ─── 长等待重试 ─────────────────────────────────────────────────
            if api_code in policy.WAIT_LONGER_AND_RETRY:
                attempt += 1
                if attempt > policy.MAX_RETRIES_LONGER:
                    self._set_api_error(
                        f"确认仪器可放样：等待运行中任务完成超时（已重试 {policy.MAX_RETRIES_LONGER} 次）",
                        msg, robot_id,
                    )
                    return False
                logger.info(
                    "AJL",
                    f"[{robot_id}] step4 [已有任务运行中]，{policy.WAIT_LONGER_SECONDS}s 后重试"
                    f"（{attempt}/{policy.MAX_RETRIES_LONGER}）",
                )
                time.sleep(policy.WAIT_LONGER_SECONDS)
                continue

            # ─── 未知错误码，直接终止 ────────────────────────────────────────
            self._set_api_error(f"确认仪器可放样(未知错误码={api_code})", msg, robot_id)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # ④ 并行：pick_disc ‖ API 步骤 1→5
    # ──────────────────────────────────────────────────────────────────────────

    def _run_parallel_pickup_and_api(
        self,
        robot_id: str,
        robot: RobotController,
        vial_barcode: str,
    ):
        """
        并行执行：
          Thread A — pick_disc（抓起色谱盘机器人动作）
          Thread B — API step1(登录) → step2(同步样品) → step3(查询仪器)
                       → step4(确认可放样) → step5(申请位置)

        两线程独立运行，主线程阻塞等待双方完成后统一检查结果。
        返回 (success: bool, place_vials: List[Dict] | None)
        """
        robot_err: List[Optional[str]] = [None]
        api_err:   List[Optional[str]] = [None]
        place_vials_holder: List        = [None]
        robot_done = threading.Event()
        api_done   = threading.Event()

        # ── Thread A：pick_disc ───────────────────────────────────────────────
        def _pick():
            try:
                self.task_state_machine.update_step(
                    robot_id, AJLStep.PICKING_DISC, "抓起色谱盘"
                )
                result = robot.send_service_request_task(
                    AJLService.ROBOT_TASK,
                    task=AJLTask.PICK_DISC,
                    area=AJLArea.DISC_PICKUP,
                    maxtime=AJLTimeout.ROBOT_ACTION,
                )
                if not result:
                    robot_err[0] = f"抓起色谱盘失败: {getattr(result, 'error_msg', '未知错误')}"
            except Exception as e:
                robot_err[0] = f"抓盘线程异常: {e}"
                logger.error("AJL", f"[{robot_id}] {robot_err[0]}")
            finally:
                robot_done.set()

        # ── Thread B：API step1→5 ─────────────────────────────────────────────
        def _api_pre_place():
            try:
                # Step 1: 登录（仅首次）
                if not self._ensure_logged_in(robot_id):
                    api_err[0] = "登录AAC失败"
                    return

                if not self._check_flow_control(robot_id):
                    api_err[0] = "流程控制中止"
                    return

                # Step 2: 同步样品数据
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_SYNC_SAMPLES, "同步样品数据到AAC"
                )
                ok, msg = self._client.step2_sync_samples(vial_barcode)
                if not ok:
                    api_err[0] = f"API [同步样品数据] 失败: {msg}"
                    return
                place_vials_holder[0] = self._client.build_place_vials_list(vial_barcode)

                if not self._check_flow_control(robot_id):
                    api_err[0] = "流程控制中止"
                    return

                # Step 3: 查询仪器
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_QUERY_INSTRUMENT, "查询仪器CDS ID"
                )
                ok, msg, cds_id, injector_id = self._client.step3_query_instrument()
                if not ok:
                    api_err[0] = f"API [查询仪器] 失败: {msg}"
                    return
                logger.info("AJL", f"[{robot_id}] 仪器: cdsId={cds_id} injectorId={injector_id}")

                if not self._check_flow_control(robot_id):
                    api_err[0] = "流程控制中止"
                    return

                # Step 4: 确认仪器可放样（带重试策略，不调用 mark_robot_done）
                if not self._step4_ready_to_place_with_retry(robot_id):
                    api_err[0] = "step4 确认仪器可放样失败"
                    return

                if not self._check_flow_control(robot_id):
                    api_err[0] = "流程控制中止"
                    return

                # Step 5: 申请进样器位置
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_APPLY_POSITION, "申请进样器位置"
                )
                ok, msg = self._client.step5_apply_position()
                if not ok:
                    api_err[0] = f"API [申请进样器位置] 失败: {msg}"
                    return

            except Exception as e:
                api_err[0] = f"API 预放样线程异常: {e}"
                logger.error("AJL", f"[{robot_id}] {api_err[0]}")
            finally:
                api_done.set()

        # ── 启动并行线程，等待双方完成 ────────────────────────────────────────
        threading.Thread(
            target=_pick, daemon=True, name=f"AJL-PickDisc-{robot_id}"
        ).start()
        threading.Thread(
            target=_api_pre_place, daemon=True, name=f"AJL-PreAPI-{robot_id}"
        ).start()

        robot_done.wait()
        api_done.wait()

        # ── 汇合点：统一处理错误 ─────────────────────────────────────────────
        if not self._check_flow_control(robot_id):
            self._on_stopped(robot_id)
            return False, None

        if robot_err[0]:
            logger.error("AJL", f"[{robot_id}] {robot_err[0]}")
            self.task_state_machine.set_error(robot_err[0], robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return False, None

        if api_err[0]:
            logger.error("AJL", f"[{robot_id}] {api_err[0]}")
            self.task_state_machine.set_error(api_err[0], robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return False, None

        return True, place_vials_holder[0]

    # ──────────────────────────────────────────────────────────────────────────
    # ③ 并行：导航返回 ‖ API 步骤 6→7→8→9
    # ──────────────────────────────────────────────────────────────────────────

    def _run_parallel_return_and_api(
        self,
        task_id: str,
        robot_id: str,
        robot: RobotController,
        place_vials: List[Dict],
    ) -> bool:
        """
        并行执行：
          Thread A — 导航回色谱盘抓取点位（step 14）
          Thread B — API step6 通知放样完成（全部小样品列表）
                       → step7 启动分析
                       → step8 轮询 RunId（若需要）
                       → step9 等待分析完成

        两个线程独立运行，主线程阻塞等待双方完成后统一检查错误。
        返回 True 表示两侧均成功，False 表示至少一侧失败（已设置状态机错误）。
        """
        # 用列表容器传递线程结果（避免 Python 闭包捕获问题）
        nav_err: List[Optional[str]] = [None]
        api_err: List[Optional[str]] = [None]
        nav_done = threading.Event()
        api_done = threading.Event()

        # ── Thread A：导航返回 ──────────────────────────────────────────────
        def _navigate_back():
            try:
                self.task_state_machine.update_step(robot_id, AJLStep.NAVIGATING_TO_HOME, "导航回 home 点位")
                if not self._navigate(robot, AJLNavigationPose.GO_HOME_0, "home 点位前0", robot_id):
                    nav_err[0] = "导航回GO_HOME_0点位失败"
                    return
                if not self._navigate(robot, AJLNavigationPose.GO_HOME_1, "home 点位前1", robot_id):
                    nav_err[0] = "导航回GO_HOME_1点位失败"
                    return
                if not self._navigate(robot, AJLNavigationPose.GO_HOME_2, "home 点位前2", robot_id):
                    nav_err[0] = "导航回GO_HOME_2点位失败"
                    return
                if not self._navigate(robot, AJLNavigationPose.HOME, "home 点位", robot_id):
                    nav_err[0] = "导航回home点位失败"
                    return
            except Exception as e:
                nav_err[0] = f"导航线程异常: {e}"
                logger.error("AJL", f"[{robot_id}] {nav_err[0]}")
            finally:
                nav_done.set()

        # ── Thread B：API 步骤 6→7→8→9 ─────────────────────────────────────
        def _api_post_place():
            try:
                # Step 6：通知放样完成
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_PLACE_COMPLETE, "通知AAC放样完成"
                )
                ok, msg = self._client.step6_place_complete(place_vials)
                if not ok:
                    api_err[0] = f"API [通知放样完成] 失败: {msg}"
                    return

                if not self._check_flow_control(robot_id):
                    return

                # Step 7：启动分析
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_START_ANALYSIS, "启动仪器分析"
                )
                ok, msg, run_id = self._client.step7_start_analysis()
                if not ok:
                    api_err[0] = f"API [启动仪器分析] 失败: {msg}"
                    return

                # Step 8：轮询 RunId（若 Step 7 未立即返回）
                if not run_id:
                    self.task_state_machine.update_step(
                        robot_id, AJLStep.API_POLL_RUN_ID, "轮询AnalysisRunId"
                    )
                    ok, msg, run_id = self._client.step8_poll_analysis_run_id()
                    if not ok:
                        api_err[0] = f"API [轮询AnalysisRunId] 失败: {msg}"
                        return

                logger.info("AJL", f"[{robot_id}] AnalysisRunId={run_id}")

                if not self._check_flow_control(robot_id):
                    return

                # Step 9：等待分析完成（阻塞轮询，机器人已在导航途中）
                self.task_state_machine.update_step(
                    robot_id, AJLStep.API_WAIT_ANALYSIS, "等待分析完成"
                )
                ok, msg, final_status = self._client.step9_query_run_status(
                    run_id, wait_finished=True
                )
                if not ok:
                    api_err[0] = f"API [等待分析完成] 失败: {msg}"
                    return

                logger.info("AJL", f"[{robot_id}] 分析完成，最终状态={final_status}")

            except Exception as e:
                api_err[0] = f"API 线程异常: {e}"
                logger.error("AJL", f"[{robot_id}] {api_err[0]}")
            finally:
                api_done.set()

        # ── 启动两线程，等待双方完成 ────────────────────────────────────────
        threading.Thread(
            target=_navigate_back, daemon=True, name=f"AJL-Nav-{task_id}"
        ).start()
        threading.Thread(
            target=_api_post_place, daemon=True, name=f"AJL-API-{task_id}"
        ).start()

        nav_done.wait()
        api_done.wait()

        # ── 汇合点：统一处理错误 ────────────────────────────────────────────
        if not self._check_flow_control(robot_id):
            self._on_stopped(robot_id)
            return False

        if nav_err[0]:
            self.task_state_machine.set_error(nav_err[0], robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return False

        if api_err[0]:
            self.task_state_machine.set_error(api_err[0], robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return False

        return True

    # ──────────────────────────────────────────────────────────────────────────
    # 私有辅助方法
    # ──────────────────────────────────────────────────────────────────────────

    def _check_flow_control(self, robot_id: str) -> bool:
        """
        检查流程控制信号，在每个关键步骤后调用。
        - 若收到停止信号（_stop_event），返回 False（调用方应退出流程）。
        - 若处于暂停状态（_pause_event 未 set），阻塞等待，直到恢复或收到停止信号。
        - 正常运行时直接返回 True。
        """
        if self._stop_event.is_set():
            return False
        if not self._pause_event.is_set():
            logger.info("AJL", f"[{robot_id}] 流程已暂停，等待恢复...")
            # 阻塞等待 _pause_event 置位（PROCESS_RESUMED 或 PROCESS_ENDED 都会触发）
            self._pause_event.wait()
            if self._stop_event.is_set():
                return False
            logger.info("AJL", f"[{robot_id}] 流程已恢复，继续执行")
        return True

    def _get_robot(self, robot_id: str) -> Optional[RobotController]:
        robot = self.robots.get(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在", robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
        return robot

    def _navigate(
        self,
        robot: RobotController,
        pose,
        label: str,
        robot_id: str,
        finalize: bool = True,
    ) -> bool:
        """
        导航到目标点，失败时记录错误并返回 False。
        NavigationState.FAILED / ABORTED 时最多重试 3 次。
        pose 支持：坐标列表、单坐标元组、字符串（AJLNavigationPose 属性名）。

        参数:
            finalize — True（默认）：失败时调用 mark_robot_done 终结任务；
                       False：仅设置错误，不终结（用于并行线程，由调用方统一处理）。
        """
        if isinstance(pose, str):
            resolved = getattr(AJLNavigationPose, pose, None)
            if resolved is None:
                msg = f"导航到{label}失败: AJLNavigationPose 无属性 '{pose}'"
                logger.error("AJL", f"[{robot_id}] {msg}")
                self.task_state_machine.set_error(msg, robot_id)
                if finalize:
                    self.task_state_machine.mark_robot_done(robot_id)
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
            distance_tolerance=AJLNavTolerance.DISTANCE,
            heading_tolerance=AJLNavTolerance.HEADING,
            translation_enable=True,
            translation_heading=AJLNavTolerance.TRANSLATION_HEADING,
        )

        def _on_feedback(fb):
            print(f"  [AJL 导航][{robot_id}] {label}: {fb.state.name}")

        _RETRYABLE = {NavigationState.FAILED, NavigationState.ABORTED}
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            result = send_navigation_action(
                robot, goal, feedback_callback=_on_feedback, retry_on_disconnect=True
            )
            if result.succeeded:
                return True

            if result.state not in _RETRYABLE:
                msg = f"导航到{label}失败: state={result.state.name}(value={result.state.value})"
                logger.error("AJL", f"[{robot_id}] {msg}")
                self.task_state_machine.set_error(msg, robot_id)
                if finalize:
                    self.task_state_machine.mark_robot_done(robot_id)
                return False

            if attempt < max_attempts:
                logger.warning(
                    "AJL",
                    f"[{robot_id}] 导航到{label} state={result.state.name}，"
                    f"第 {attempt}/{max_attempts} 次失败，1s 后重试...",
                )
                time.sleep(1.0)
                continue

            msg = f"导航到{label}失败({result.state.name})，已重试 {max_attempts} 次"
            logger.error("AJL", f"[{robot_id}] {msg}")
            self.task_state_machine.set_error(msg, robot_id)
            if finalize:
                self.task_state_machine.mark_robot_done(robot_id)
            return False

        return False

    def _check_result(self, result, label: str, robot_id: str) -> bool:
        """检查 send_service_request_task 返回值，失败时记录错误并返回 False。"""
        if not result:
            msg = f"{label}失败: {getattr(result, 'error_msg', '未知错误')}"
            logger.error("AJL", f"[{robot_id}] {msg}")
            self.task_state_machine.set_error(msg, robot_id)
            self.task_state_machine.mark_robot_done(robot_id)
            return False
        return True

    def _set_api_error(self, step_label: str, msg: str, robot_id: str):
        """记录 API 错误状态，不终结任务（供并行线程内调用）。"""
        full_msg = f"API [{step_label}] 失败: {msg}"
        logger.error("AJL", f"[{robot_id}] {full_msg}")
        self.task_state_machine.set_error(full_msg, robot_id)

    def _on_api_error(self, step_label: str, msg: str, robot_id: str):
        """API 调用失败时统一处理（设置错误并终结任务）。"""
        self._set_api_error(step_label, msg, robot_id)
        self.task_state_machine.mark_robot_done(robot_id)

    def _on_stopped(self, robot_id: str):
        """收到停止信号时的清理逻辑。"""
        logger.info("AJL", f"[{robot_id}] 流程已根据停止信号退出")
        self._process_active = False
        self.task_state_machine.cancel_task()

    @staticmethod
    def _generate_vial_barcode() -> str:
        """自动生成样品条码：时间戳 + 序号，如 '20240619135729-1-001'"""
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{ts}-1-001"
