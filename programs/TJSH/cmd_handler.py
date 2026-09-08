"""
命令处理器模块
处理各种CMD_TYPES的具体执行逻辑
"""
import json
import time
import threading
import uuid
from typing import Dict, List, Any, Optional
from hardware.robot_controller import RobotController
from core.bottle_manager import get_bottle_manager
from core.task_optimizer import get_task_optimizer
from infrastructure.error_logger import get_error_logger
from infrastructure.storage_manager import get_storage_manager, save_back_temp_storage
from infrastructure.constants import (
    HTTP_SERVER_PORT, BottleState,
    ROSTopic, ROSService, ROSTopicMessageType, ROSServiceMessageType,
    ENABLE_ROBOT_B_ACTIONS,
    get_main_ros_service, StationArea,
    ErrorCode, make_error_response, make_success_response,
)
from programs.TJSH.constants import NavigationPose
from core.task_state_machine import get_task_state_machine, TaskStep, TaskStatus
from core.robot_actions import handle_robot_action_command, is_robot_actions_enabled, get_available_actions
from core.station_counter import get_station_counter, StationCounter
from hardware.battery_monitor import get_battery_monitor, is_robot_available_for_task
from infrastructure.config_loader import reload_config, display_config_info
from network.websocket_client import get_websocket_client
from hardware.navigation_utils import (
    wait_for_topic_message,
    wait_for_navigation_finished,
    navigate_to_home_before_task,
    build_navigation_goal,
    send_navigation_action,
    navigate_to_waypoints,
    TaskType,
    get_robot_odom,
)
from hardware.task_utils import (
    send_task_action,
    TaskFeedback,
    TaskResult,
)
from programs.ATC.ATC import ATCHandler
from programs.WRC.WRC import WRCHandler
from programs.WRC_FLOW.WRC_FLOW import WRCFlowHandler
from programs.AJL_anjielun.AJL import AJLHandler
from programs.WAIC.WAIC import WAICHandler
from programs.KAIAO.KAIAO import KAIAOHandler
from programs.KAIAO_FLOW.KAIAO_FLOW import KAIAOFlowHandler
from programs.CONST_FLOW.CONST_FLOW import CONSTFlowHandler
from handlers.dispatcher_base import BaseCmdDispatcher
from infrastructure.pose_loader import get_active_project

logger = get_error_logger()


def _parse_cv_detect_return_params(return_params: str):
    """
    从 cv_detect 服务的新协议 return_params 字段中解析 object_pose 和 object_type。
    
    参数:
        return_params: 新协议 values.return_params 字符串，通常是 JSON 格式，例如：
            '{"target_pose_return": [x,y,z,...], "type_return": "glass_bottle_500"}'
            为兼容演进，也容许键名写作 object_pose / object_type。
    
    返回:
        (object_pose, object_type) 二元组；解析失败则均为 None。
    """
    if not return_params:
        return None, None
    try:
        if isinstance(return_params, dict):
            data = return_params
        else:
            data = json.loads(return_params)
    except (ValueError, TypeError) as e:
        logger.warning("命令处理器", f"cv_detect return_params 解析失败: {e}, 原始值={return_params!r}")
        return None, None
    if not isinstance(data, dict):
        return None, None
    # 两套键名都接收，方便固件演进
    object_pose = data.get("target_pose_return", data.get("object_pose"))
    object_type = data.get("type_return", data.get("object_type"))
    return object_pose, object_type


class CmdHandler(BaseCmdDispatcher):
    """
    TJSH 项目命令处理器。

    继承 BaseCmdDispatcher，获得：
      - 机器人管理工具（get_robot / is_robot_busy / check_battery_availability 等）
      - 通用 handlers（START_WORKING / RESET_SYSTEM / GET_TASK_STATE /
                        ROBOT_ACTION / 5 个硬件传感器状态查询）
      - handle_command（休眠门卫 + handler_map 分发）

    本类只负责：
      - TJSH 项目特有业务 handlers（PICK_UP / SCAN_QRCODE / SPLIT_LIQUID 等）
      - ATC / AJL 项目命令的路由注册
      - TJSH 特有事件（scan_enter_id / pick_from_opener）的初始化与重置
    """

    def __init__(self, robots: Dict[str, RobotController] = None):
        # ── 通用基础初始化（robots、状态机、事件、handler_map 基础表）────────
        super().__init__(robots)

        # ── TJSH 特有资源（无论项目是否激活都需要创建，供 handle_reset_system 使用）
        self.bottle_manager = get_bottle_manager()
        self.task_optimizer = get_task_optimizer()
        self.scan_enter_id_event = threading.Event()
        self.scan_enter_id_data = None
        self.pick_from_opener_500_event = threading.Event()
        self.pick_from_opener_500_data = None
        self.pick_from_opener_250_event = threading.Event()
        self.pick_from_opener_250_data = None

        # ── 项目处理器（按 active_project 配置按需创建）────────────────────
        self._atc  = None
        self._ajl  = None
        self._waic = None
        self._wrc_flow = None
        self._const_flow = None

        # ── 读取激活项目，决定注册哪些命令 ───────────────────────────────
        active_project = get_active_project()
        self.active_project = active_project
        print(f"\n📦 激活项目: {active_project}  "
              f"（修改 infrastructure/constants.py 中的 DEFAULT_ACTIVE_PROJECT 可切换，"
              f"或在 /config/robot_config.json 中添加 active_project 字段覆盖）\n")
        logger.info("命令处理器", f"激活项目: {active_project}")

        # ── 注册 TJSH 命令（瓶子处理、液体分装等）────────────────────────
        if active_project in ("TJSH", "ALL"):
            self._handler_map.update({
                "PICK_UP":                      self.handle_pickup,
                "PUT_TO":                       self.handle_put_to,
                "TAKE_BOTTOL_FROM_SP_TO_SP":    self.handle_transfer,
                "SCAN_QRCODE":                  self.handle_scan_qrcode,
                "SCAN_QRCODE_ENTER_ID":         self.handle_scan_qrcode_enter_id,
                "ENTER_ID":                     self.handle_enter_id,
                "BOTTLE_GET":                   self.handle_bottle_get,
                "SPLIT_LIQUID":                 self.handle_split_liquid,
                "PICK_FROM_BOTTLE_OPENER_500":  self.handle_pick_from_bottle_opener_500,
                "PICK_FROM_BOTTLE_OPENER_250":  self.handle_pick_from_bottle_opener_250,
                "PICK_FROM_BOTTLE_OPENER":      self.handle_pick_from_bottle_opener,
                "scanner":                      self.handle_ws_scanner_notify,
                "bottle_opener":                self.handle_ws_bottle_opener_notify,
                "REFILL_EMPTY_BOTTLES":         self.handle_refill_empty_bottles,
                "GET_STATION_COUNTER":          self.handle_get_station_counter,
                "TRANSFER_TO_CHROMATOGRAPH":    self.handle_transfer_to_chromatograph,
            })

        # ── 注册 ATC 命令（零件转移、传送带流程控制）────────────────────
        if active_project in ("ATC", "ALL"):
            self._atc = ATCHandler(robots=self.robots)
            self._handler_map.update({
                "TRANS_COMPONENT":              self._atc.handle_trans_component,
                "ATC_NEXT_STEP":                self._atc.handle_next_step,
                "PROCESS_BEGINS":               self._atc.handle_process_begins,
                "PROCESS_PAUSED":               self._atc.handle_process_paused,
                "PROCESS_RESUMED":              self._atc.handle_process_resumed,
                "PROCESS_ENDED":                self._atc.handle_process_ended,
                "MANUAL_RESET_COMPLETED":       self._atc.handle_manual_reset_completed,
            })

        # ── 注册 WRC 命令（演示装配流程）────────────────────────────────
        if active_project in ("WRC", "ALL"):
            self._wrc = WRCHandler(robots=self.robots)
            wrc_cmds = {
                "TRANS_COMPONENT":              self._wrc.handle_trans_component,
                "WRC_NEXT_STEP":                self._wrc.handle_next_step,
                "PROCESS_BEGINS":               self._wrc.handle_process_begins,
                "PROCESS_PAUSED":               self._wrc.handle_process_paused,
                "PROCESS_RESUMED":              self._wrc.handle_process_resumed,
                "PROCESS_ENDED":                self._wrc.handle_process_ended,
                "MANUAL_RESET_COMPLETED":       self._wrc.handle_manual_reset_completed,
            }
            # ALL 模式下 PROCESS_* 等已由 ATC 注册时不覆盖；单项目 WRC 全量注册
            if active_project == "WRC":
                self._handler_map.update(wrc_cmds)
            else:
                self._handler_map.setdefault("WRC_NEXT_STEP", wrc_cmds["WRC_NEXT_STEP"])
                for k, v in wrc_cmds.items():
                    if k not in self._handler_map:
                        self._handler_map[k] = v

        # ── 注册 WRC_FLOW 命令（图形化流程编排引擎 · WRC 试点）───────────
        # 只在单独激活 WRC_FLOW 时注册，不参与 "ALL" 模式：
        # 它和 WRC 共用同一批 cmd_type（PROCESS_BEGINS 等），两者同时注册会互相覆盖，
        # 而 WRC_FLOW 目前只是试点，默认不应该在 ALL 模式下悄悄抢走 WRC 的命令。
        if active_project == "WRC_FLOW":
            self._wrc_flow = WRCFlowHandler(robots=self.robots)
            self._handler_map.update({
                "PROCESS_BEGINS":               self._wrc_flow.handle_process_begins,
                "PROCESS_PAUSED":               self._wrc_flow.handle_process_paused,
                "PROCESS_RESUMED":              self._wrc_flow.handle_process_resumed,
                "PROCESS_ENDED":                self._wrc_flow.handle_process_ended,
                "MANUAL_RESET_COMPLETED":       self._wrc_flow.handle_manual_reset_completed,
                "WRC_FLOW_SIGNAL":              self._wrc_flow.handle_signal,
            })

        # ── 注册 AJL 命令（安捷伦色谱仪放样）───────────────────────────
        if active_project in ("AJL", "ALL"):
            self._ajl = AJLHandler(robots=self.robots)
            # 通用别名（始终注册，与 cmd_type 无关）
            self._handler_map.update({
                "AJL_START_TASK":               self._ajl.handle_start_task,
                "AJL_STOP_TASK":                self._ajl.handle_stop_task,
                "AJL_PROCESS_BEGINS":           self._ajl.handle_process_begins,
                "AJL_PROCESS_PAUSED":           self._ajl.handle_process_paused,
                "AJL_PROCESS_RESUMED":          self._ajl.handle_process_resumed,
                "AJL_PROCESS_ENDED":            self._ajl.handle_process_ended,
            })
            # 单 AJL 项目模式：直接使用无前缀的流程控制 cmd_type（与测试命令一致）
            # ALL 模式下这些 cmd_type 已由 ATC 注册，AJL 使用 AJL_* 前缀避免冲突
            if active_project == "AJL":
                self._handler_map.update({
                    "PROCESS_BEGINS":           self._ajl.handle_process_begins,
                    "PROCESS_PAUSED":           self._ajl.handle_process_paused,
                    "PROCESS_RESUMED":          self._ajl.handle_process_resumed,
                    "PROCESS_ENDED":            self._ajl.handle_process_ended,
                })

        # ── 注册 WAIC 命令（箱子搬运、中间件导航接口）──────────────────
        if active_project in ("WAIC", "ALL"):
            self._waic = WAICHandler(robots=self.robots, task_state_machine=self.task_state_machine)
            self._handler_map.update({
                "PICK_BOX_TO_SP": self._waic.handle_pick_box_to_sp,
                "PICK_UP_BOX":    self._waic.handle_pick_up_box,
                "PUT_DOWN_BOX":   self._waic.handle_put_down_box,
                "NAVIGATION":     self._waic.handle_navigation,
            })

        # ── 注册 KAIAO 命令（货架/零件搬运，参数与 WAIC 有差异）────────
        if active_project in ("KAIAO", "ALL"):
            self._kaiao = KAIAOHandler(robots=self.robots, task_state_machine=self.task_state_machine)
            self._handler_map.update({
                "PICK_BOX_TO_SP":       self._kaiao.handle_pick_box_to_sp,
                "PICK_COMPONENT_TO_SP": self._kaiao.handle_pick_component_to_sp,
                "PICK_UP_BOX":          self._kaiao.handle_pick_up_box,
                "PUT_DOWN_BOX":         self._kaiao.handle_put_down_box,
                "NAVIGATION":           self._kaiao.handle_navigation,
                "CANCEL_NAVIGATION":    self._kaiao.handle_cancel_navigation,
            })

        # ── 注册 KAIAO_FLOW 命令（图形化流程编排引擎 · KAIAO 试点）──────
        # 与 WRC_FLOW 同理：只在单独激活时注册，不参与 "ALL" 模式，
        # 否则会和上面的 KAIAO 抢同一批 cmd_type（PICK_BOX_TO_SP 等）。
        #
        # 这里刻意只认 KAIAO_FLOW 一个编排入口，不再直接引用原生 KAIAO 的处理器：
        # 哪些命令走流程图、哪些暂时转交原生实现，是 KAIAO_FLOW 自己的事
        # （见 KAIAO_FLOW.COMMAND_FLOWS / PENDING_FLOW_COMMANDS），
        # 命令分发这一层不必跟着变。底层能力仍复用同一个 KAIAOHandler 实例，
        # 货架重量/持箱状态不会分裂成两份。
        if active_project == "KAIAO_FLOW":
            self._kaiao_flow = KAIAOFlowHandler(
                robots=self.robots, task_state_machine=self.task_state_machine,
            )
            self._handler_map.update(self._kaiao_flow.build_command_map())

        # CONST_FLOW：PROCESS_BEGINS / PAUSED / ENDED 由 handler 注册；
        # 是否必须先 PROCESS_BEGINS 看 robot_config.flow_control.require_process_begins。
        if active_project == "CONST_FLOW":
            self._const_flow = CONSTFlowHandler(robots=self.robots)
            self._handler_map.update(self._const_flow.build_command_map())


    def handle_pickup(self, cmd_data: Dict) -> Dict:
        """
        处理PICK_UP命令
        拿取东西到平台
        """
        params = cmd_data.get("params", {})
        target_params = params.get("target_params", [])
        timeout = params.get("timeout", 10.0)
        
        # 提取bottle_id列表
        bottle_ids = [item["bottle_id"] for item in target_params]
        logger.info("命令处理器", f"PICK_UP - 瓶子数量: {len(bottle_ids)}")
        
        # 任务优化
        task_list, failed_bottles = self.task_optimizer.optimize_pickup_task(bottle_ids)
        
        if failed_bottles:
            logger.warning("命令处理器", f"以下瓶子无法拾取: {failed_bottles}")
        
        # 执行任务
        success_count = 0
        for nav_pose, bottles in task_list.items():
            # 等待导航状态
            self.robot_a.send_service_request(ROSTopic.NAVIGATION_STATUS, action="waiting_navigation_status")
            
            # 导航到目标点位
            result = self.robot_a.send_service_request(
                ROSTopic.NAVIGATION_STATUS, 
                action="navigation_to_pose",
                extra_params={"navigation_pose": nav_pose}
            )
            
            if not result:
                logger.error("命令处理器", f"导航失败: {nav_pose}")
                continue
            
            # 对每个瓶子执行拾取操作
            for bottle_id in bottles:
                bottle = self.bottle_manager.get_bottle(bottle_id)
                if not bottle:
                    continue
                
                # 1. 抓取物体
                grab_result = self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "grab_object",
                    extra_params={
                        "strawberry": {
                            "type": bottle.object_type,
                            "target_pose": bottle.target_pose,
                            "hand": bottle.hand
                        }
                    }
                )
                
                if not grab_result:
                    logger.error("命令处理器", f"抓取失败: {bottle_id}")
                    continue
                
                # 2. 转腰到背面
                self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "turn_waist",
                    extra_params={
                        "angle": "180",
                        "obstacle_avoidance": True
                    }
                )
                
                # 3. 放置到后部平台
                # 确定后部平台的放置点位
                back_pose = f"back_temp_{bottle.object_type.split('_')[-1]}_00{success_count + 1}"
                
                put_result = self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "put_object",
                    extra_params={
                        "strawberry": {
                            "type": bottle.object_type,
                            "target_pose": back_pose,
                            "hand": bottle.hand,
                            "safe_pose": "preset"
                        }
                    }
                )
                
                if put_result:
                    # 更新瓶子位置
                    self.bottle_manager.place_bottle(bottle_id, back_pose)
                    success_count += 1
                
                # 4. 转回正面
                self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "turn_waist",
                    extra_params={
                        "angle": "0",
                        "obstacle_avoidance": True
                    }
                )
        
        return {
            "success": True,
            "message": f"PICK_UP完成",
            "success_count": success_count,
            "failed_bottles": failed_bottles,
            "total": len(bottle_ids)
        }
    
    def handle_put_to(self, cmd_data: Dict) -> Dict:
        """
        处理PUT_TO命令
        放下东西到某个地方
        """
        params = cmd_data.get("params", {})
        release_params = params.get("release_params", [])
        
        logger.info("命令处理器", f"PUT_TO - 数量: {len(release_params)}")
        
        # 任务优化
        task_list, failed_bottles = self.task_optimizer.optimize_put_task(release_params)
        
        # 执行任务
        success_count = 0
        for nav_pose, items in task_list.items():
            # 等待导航状态
            self.robot_a.send_service_request(ROSTopic.NAVIGATION_STATUS, action="waiting_navigation_status")
            
            # 导航到目标点位
            result = self.robot_a.send_service_request(
                ROSTopic.NAVIGATION_STATUS,
                action="navigation_to_pose",
                extra_params={"navigation_pose": nav_pose}
            )
            
            if not result:
                logger.error("命令处理器", f"导航失败: {nav_pose}")
                continue
            
            # 对每个瓶子执行放置操作
            for bottle_id, release_pose in items:
                bottle = self.bottle_manager.get_bottle(bottle_id)
                if not bottle:
                    continue
                
                # 1. 转腰到背面
                self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "turn_waist",
                    extra_params={
                        "angle": "180",
                        "obstacle_avoidance": True
                    }
                )
                
                # 2. 从后部平台抓取
                grab_result = self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "grab_object",
                    extra_params={
                        "strawberry": {
                            "type": bottle.object_type,
                            "target_pose": bottle.location or f"back_temp_{bottle.object_type.split('_')[-1]}_001",
                            "hand": bottle.hand
                        }
                    }
                )
                
                if not grab_result:
                    logger.error("命令处理器", f"抓取失败: {bottle_id}")
                    continue
                
                # 3. 转回正面
                self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "turn_waist",
                    extra_params={
                        "angle": "0",
                        "obstacle_avoidance": True
                    }
                )
                
                # 4. 放置到目标点位
                put_result = self.robot_a.send_service_request(
                    self.robot_a.get_robot_service(),
                    "put_object",
                    extra_params={
                        "strawberry": {
                            "type": bottle.object_type,
                            "target_pose": release_pose,
                            "hand": bottle.hand,
                            "safe_pose": "preset"
                        }
                    }
                )
                
                if put_result:
                    # 更新瓶子位置
                    self.bottle_manager.remove_bottle_from_pose(bottle_id, bottle.location)
                    self.bottle_manager.place_bottle(bottle_id, release_pose)
                    success_count += 1
        
        return {
            "success": True,
            "message": "PUT_TO完成",
            "success_count": success_count,
            "failed_bottles": failed_bottles,
            "total": len(release_params)
        }
    
    def handle_transfer(self, cmd_data: Dict) -> Dict:
        """
        处理TAKE_BOTTLE_FROM_SP_TO_SP命令
        把样品瓶从某处拿到某处
        """
        params = cmd_data.get("params", {})
        target_params = params.get("target_params", [])
        release_params = params.get("release_params", [])
        
        logger.info("命令处理器", 
                   f"TRANSFER - 拾取: {len(target_params)}, 放置: {len(release_params)}")
        
        # 任务优化
        task_list2, failed_bottles = self.task_optimizer.optimize_transfer_task(
            target_params, release_params
        )
        
        # 执行任务
        total_success = 0
        for batch in task_list2:
            pick_tasks = batch["pick"]
            put_tasks = batch["put"]
            
            # 执行拾取任务
            for nav_pose, bottles in pick_tasks.items():
                self.robot_a.send_service_request(ROSTopic.NAVIGATION_STATUS, action="waiting_navigation_status")
                self.robot_a.send_service_request(
                    ROSTopic.NAVIGATION_STATUS,
                    action="navigation_to_pose",
                    extra_params={"navigation_pose": nav_pose}
                )
                
                for bottle_id in bottles:
                    bottle = self.bottle_manager.get_bottle(bottle_id)
                    if not bottle:
                        continue
                    
                    # 抓取、转腰、放置到后部平台的流程
                    self._execute_pickup_sequence(bottle)
            
            # 执行放置任务
            for nav_pose, items in put_tasks.items():
                self.robot_a.send_service_request(ROSTopic.NAVIGATION_STATUS, action="waiting_navigation_status")
                self.robot_a.send_service_request(
                    ROSTopic.NAVIGATION_STATUS,
                    action="navigation_to_pose",
                    extra_params={"navigation_pose": nav_pose}
                )
                
                for bottle_id, release_pose in items:
                    bottle = self.bottle_manager.get_bottle(bottle_id)
                    if not bottle:
                        continue
                    
                    # 从后部平台抓取、转腰、放置到目标点位的流程
                    if self._execute_putdown_sequence(bottle, release_pose):
                        total_success += 1
        
        return {
            "success": True,
            "message": "TRANSFER完成",
            "success_count": total_success,
            "failed_bottles": failed_bottles,
            "total": len(target_params)
        }

    def handle_scan_qrcode(self, cmd_data: Dict) -> Dict:
        """
        处理SCAN_QRCODE命令（异步模式）
        立即返回task_id，后台线程执行任务
        
        params中可指定robot_id来选择执行任务的机器人
        """
        # 生成唯一任务ID
        task_id = f"SCAN_QRCODE_{uuid.uuid4().hex[:8]}"
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params", {})
        
        # 从params中获取robot_id，默认使用robot_a
        robot_id = params.get("robot_id", "robot_a")
        # 验证机器人是否存在
        robot = self.get_robot(robot_id)
        if robot is None:
            available_robots = list(self.robots.keys()) if self.robots else []
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"指定的机器人 {robot_id} 不存在",
                available_robots=available_robots
            )
        
        # 检查机器人电量状态
        battery_check = self.check_battery_availability(robot_id, "SCAN_QRCODE")
        if battery_check["error_response"]:
            return battery_check["error_response"]
        
        # 如果需要先返回home点位
        if battery_check["need_go_home"]:
            if not self._navigate_to_home_before_task(robot_id, battery_check["home_pose"]):
                return make_error_response(
                    ErrorCode.NAVIGATION_FAILED,
                    f"机器人 {robot_id} 返回home点位失败，无法执行任务"
                )
        
        # 检查机器人是否正忙
        if self.is_robot_busy(robot_id):
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                f"机器人 {robot_id} 正忙，无法执行新任务",
                current_task_id=state.get("cmd_id"),
                current_status=state.get("status"),
                current_step=state.get("current_step", {}).get("description")
            )
        
        logger.info("命令处理器", f"开始SCAN_QRCODE任务: {cmd_id}, 使用机器人: {robot_id}")
        
        # 初始化状态机（传入robot_id）
        self.task_state_machine.start_task(cmd_id, robot_id)
        # 在后台线程执行扫码任务
        scan_thread = threading.Thread(
            target=self._execute_scan_qrcode_async_action,
            args=(cmd_id, robot_id),
            daemon=True,
            name=f"ScanThread-{cmd_id}-{robot_id}"
        )
        scan_thread.start()
        
        # 立即返回任务ID
        return make_success_response(
            "SCAN_QRCODE任务已启动",
            cmd_id=cmd_id,
            robot_id=robot_id,
            note="使用 GET_TASK_STATE 命令查询任务状态"
        )

    def _execute_scan_qrcode_async_action(self, task_id: str, robot_id: str = "robot_a"):
        """
        异步执行SCAN_QRCODE任务（后台线程）
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        # 获取对应的机器人实例
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            logger.error("命令处理器", f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")
            # 订阅导航状态topic
            logger.info("命令处理器", "订阅 /navigation_status topic")
            subscribe_success = robot.subscribe_topic(
                topic_name=ROSTopic.NAVIGATION_STATUS,
                msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
                throttle_rate=0,
                queue_length=1
            )
            if not subscribe_success:
                self.task_state_machine.set_error("订阅导航状态失败")
                logger.error("命令处理器", "订阅导航状态失败")

            # 获取存储管理器
            storage_mgr = get_storage_manager()
            back_temp_storage = storage_mgr.get_storage()
            # 测试代码
            '''scan_store_result = robot.send_service_request_task(
                "/robot_task",
                task="pick_up_component_A",
                area="point_1"
            )
            input("11111111111111111")'''
            # 步骤1: 导航到扫描台
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_SCAN, "开始导航到扫描台")

            # 通过发布topic消息触发导航（类似rospy.Publisher）
            '''navigation_publish_result = robot.publish_topic(
                topic_name="/navigation_control",
                msg_type="std_msgs/String",
                msg_data={"data": NavigationPose.SCAN_TABLE}  # 导航目标位置
            )
            if not navigation_publish_result:
                logger.error("命令处理器", "发布导航命令失败")
                print("发布导航命令失败")
                self.task_state_machine.set_error("发布导航命令失败")
                return
            # 导航到最后一段会断网
            # 导航后检查导航状态（支持连接断开重连）
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                logger.error("命令处理器", "导航到扫描台失败")
                print("导航到扫描台失败")
                return'''
            
            # 通过发布导航action触发导航：扫码台
            # 带反馈回调
            goal = build_navigation_goal(
                NavigationPose.SCAN_TABLE,  # 路点 list：中间点 + 终点
                distance_tolerance=0.06,
                heading_tolerance=0.08,
                translation_enable=True,
                translation_heading=0.0,
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, timeout=1200.0, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到扫码台失败")
                print("导航到扫码台失败")
                logger.error("命令处理器", "导航到扫码台失败")
                return
           
            # 单个导航点位操作动作：扫码并放置到后部暂存区
            # 走通用 Action 协议（task_utils，底层是 action_utils.send_action），
            # 与导航 action 同构：
            #   - 订阅 feedback/result topic
            #   - 发布 goal 后阻塞等终态
            #   - 支持 feedback_callback 与断线重连
            # 如需退回旧的 service 调用，把 send_task_action(...) 换成
            # robot.send_service_request(ROSService.CHEM_PROJECT_SERVICE, task="SCAN_TABLE") 即可。
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES, "扫码并放置到后部暂存区")

            '''def _on_task_fb(fb: TaskFeedback):
                print(f"[SCAN_TABLE] status={fb.status} params={fb.current_params}")

            scan_store_result: TaskResult = send_task_action(
                robot,
                task="SCAN_TABLE",
                feedback_callback=_on_task_fb,
            )
            if not scan_store_result:
                self.task_state_machine.set_error(
                    f"扫码并放置到后部暂存区失败: {scan_store_result.error_msg}"
                )
                logger.error(
                    "命令处理器",
                    f"扫码并放置到后部暂存区失败: {scan_store_result.error_msg}",
                )
                return
            # 业务 return_params（如需结构化用 scan_store_result.parse_return_params()）
            if scan_store_result.return_params:
                print(f"[SCAN_TABLE] return_params={scan_store_result.return_params}")'''
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "SCAN_TABLE"
            )
            if not scan_store_result:
                self.task_state_machine.set_error(
                    f"扫码并放置到后部暂存区失败: {scan_store_result.error_msg}"
                )
                logger.error("命令处理器", f"扫码并放置到后部暂存区失败: {scan_store_result.error_msg}")
                return


            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_WAITING_SPLIT_AREA_TRANSFER, "导航到分液台待分液区（转运任务点位）")
            # 通过发布导航action触发导航：分液台待分液区任务准备点位（转运任务点位）
            # 带反馈回调
            '''goal = build_navigation_goal([
                NavigationPose.TASK_PREPARE_WAITING_SPLIT_AREA_TRANSFER  # 目标点
            ])
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到分液台待分液区任务准备点位失败")
                print("导航到分液台待分液区任务准备点位失败")
                logger.error("命令处理器", "导航到分液台待分液区任务准备点位失败")
                return

            # 连续导航之间的停顿
            time.sleep(1.5)'''

            # 通过发布导航action触发导航：分液台待分液区（转运任务点位）
            goal = build_navigation_goal(
                NavigationPose.WAITING_SPLIT_AREA_TRANSFER,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, timeout=1200.0, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到分液台待分液区（转运任务点位）失败")
                print("导航到分液台待分液区（转运任务点位）失败")
                logger.error("命令处理器", "导航到分液台待分液区（转运任务点位）失败")
                return


            # 步骤11: 放下暂存区所有瓶子到分液台
            self.task_state_machine.update_step(TaskStep.PUTTING_DOWN, "放下暂存区所有瓶子到分液台")
            # 单个导航点位操作动作：放下暂存区所有瓶子到分液台
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "WAITING_SPLIT_AREA_TRANSFER"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("放下暂存区所有瓶子到分液台失败")
                logger.error("命令处理器", "放下暂存区所有瓶子到分液台失败")
                return
            
            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "扫码并放置到后部暂存区任务完成")
            self.task_state_machine.complete_task(True, "扫码并放置到后部暂存区流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")

        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")


    def _execute_scan_qrcode_async(self, task_id: str, robot_id: str = "robot_a"):
        """
        异步执行SCAN_QRCODE任务（后台线程）
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        # 获取对应的机器人实例
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            logger.error("命令处理器", f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")
            
            # 订阅导航状态topic
            logger.info("命令处理器", "订阅 /navigation_status topic")
            subscribe_success = robot.subscribe_topic(
                topic_name=ROSTopic.NAVIGATION_STATUS,
                msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
                throttle_rate=0,
                queue_length=1
            )
            if not subscribe_success:
                self.task_state_machine.set_error("订阅导航状态失败")
                logger.error("命令处理器", "订阅导航状态失败")

            # 获取存储管理器
            storage_mgr = get_storage_manager()
            back_temp_storage = storage_mgr.get_storage()

            # 步骤1: 导航到扫描台
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_SCAN, "开始导航到扫描台")
            # 导航准备位置（开始时候固定在home点触发）
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "home"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return
            #input(" ###################")
            # 通过发布topic消息触发导航（类似rospy.Publisher）
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.SCAN_TABLE}  # 导航目标位置
            )
            if not navigation_publish_result:
                logger.error("命令处理器", "发布导航命令失败")
                print("发布导航命令失败")
                self.task_state_machine.set_error("发布导航命令失败")
                return
            # 导航到最后一段会断网
            # 导航后检查导航状态（支持连接断开重连）
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                logger.error("命令处理器", "导航到扫描台失败")
                print("导航到扫描台失败")
                return
            # 步骤2: 抓取扫描枪（如果需要）
            # self.task_state_machine.update_step(TaskStep.GRAB_SCAN_GUN, "抓取扫描枪")
            # ... 抓取扫描枪的代码 ...
            '''grab_object_result = self.robot_a.send_service_request(
                ROSService.STRAWBERRY_SERVICE,
                task="grab_object",
                extra_params={
                    "type": "scan_gun",
                    "target_pose": "scan_gun",
                    "hand": "right"
                }
            )'''
            
            # 循环处理瓶子：扫码并放置到后部暂存区
            scan_store_result = self._scan_and_store_bottles_loop_press_button(robot, storage_mgr, None, robot_id)
            '''if not scan_store_result:
                return  # 出错时已在内部设置错误状态'''
            
            # 步骤10: 导航到分液台待分液区（转运任务点位）
            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "scan_area"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return

            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_WAITING_SPLIT_AREA_TRANSFER, "导航到分液台待分液区（转运任务点位）")
            # 导航到分液台待分液区任务准备点位（转运任务点位）
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.TASK_PREPARE_WAITING_SPLIT_AREA_TRANSFER}
            )
            if not navigation_publish_result:
                logger.error("命令处理器", "发布导航命令失败")
                print("发布导航命令失败")
                self.task_state_machine.set_error("发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                return
            # 连续导航之间的停顿
            time.sleep(1.5)

            # 导航到分液台待分液区（转运任务点位）
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.WAITING_SPLIT_AREA_TRANSFER}
            )
            if not navigation_publish_result:
                logger.error("命令处理器", "发布导航命令失败")
                print("发布导航命令失败")
                self.task_state_machine.set_error("发布导航命令失败")
                return
            # 导航到最后一段会断网
            # 导航后检查导航状态（支持连接断开重连）
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                return

            '''storage_mgr.update_slot(robot_id, "glass_bottle_500", 0, 66666)
            storage_mgr.set_bottle_state(robot_id, "glass_bottle_500", 0, BottleState.NOT_SPLIT)
            storage_mgr.update_slot(robot_id, "glass_bottle_500", 1, 77777)
            storage_mgr.set_bottle_state(robot_id, "glass_bottle_500", 1, BottleState.SPLIT_DONE)'''
            # 步骤11: 放下暂存区所有瓶子到分液台
            self.task_state_machine.update_step(TaskStep.PUTTING_DOWN, "放下暂存区所有瓶子到分液台")

            # 获取暂存区所有 glass_bottle_500 且未分液的瓶子
            # 注意：使用 storage_mgr.get_storage(robot_id) 获取最新数据，而不是之前的快照
            all_bottles_in_storage = []
            bottle_type = "glass_bottle_500"
            current_storage = storage_mgr.get_storage(robot_id)
            for slot_index, slot in enumerate(current_storage.get(bottle_type, [])):
                bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                print(f"slot {slot_index}: {bottle_info}")
                # 检查瓶子存在且状态为未分液
                if bottle_info and bottle_info.get("bottle_state") == BottleState.NOT_SPLIT:
                    all_bottles_in_storage.append(bottle_info)
            print(f"all_bottles_in_storage: {all_bottles_in_storage}")
            logger.info("命令处理器", f"暂存区共有 {len(all_bottles_in_storage)} 个瓶子需要放置")
            print(f"\n✓ 暂存区共有 {len(all_bottles_in_storage)} 个瓶子")
            
            # 特殊初始化动作
            put_down_split_table_init_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_down_init",
                extra_params={
                    "area": "waiting_split_area"
                }
            )
            if not put_down_split_table_init_result:
                self.task_state_machine.set_error("特殊初始化动作失败")
                logger.error("命令处理器", "特殊初始化动作失败")
                return
            
            # 调用双手放瓶子循环函数
            if not self._put_bottles_two_hands_loop(robot, storage_mgr, "waiting_split_area", all_bottles_in_storage, robot_id):
                return
            
            self.task_state_machine.update_step(TaskStep.COMPLETED, "任务完成")
            self.task_state_machine.complete_task(True, "流程结束")
                
            # 特殊导航后准备姿势
            navigation_prepare_split_table_result = robot.send_service_request(
                robot.get_robot_service(),
                "transfer_waiting_split_area",
                extra_params={
                    "area": "waiting_split_area"
                }
            )
            if not navigation_prepare_split_table_result:
                self.task_state_machine.set_error("特殊导航后准备姿势失败")
                logger.error("命令处理器", "特殊导航后准备姿势失败")
                return
            
            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "扫码并放置到后部暂存区任务完成")
            self.task_state_machine.complete_task(True, "扫码并放置到后部暂存区流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")

        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")
        # 注意：不在 finally 中取消订阅，因为：
        # 1. 重连后需要继续使用该订阅
        # 2. 订阅由 robot_controller 的 _resubscribe_topics 自动管理
        # 3. 如果需要手动取消，应该在任务真正完成后调用

    

    
    def handle_split_liquid(self, cmd_data: Dict) -> Dict:
        """
        处理SPLIT_LIQUID命令（异步模式）
        立即返回task_id，后台线程执行任务
        
        params中可指定robot_id来选择执行任务的机器人
        """
        # 生成唯一任务ID
        task_id = f"SPLIT_LIQUID_{uuid.uuid4().hex[:8]}"
        cmd_id = cmd_data.get("cmd_id")
        params = cmd_data.get("params", {})
        
        # 从params中获取robot_id，默认使用robot_a
        robot_id = params.get("robot_id", "robot_a")
        
        # 验证机器人是否存在
        robot = self.get_robot(robot_id)
        if robot is None:
            available_robots = list(self.robots.keys()) if self.robots else []
            return make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"指定的机器人 {robot_id} 不存在",
                available_robots=available_robots
            )
        
        # 检查机器人电量状态
        battery_check = self.check_battery_availability(robot_id, "SPLIT_LIQUID")
        if battery_check["error_response"]:
            return battery_check["error_response"]
        
        # 如果需要先返回home点位
        if battery_check["need_go_home"]:
            if not self._navigate_to_home_before_task(robot_id, battery_check["home_pose"]):
                return make_error_response(
                    ErrorCode.NAVIGATION_FAILED,
                    f"机器人 {robot_id} 返回home点位失败，无法执行任务"
                )
        
        # 检查机器人是否正忙
        if self.is_robot_busy(robot_id):
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                f"机器人 {robot_id} 正忙，无法执行新任务",
                current_task_id=state.get("cmd_id"),
                current_status=state.get("status"),
                current_step=state.get("current_step", {}).get("description")
            )
        
        logger.info("命令处理器", f"开始SPLIT_LIQUID任务: {cmd_id}, 使用机器人: {robot_id}")
        
        # 初始化状态机（传入robot_id）
        self.task_state_machine.start_task(cmd_id, robot_id)
        
        # 在后台线程执行分液任务
        split_thread = threading.Thread(
            target=self._execute_split_liquid_async_action,
            args=(cmd_id, robot_id),
            daemon=True,
            name=f"SplitThread-{cmd_id}-{robot_id}"
        )
        split_thread.start()
        
        # 立即返回任务ID
        return make_success_response(
            "SPLIT_LIQUID任务已启动",
            cmd_id=cmd_id,
            robot_id=robot_id,
            note="使用 GET_TASK_STATE 命令查询任务状态"
        )
    

    def _execute_split_liquid_async_action(self, task_id: str, robot_id: str = "robot_a"):
        """
        异步执行SPLIT_LIQUID任务（后台线程）
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            logger.error("命令处理器", f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")

            # 订阅导航状态topic
            robot.subscribe_topic(
                topic_name=ROSTopic.NAVIGATION_STATUS,
                msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
            )

            '''self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_WAITING_SPLIT_AREA_SPLIT, "导航到待分液区（分液任务点位）(前进进入)")  
            # 导航到分液任务准备点位
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.TASK_PREPARE_SPLIT}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到分液任务准备点位失败")
                logger.error("命令处理器", "导航到分液任务准备点位失败")
                return
            # 连续导航点之间的停顿，放置导航状态刷新不及时/zj_humanoid/navigation/odom_info
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.WAITING_SPLIT_AREA_SPLIT_FORWARD}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到待分液区（分液任务点位）(前进进入)失败")
                logger.error("命令处理器", "导航到待分液区（分液任务点位）失败")
                return'''
            # 导航到待分液区（分液任务点位）
            goal = build_navigation_goal(
                NavigationPose.WAITING_SPLIT_AREA_SPLIT,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到待分液区（分液任务点位）失败")
                print("导航到待分液区（分液任务点位）失败")
                logger.error("命令处理器", "导航到待分液区（分液任务点位）失败")
                return


            # 单个导航点位操作动作：在待分液区抓取瓶子放到后部暂存区
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES, "在待分液区抓取瓶子放到后部暂存区")
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "WAITING_SPLIT_AREA_SPLIT"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("在待分液区抓取瓶子放到后部暂存区失败")
                logger.error("命令处理器", "在待分液区抓取瓶子放到后部暂存区失败")
                return
            
            # 导航到空瓶区
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_SPLIT_MACHINE_SPLIT, "导航到分液台（分液任务点位）(前进进入)")
            goal = build_navigation_goal(
                NavigationPose.EMPTY_BOTTLE_AREA_SPLIT,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到空瓶区失败")
                print("导航到空瓶区失败")
                logger.error("命令处理器", "导航到空瓶区失败")
                return


            # 单个导航点位操作动作：在空瓶区抓取瓶子放到后部暂存区
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES_WAITING_SPLIT_AREA, "在空瓶区抓取瓶子放到后部暂存区")
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "EMPTY_BOTTLE_AREA_SPLIT"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("在空瓶区抓取瓶子放到后部暂存区失败")
                logger.error("命令处理器", "在空瓶区抓取瓶子放到后部暂存区失败")
                return
            
            # 导航到分液台（分液任务点位）
            goal = build_navigation_goal(
                NavigationPose.SPLIT_LIQUID_AREA_SPLIT,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到分液台（分液任务点位）失败")
                print("导航到分液台（分液任务点位）失败")
                logger.error("命令处理器", "导航到分液台（分液任务点位）失败")
                return

            
            # 单个导航点位操作动作：分液动作
            self.task_state_machine.update_step(TaskStep.ACTION_POURING_WATER, "分液动作")
            pour_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "SPLIT_LIQUID_AREA_SPLIT"
            )
            if not pour_result:
                self.task_state_machine.set_error("分液动作失败")
                logger.error("命令处理器", "分液动作失败")
                return

            # 导航到500ml分液完成暂存区（分液任务点位）
            goal = build_navigation_goal(
                NavigationPose.SPLIT_DONE_500ML_AREA_SPLIT,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到500ml分液完成暂存区（分液任务点位）失败")
                print("导航到500ml分液完成暂存区（分液任务点位）失败")
                logger.error("命令处理器", "导航到500ml分液完成暂存区（分液任务点位）失败")
                return

            
            # 单个导航点位操作动作：从后部暂存区抓取瓶子放到500ml分液完成暂存区
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES_WAITING_SPLIT_AREA, "从后部暂存区抓取瓶子放到500ml分液完成暂存区")
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "SPLIT_DONE_500ML_AREA_SPLIT"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("从后部暂存区抓取瓶子放到500ml分液完成暂存区失败")
                logger.error("命令处理器", "从后部暂存区抓取瓶子放到500ml分液完成暂存区失败")
            
            # 导航到250ml分液完成暂存区（分液任务点位）
            goal = build_navigation_goal(
                NavigationPose.SPLIT_DONE_250ML_AREA_SPLIT,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到250ml分液完成暂存区（分液任务点位）失败")
                print("导航到250ml分液完成暂存区（分液任务点位）失败")
                logger.error("命令处理器", "导航到250ml分液完成暂存区（分液任务点位）失败")
                return

            # 网络/WebSocket 断线恢复后的导航结果等待、code=10009 重试
            # 均由 navigation_utils.send_navigation_action 统一处理。
            # 单个导航点位操作动作：从后部暂存区抓取瓶子放到250ml分液完成暂存区
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES_WAITING_SPLIT_AREA, "从后部暂存区抓取瓶子放到250ml分液完成暂存区")
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "SPLIT_DONE_250ML_AREA_SPLIT"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("从后部暂存区抓取瓶子放到250ml分液完成暂存区失败")
                logger.error("命令处理器", "从后部暂存区抓取瓶子放到250ml分液完成暂存区失败")
                return
            
            # 回到home点躲避另一台（HOME_ROBOT_B）
            goal = build_navigation_goal(
                NavigationPose.HOME_ROBOT_B,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到home点失败")
                print("导航到home点失败")
                logger.error("命令处理器", "导航到home点失败")
                return

            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "分液任务完成")
            self.task_state_machine.complete_task(True, "分液流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")

        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")

    def _execute_split_liquid_async(self, task_id: str, robot_id: str = "robot_a"):
        """
        异步执行SPLIT_LIQUID任务（后台线程）
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        # 获取对应的机器人实例
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            logger.error("命令处理器", f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")

            # 获取存储管理器
            storage_mgr = get_storage_manager()
            back_temp_storage = storage_mgr.get_storage(robot_id)
            '''# 调试代码，模拟新增空瓶子
            storage_mgr.update_slot(robot_id, "glass_bottle_500", 1, 22222)
            storage_mgr.set_bottle_state(robot_id, "glass_bottle_500", 1, BottleState.NOT_SPLIT)'''

            # 订阅导航状态topic
            robot.subscribe_topic(
                topic_name=ROSTopic.NAVIGATION_STATUS,
                msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
            )
            # 大步骤：在待分液区抓取瓶子
            # 导航到待分液区（分液任务点位）
            #input("navigating to waiting split area split...")
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_WAITING_SPLIT_AREA_SPLIT, "导航到待分液区（分液任务点位）(前进进入)")
            # 通过topic触发导航
            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "home"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return
            
            # 导航到分液任务准备点位
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.TASK_PREPARE_SPLIT}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到分液任务准备点位失败")
                logger.error("命令处理器", "导航到分液任务准备点位失败")
                return
            # 连续导航点之间的停顿，放置导航状态刷新不及时
            time.sleep(1.5)
            
            # 导航到分液台待分液区（分液任务点位）(前进进入)
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.WAITING_SPLIT_AREA_SPLIT_FORWARD}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到待分液区（分液任务点位）(前进进入)失败")
                logger.error("命令处理器", "导航到待分液区（分液任务点位）失败")
                return

            # 封装技能：在待分液区抓取瓶子放到后部暂存区
            self._scan_and_store_bottles_loop(robot, storage_mgr, StationCounter.WAITING_SPLIT_AREA, robot_id)
            
            # 获取暂存区所有未分液的瓶子
            glass_bottle_250_in_storage_split = []
            glass_bottle_500_in_storage_split = []
            for bottle_type, slots in back_temp_storage.items():
                for slot_index, slot in enumerate(slots):
                    if not storage_mgr.is_slot_empty(slot):  # 不为空的槽位
                        bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                        # 只处理未分液的瓶子
                        if bottle_info and bottle_info.get("bottle_state") == BottleState.NOT_SPLIT:
                            if bottle_type == "glass_bottle_250":
                                glass_bottle_250_in_storage_split.append({
                                    "bottle_type": bottle_type,
                                    "bottle_id": bottle_info.get("bottle_id"),
                                    "slot_index": slot_index
                                })
                            elif bottle_type == "glass_bottle_500":
                                glass_bottle_500_in_storage_split.append({
                                    "bottle_type": bottle_type,
                                    "bottle_id": bottle_info.get("bottle_id"),
                                    "slot_index": slot_index
                                })
            logger.info("命令处理器", f"暂存区共有 {len(glass_bottle_250_in_storage_split)} 个250ml未分液瓶子和 {len(glass_bottle_500_in_storage_split)} 个500ml未分液瓶子")
            print(f"\n✓ 暂存区共有 {len(glass_bottle_250_in_storage_split)} 个250ml未分液瓶子和 {len(glass_bottle_500_in_storage_split)} 个500ml未分液瓶子")

            # 如果空瓶子不足，补满空瓶
            if len(glass_bottle_250_in_storage_split) < len(glass_bottle_500_in_storage_split):
                # 大步骤：在空瓶区抓取瓶子
                # 导航到空瓶区（分液任务点位）
                #input("navigating to empty bottle area split...")
                self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_EMPTY_BOTTLE_AREA_SPLIT, "导航到空瓶区（分液任务点位）(前进进入)")
                # 通过topic触发导航
                # 导航准备位置
                navigation_prepare_result = robot.send_service_request(
                    robot.get_robot_service(),
                    "navigation_prepare",
                    extra_params={
                        "area": "split_liquid_area"
                    }
                )
                if not navigation_prepare_result:
                    self.task_state_machine.set_error("导航准备位置失败")
                    logger.error("命令处理器", "导航准备位置失败")
                    return
                navigation_publish_result = robot.publish_topic(
                    topic_name="/navigation_control",
                    msg_type="std_msgs/String",
                    msg_data={"data": NavigationPose.EMPTY_BOTTLE_AREA_SPLIT_FORWARD}
                )
                if not navigation_publish_result:
                    self.task_state_machine.set_error("发布导航命令失败")
                    logger.error("命令处理器", "发布导航命令失败")
                    return
                # 等待导航完成
                waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
                if not waiting_navigation_status_result:
                    self.task_state_machine.set_error("导航到空瓶区（分液任务点位）(前进进入)失败")
                    logger.error("命令处理器", "导航到空瓶区（分液任务点位）(前进进入)失败")
                    return
                '''# 调试代码，模拟新增空瓶子
                storage_mgr.update_slot(robot_id, "glass_bottle_250", 1, 66666)
                storage_mgr.set_bottle_state(robot_id, "glass_bottle_250", 1, BottleState.NOT_SPLIT)'''
                # 封装技能：在空瓶区抓取瓶子放到后部暂存区
                self._scan_and_store_bottles_loop(robot, storage_mgr, StationArea.EMPTY_BOTTLE_AREA, robot_id)

            # 获取暂存区所有未分液的瓶子
            glass_bottle_250_in_storage_split = []
            glass_bottle_500_in_storage_split = []
            for bottle_type, slots in back_temp_storage.items():
                for slot_index, slot in enumerate(slots):
                    if not storage_mgr.is_slot_empty(slot):  # 不为空的槽位
                        bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                        # 只处理未分液的瓶子
                        if bottle_info and bottle_info.get("bottle_state") == BottleState.NOT_SPLIT:
                            if bottle_type == "glass_bottle_250":
                                glass_bottle_250_in_storage_split.append({
                                    "bottle_type": bottle_type,
                                    "bottle_id": bottle_info.get("bottle_id"),
                                    "slot_index": slot_index
                                })
                            elif bottle_type == "glass_bottle_500":
                                glass_bottle_500_in_storage_split.append({
                                    "bottle_type": bottle_type,
                                    "bottle_id": bottle_info.get("bottle_id"),
                                    "slot_index": slot_index
                                })
            logger.info("命令处理器", f"暂存区共有 {len(glass_bottle_250_in_storage_split)} 个250ml未分液瓶子和 {len(glass_bottle_500_in_storage_split)} 个500ml未分液瓶子")
            print(f"\n✓ 暂存区共有 {len(glass_bottle_250_in_storage_split)} 个250ml未分液瓶子和 {len(glass_bottle_500_in_storage_split)} 个500ml未分液瓶子")

            # 导航到分液台（分液任务点位）
            #input("navigating to split machine split...")
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_SPLIT_MACHINE_SPLIT, "导航到分液台（分液任务点位）(前进进入)")
            # 通过topic触发导航
            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "split_liquid_area"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.SPLIT_LIQUID_AREA_SPLIT_FORWARD}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到分液台（分液任务点位）(前进进入)失败")
                logger.error("命令处理器", "导航到分液台（分液任务点位）(前进进入)失败")
                return

            # 遍历所有瓶子，执行放置动作
            # 循环次数按照最少的瓶子数量来决定（万一空瓶区数量没了，要区分开来）
            #input("placing bottles to bottle opener split...")
            both_hands_operation = True
            max_loop_count = min(len(glass_bottle_250_in_storage_split), len(glass_bottle_500_in_storage_split))
            for i in range(max_loop_count):
                glass_bottle_250_info = glass_bottle_250_in_storage_split[i]
                glass_bottle_500_info = glass_bottle_500_in_storage_split[i]
                # 双手分开操作
                if not both_hands_operation:
                    # 步骤1: 从后部暂存区拿起瓶子,并放下瓶子到500ml开瓶器
                    pick_up_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "pick_from_back_temp",
                        extra_params={
                            "type": glass_bottle_500_info["bottle_type"],
                            "target_pose": "bottle_opener_500",
                            "end_pose": glass_bottle_500_info["slot_index"]
                        }
                    )
                    if not pick_up_result:
                        logger.error("命令处理器", "拿起瓶子到500ml开瓶器失败")
                        return
                    print(f"✓ 步骤1完成: 从后部暂存区拿起瓶子成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_500_info['bottle_id']} 已从后部暂存区拿起成功")

                    print(f"放下瓶子 {glass_bottle_500_info['bottle_id']} 到500ml开瓶器")
                    put_down_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "put_down",
                        extra_params={
                            "type": glass_bottle_500_info["bottle_type"],
                            "target_pose": "bottle_opener_500"
                        }
                    )
                    if not put_down_result:
                        self.task_state_machine.set_error("放下瓶子到500ml开瓶器失败")
                        logger.error("命令处理器", "放下瓶子到500ml开瓶器失败")
                        return
                    print(f"✓ 步骤2完成: 放下瓶子到500ml开瓶器成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_500_info['bottle_id']} 已放下到500ml开瓶器")
                    self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER_500_SUCCESS, "放下瓶子到500ml开瓶器成功")

                    # ========== 步骤3和步骤4并行执行 ==========
                    # 清除之前的事件状态
                    self.pick_from_opener_500_event.clear()
                    self.pick_from_opener_250_event.clear()
                    self.pick_from_opener_500_data = None
                    self.pick_from_opener_250_data = None
                    
                    # 用于存储步骤3的结果
                    step3_result_holder = {"success": False, "completed": False}
                    # 用于存储步骤4等待结果
                    step4_result_holder = {"success": False}
                    
                    def execute_step3_and_step4():
                        """在线程中执行步骤3和步骤4"""
                        # 步骤3: 从后部暂存区抓取瓶子，并放下瓶子到250ml开瓶器
                        pick_up_result = robot.send_service_request(
                            robot.get_robot_service(),
                            "pick_from_back_temp",
                            extra_params={
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "back_temp_250_" + str(glass_bottle_250_info["slot_index"])
                            }
                        )
                        if not pick_up_result:
                            logger.error("命令处理器", f"[并行] 从后部暂存区抓取瓶子失败: {glass_bottle_250_info['bottle_id']}")
                            return
                        print(f"✓ [并行] 从后部暂存区抓取瓶子成功: {glass_bottle_250_info['bottle_id']}")
                        logger.info("命令处理器", f"[并行] 瓶子 {glass_bottle_250_info['bottle_id']} 已从后部暂存区抓取成功")

                        logger.info("命令处理器", f"[并行] 放下瓶子 {glass_bottle_250_info['bottle_id']} 到250ml开瓶器")
                        print(f"[并行] 放下瓶子 {glass_bottle_250_info['bottle_id']} 到250ml开瓶器")
                        put_down_250_result = robot.send_service_request(
                            robot.get_robot_service(),
                            "put_down",
                            extra_params={
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "bottle_opener_250",
                            }
                        )
                        self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER_250_SUCCESS, "放下瓶子到250ml开瓶器成功")
                        if put_down_250_result:
                            step3_result_holder["success"] = True
                            self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER_250_SUCCESS, "放下瓶子到250ml开瓶器成功")
                            logger.info("命令处理器", f"[并行] 瓶子 {glass_bottle_250_info['bottle_id']} 已放下到250ml开瓶器")
                            print(f"✓ [并行] 瓶子 {glass_bottle_250_info['bottle_id']} 已放下到250ml开瓶器")
                            
                            # 步骤4: 等待250ml开瓶器完成开瓶
                            print(f"\n======================================================================")
                            print(f"【步骤4】等待250ml开瓶器完成开瓶...")
                            print(f"请使用以下命令发送完成信号:")
                            print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_250_command.json")
                            print(f"======================================================================\n")
                            
                            # 等待HTTP命令
                            if self.pick_from_opener_250_event.wait(timeout=600):  # 10分钟超时
                                if self.pick_from_opener_250_data and self.pick_from_opener_250_data.get("success"):
                                    step4_result_holder["success"] = True
                                    logger.info("命令处理器", "[并行] 250ml开瓶器完成信号已接收")
                                    print(f"✓ [并行] 250ml开瓶器完成信号已接收")
                                else:
                                    logger.error("命令处理器", "[并行] 250ml开瓶器完成信号为失败")
                                    print(f"✗ [并行] 250ml开瓶器完成信号为失败")
                            else:
                                logger.error("命令处理器", "[并行] 等待250ml开瓶器完成信号超时")
                                print(f"✗ [并行] 等待250ml开瓶器完成信号超时")
                        else:
                            logger.error("命令处理器", "[并行] 放下瓶子到250ml开瓶器失败")
                            print(f"✗ [并行] 放下瓶子到250ml开瓶器失败")
                        step3_result_holder["completed"] = True
                    
                    # 启动步骤3和步骤4的线程
                    step3_4_thread = threading.Thread(target=execute_step3_and_step4, daemon=True)
                    step3_4_thread.start()
                    
                    # 步骤2: 等待500ml开瓶器完成开瓶（主线程）
                    print(f"\n======================================================================")
                    print(f"【步骤2】等待500ml开瓶器完成开瓶...")
                    print(f"【步骤3】同时执行: 放下瓶子到250ml开瓶器")
                    print(f"请使用以下命令发送500ml完成信号:")
                    print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_500_command.json")
                    print(f"======================================================================\n")
                    
                    step2_success = False
                    if self.pick_from_opener_500_event.wait(timeout=600):  # 10分钟超时
                        if self.pick_from_opener_500_data and self.pick_from_opener_500_data.get("success"):
                            step2_success = True
                            logger.info("命令处理器", "500ml开瓶器完成信号已接收")
                            print(f"✓ 500ml开瓶器完成信号已接收")
                        else:
                            self.task_state_machine.set_error("500ml开瓶器完成信号为失败")
                            logger.error("命令处理器", "500ml开瓶器完成信号为失败")
                            return
                    else:
                        self.task_state_machine.set_error("等待500ml开瓶器完成信号超时")
                        logger.error("命令处理器", "等待500ml开瓶器完成信号超时")
                        return
                    
                    # 等待步骤3完成
                    step3_4_thread.join(timeout=10)  # 等待步骤3完x成，最多10秒
                    
                    # 检查步骤2成功 AND 步骤3成功
                    if not (step2_success and step3_result_holder["success"]):
                        self.task_state_machine.set_error("步骤2或步骤3执行失败")
                        logger.error("命令处理器", f"步骤2或步骤3执行失败: step2={step2_success}, step3={step3_result_holder['success']}")
                        return
                    
                    print(f"✓ 步骤2和步骤3都已成功完成，继续执行步骤5")

                    # 步骤5: 从500ml开瓶器抓取
                    input("grab_from_bottle_opener")
                    self.task_state_machine.update_step(TaskStep.GRABBING_FROM_BOTTLE_OPENER_500, "从500ml开瓶器抓取瓶子")
                    logger.info("命令处理器", "从500ml开瓶器抓取瓶子")
                    print(f"步骤5: 从500ml开瓶器抓取瓶子")
                    grab_result_500 = robot.send_service_request(
                        robot.get_robot_service(),
                        "grab_from_bottle_opener",
                        extra_params={
                            "target_pose": "bottle_opener_500"
                        }
                    )
                    if not grab_result_500:
                        self.task_state_machine.set_error("从500ml开瓶器抓取失败")
                        logger.error("命令处理器", "从500ml开瓶器抓取失败")
                        return
                    print(f"✓ 步骤5完成: 从500ml开瓶器抓取成功")
                    
                    # 等待步骤4完成（步骤4在步骤3之后已经开始等待）
                    # 步骤3-4线程应该已经在执行步骤4的等待了
                    step3_4_thread.join(timeout=600)  # 等待步骤4完成
                    
                    # 检查步骤4是否成功
                    if not step4_result_holder["success"]:
                        self.task_state_machine.set_error("步骤4执行失败: 250ml开瓶器完成信号未收到或失败")
                        logger.error("命令处理器", "步骤4执行失败")
                        return
                    
                    print(f"✓ 步骤4已成功完成，继续执行步骤6")
                    
                    # 步骤6: 从250ml开瓶器抓取
                    input("grab_from_bottle_opener")
                    self.task_state_machine.update_step(TaskStep.GRABBING_FROM_BOTTLE_OPENER_250, "从250ml开瓶器抓取瓶子")
                    logger.info("命令处理器", "从250ml开瓶器抓取瓶子")
                    print(f"步骤6: 从250ml开瓶器抓取瓶子")
                    grab_result_250 = robot.send_service_request(
                        robot.get_robot_service(),
                        "grab_from_bottle_opener",
                        extra_params={
                            "target_pose": "bottle_opener_250"
                        }
                    )
                    if not grab_result_250:
                        self.task_state_machine.set_error("从250ml开瓶器抓取失败")
                        logger.error("命令处理器", "从250ml开瓶器抓取失败")
                        return
                    print(f"✓ 步骤6完成: 从250ml开瓶器抓取成功")
                    
                    # 步骤7: 分液
                    input("pouring_water")
                    self.task_state_machine.update_step(TaskStep.POURING_WATER, "分液")
                    pull_water_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "pouring_water",
                        extra_params={
                            "volumn": 200
                        }
                    )
                    if not pull_water_result:
                        self.task_state_machine.set_error("抽水失败")
                        logger.error("命令处理器", "抽水失败")
                        return
                    
                    # 步骤8: 放置到500ml开瓶器
                    input("put_to_bottle_opener")
                    self.task_state_machine.update_step(TaskStep.PUTTING_TO_BOTTLE_OPENER_500, "放置到500ml开瓶器")
                    put_to_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "put_to_bottle_opener",
                        extra_params={
                            "target_pose": "bottle_opener_500"
                        }
                    )
                    if not put_to_result:
                        self.task_state_machine.set_error("放置到开瓶器失败")
                        logger.error("命令处理器", "放置到开瓶器失败")
                        return
                    
                    # ========== 步骤9和步骤10并行执行 ==========
                    # 清除之前的事件状态（复用步骤2-6的事件）
                    self.pick_from_opener_500_event.clear()
                    self.pick_from_opener_250_event.clear()
                    self.pick_from_opener_500_data = None
                    self.pick_from_opener_250_data = None
                    
                    # 用于存储步骤10的结果
                    step10_result_holder = {"success": False, "completed": False}
                    # 用于存储步骤11等待结果
                    step11_result_holder = {"success": False}
                    
                    def execute_step10_and_step11():
                        """在线程中执行步骤10和步骤11"""
                        # 步骤10: 放置到250ml开瓶器
                        input("put_to_bottle_opener")
                        self.task_state_machine.update_step(TaskStep.PUTTING_TO_BOTTLE_OPENER_250, "放置到250ml开瓶器")
                        logger.info("命令处理器", "[并行] 放置到250ml开瓶器")
                        print(f"[并行] 步骤10: 放置到250ml开瓶器")
                        put_to_result_250 = robot.send_service_request(
                            robot.get_robot_service(),
                            "put_to_bottle_opener",
                            extra_params={
                                "target_pose": "bottle_opener_250"
                            }
                        )
                        if put_to_result_250:
                            step10_result_holder["success"] = True
                            logger.info("命令处理器", "[并行] 放置到250ml开瓶器成功")
                            print(f"✓ [并行] 步骤10完成: 放置到250ml开瓶器成功")
                            
                            # 步骤11: 等待250ml开瓶器完成关瓶
                            print(f"\n======================================================================")
                            print(f"【步骤11】等待250ml开瓶器完成关瓶...")
                            print(f"请使用以下命令发送完成信号:")
                            print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_250_command.json")
                            print(f"======================================================================\n")
                            
                            # 等待HTTP命令
                            if self.pick_from_opener_250_event.wait(timeout=600):  # 10分钟超时
                                if self.pick_from_opener_250_data and self.pick_from_opener_250_data.get("success"):
                                    step11_result_holder["success"] = True
                                    logger.info("命令处理器", "[并行] 250ml开瓶器关瓶完成信号已接收")
                                    print(f"✓ [并行] 250ml开瓶器关瓶完成信号已接收")
                                else:
                                    logger.error("命令处理器", "[并行] 250ml开瓶器关瓶完成信号为失败")
                                    print(f"✗ [并行] 250ml开瓶器关瓶完成信号为失败")
                            else:
                                logger.error("命令处理器", "[并行] 等待250ml开瓶器关瓶完成信号超时")
                                print(f"✗ [并行] 等待250ml开瓶器关瓶完成信号超时")
                        else:
                            logger.error("命令处理器", "[并行] 放置到250ml开瓶器失败")
                            print(f"✗ [并行] 放置到250ml开瓶器失败")
                        step10_result_holder["completed"] = True
                    
                    # 启动步骤10和步骤11的线程
                    step10_11_thread = threading.Thread(target=execute_step10_and_step11, daemon=True)
                    step10_11_thread.start()
                    
                    # 步骤9: 等待500ml开瓶器完成关瓶（主线程）
                    print(f"\n======================================================================")
                    print(f"【步骤9】等待500ml开瓶器完成关瓶...")
                    print(f"【步骤10】同时执行: 放置到250ml开瓶器")
                    print(f"请使用以下命令发送500ml完成信号:")
                    print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_500_command.json")
                    print(f"======================================================================\n")
                    
                    step9_success = False
                    if self.pick_from_opener_500_event.wait(timeout=600):  # 10分钟超时
                        if self.pick_from_opener_500_data and self.pick_from_opener_500_data.get("success"):
                            step9_success = True
                            logger.info("命令处理器", "500ml开瓶器关瓶完成信号已接收")
                            print(f"✓ 500ml开瓶器关瓶完成信号已接收")
                        else:
                            self.task_state_machine.set_error("500ml开瓶器关瓶完成信号为失败")
                            logger.error("命令处理器", "500ml开瓶器关瓶完成信号为失败")
                            return
                    else:
                        self.task_state_machine.set_error("等待500ml开瓶器关瓶完成信号超时")
                        logger.error("命令处理器", "等待500ml开瓶器关瓶完成信号超时")
                        return
                    
                    # 等待步骤10完成
                    step10_11_thread.join(timeout=10)  # 等待步骤10完成，最多10秒
                    
                    # 检查步骤9成功 AND 步骤10成功
                    if not (step9_success and step10_result_holder["success"]):
                        self.task_state_machine.set_error("步骤9或步骤10执行失败")
                        logger.error("命令处理器", f"步骤9或步骤10执行失败: step9={step9_success}, step10={step10_result_holder['success']}")
                        return
                    
                    print(f"✓ 步骤9和步骤10都已成功完成，继续执行步骤12")

                    # 步骤12: 从500ml开瓶器取回放到后部暂存区
                    input("pick_from_bottle_opener_to_back_temp")
                    self.task_state_machine.update_step(TaskStep.PICK_FROM_BOTTLE_OPENER_500_TO_BACK_TEMP, "从500ml开瓶器取回放到后部暂存区")
                    logger.info("命令处理器", "从500ml开瓶器取回放到后部暂存区")
                    print(f"步骤12: 从500ml开瓶器取回放到后部暂存区")
                    pick_back_result_500 = robot.send_service_request(
                        robot.get_robot_service(),
                        "pick_from_bottle_opener_to_back_temp",
                        extra_params={
                            "target_pose": "back_temp_500_0"
                        }
                    )
                    if not pick_back_result_500:
                        self.task_state_machine.set_error("从500ml开瓶器取回失败")
                        logger.error("命令处理器", "从500ml开瓶器取回失败")
                        return
                    print(f"✓ 步骤12完成: 从500ml开瓶器取回成功")
                    
                    # 等待步骤11完成（步骤11在步骤10之后已经开始等待）
                    step10_11_thread.join(timeout=600)  # 等待步骤11完成
                    
                    # 检查步骤11是否成功
                    if not step11_result_holder["success"]:
                        self.task_state_machine.set_error("步骤11执行失败: 250ml开瓶器关瓶完成信号未收到或失败")
                        logger.error("命令处理器", "步骤11执行失败")
                        return
                    
                    print(f"✓ 步骤11已成功完成，继续执行步骤13")
                    
                    # 步骤13: 从250ml开瓶器取回放到后部暂存区
                    input("pick_from_bottle_opener_to_back_temp")
                    self.task_state_machine.update_step(TaskStep.PICK_FROM_BOTTLE_OPENER_250_TO_BACK_TEMP, "从250ml开瓶器取回放到后部暂存区")
                    logger.info("命令处理器", "从250ml开瓶器取回放到后部暂存区")
                    print(f"步骤13: 从250ml开瓶器取回放到后部暂存区")
                    pick_back_result_250 = robot.send_service_request(
                        robot.get_robot_service(),
                        "pick_from_bottle_opener_to_back_temp",
                        extra_params={
                            "target_pose": "back_temp_250_0"
                        }
                    )
                    if not pick_back_result_250:
                        self.task_state_machine.set_error("从250ml开瓶器取回失败")
                        logger.error("命令处理器", "从250ml开瓶器取回失败")
                        return
                    print(f"✓ 步骤13完成: 从250ml开瓶器取回成功")
                    
                    # 标记瓶子为已分液
                    storage_mgr.set_bottle_state(robot_id, glass_bottle_500_info["bottle_type"], glass_bottle_500_info["slot_index"], BottleState.SPLIT_DONE)
                    storage_mgr.set_bottle_state(robot_id, glass_bottle_250_info["bottle_type"], glass_bottle_250_info["slot_index"], BottleState.SPLIT_DONE)


                    logger.info("命令处理器", f"瓶子 {glass_bottle_500_info['bottle_id']} 和 {glass_bottle_250_info['bottle_id']} 已标记为已分液")
                # 双手操作
                else:
                    # 步骤1: 从后部暂存区拿取指定位置瓶子
                    #input("picking bottle from back storage...")
                    print(f"从后部暂存区拿取指定位置瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']}")
                    pick_up_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "pick_from_back_temp_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "back_temp_250_" + str(glass_bottle_250_info["slot_index"])
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "back_temp_500_" + str(glass_bottle_500_info["slot_index"])
                            }
                        }
                    )
                    print(f"pick_up_result: {pick_up_result}")
                    if not pick_up_result:
                        self.task_state_machine.set_error("从后部暂存区拿取指定位置瓶子失败")
                        logger.error("命令处理器", "从后部暂存区拿取指定位置瓶子失败")
                        return
                    print(f"✓ 步骤1完成: 从后部暂存区拿取指定位置瓶子成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已从后部暂存区拿取成功")

                    # 步骤2：双手放下手中瓶子到开瓶器
                    #input("put_down_bothhand")
                    put_down_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "put_down_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "bottle_opener_250"
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "bottle_opener_500"
                            }
                        }
                    )
                    if not put_down_result:
                        self.task_state_machine.set_error("双手放下手中瓶子到开瓶器失败")
                        logger.error("命令处理器", "双手放下手中瓶子到开瓶器失败")
                        return
                    print(f"✓ 步骤2完成: 双手放下手中瓶子到开瓶器成功")
                    self.task_state_machine.update_step(TaskStep.PUTTING_TO_BOTTLE_OPENER_500_AND_250_SUCCESS, "放下瓶子到500ml和250ml开瓶器成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已双手放下手中瓶子到开瓶器成功")

                    # 步骤3，4：等待开瓶完成命令（并行执行）
                    print(f"\n======================================================================")
                    print(f"【步骤2】等待500ml开瓶器完成开瓶...")
                    print(f"【步骤3】等待250ml开瓶器完成开瓶...")
                    print(f"请使用以下命令发送500ml完成信号:")
                    print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_500_command.json")
                    print(f"======================================================================\n")
                    # 清除之前的事件状态
                    self.pick_from_opener_500_event.clear()
                    self.pick_from_opener_250_event.clear()
                    self.pick_from_opener_500_data = None
                    self.pick_from_opener_250_data = None
                    
                    # 用于存储步骤3等待结果
                    step3_result_holder = {"success": False}
                    # 用于存储步骤4等待结果
                    step4_result_holder = {"success": False}
                    def execute_step3():
                        """在线程中执行步骤3"""
                        # 步骤3: 等待500ml开瓶器完成开瓶
                        if self.pick_from_opener_500_event.wait(timeout=600):  # 10分钟超时
                            if self.pick_from_opener_500_data and self.pick_from_opener_500_data.get("success"):
                                step3_result_holder["success"] = True
                                logger.info("命令处理器", "500ml开瓶器完成开瓶信号已接收")
                                print(f"✓ 500ml开瓶器完成开瓶信号已接收")
                            else:
                                self.task_state_machine.set_error("500ml开瓶器完成开瓶信号为失败")
                                logger.error("命令处理器", "500ml开瓶器完成开瓶信号为失败")
                                return
                        else:
                            self.task_state_machine.set_error("等待500ml开瓶器完成开瓶信号超时")
                            logger.error("命令处理器", "等待500ml开瓶器完成开瓶信号超时")
                            return
                        step3_result_holder["success"] = True
                    
                    # 启动步骤3线程
                    step3_thread = threading.Thread(target=execute_step3, daemon=True)
                    step3_thread.start()

                    # 步骤4: 等待250ml开瓶器完成开瓶
                    if self.pick_from_opener_250_event.wait(timeout=600):  # 10分钟超时
                        if self.pick_from_opener_250_data and self.pick_from_opener_250_data.get("success"):
                            step4_result_holder["success"] = True
                            logger.info("命令处理器", "250ml开瓶器完成开瓶信号已接收")
                            print(f"✓ 250ml开瓶器完成开瓶信号已接收")
                        else:
                            self.task_state_machine.set_error("250ml开瓶器完成开瓶信号为失败")
                            logger.error("命令处理器", "250ml开瓶器完成开瓶信号为失败")
                            return
                    else:
                        self.task_state_machine.set_error("等待250ml开瓶器完成开瓶信号超时")
                        logger.error("命令处理器", "等待250ml开瓶器完成开瓶信号超时")
                        return
                    step4_result_holder["success"] = True

                    # 等待步骤3完成
                    step3_thread.join(timeout=10)  # 等待步骤3完成，最多10秒
                    
                    # 检查步骤3是否成功
                    if not step3_result_holder["success"]:
                        self.task_state_machine.set_error("步骤3执行失败")
                        logger.error("命令处理器", "步骤3执行失败")
                        return
                    
                    # 步骤5：双手从开瓶器抓取瓶子(握住状态)
                    #input("grab_from_bottle_opener_bothhand")
                    self.task_state_machine.update_step(TaskStep.GRABBING_FROM_BOTTLE_OPENER_500_AND_250, "双手从开瓶器抓取瓶子")
                    logger.info("命令处理器", "双手从开瓶器抓取瓶子")
                    print(f"步骤5: 双手从开瓶器抓取瓶子")
                    grab_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "grab_from_bottle_opener_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "bottle_opener_250"
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "bottle_opener_500"
                            }
                        }
                    )
                    if not grab_result:
                        self.task_state_machine.set_error("双手从开瓶器抓取瓶子失败")
                        logger.error("命令处理器", "双手从开瓶器抓取瓶子失败")
                        return
                    print(f"✓ 步骤5完成: 双手从开瓶器抓取瓶子成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已双手从开瓶器抓取瓶子成功")

                    # 步骤6：分液
                    #input("pouring_water")
                    self.task_state_machine.update_step(TaskStep.POURING_WATER, "分液")
                    logger.info("命令处理器", "分液")
                    print(f"步骤6: 分液")
                    pour_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "pouring_water",
                        extra_params={
                            "volume": "250"
                        }
                    )
                    if not pour_result:
                        self.task_state_machine.set_error("分液失败")
                        logger.error("命令处理器", "分液失败")
                        return
                    print(f"✓ 步骤6完成: 分液成功")
                    logger.info("命令处理器", "分液成功")
                    
                    # 步骤7：双手把手上的瓶子放到目标开瓶器上（握住状态）
                    #input("put_to_bottle_opener_bothhand")
                    self.task_state_machine.update_step(TaskStep.PUTTING_TO_BOTTLE_OPENER_500_AND_250, "双手把手上的瓶子放到目标开瓶器上")
                    logger.info("命令处理器", "双手把手上的瓶子放到目标开瓶器上")
                    print(f"步骤7: 双手把手上的瓶子放到目标开瓶器上")
                    put_to_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "put_to_bottle_opener_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "bottle_opener_250"
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "bottle_opener_500"
                            }
                        }
                    )
                    if not put_to_result:
                        self.task_state_machine.set_error("双手把手上的瓶子放到目标开瓶器上失败")
                        logger.error("命令处理器", "双手把手上的瓶子放到目标开瓶器上失败")
                        return
                    print(f"✓ 步骤7完成: 双手把手上的瓶子放到目标开瓶器上成功")
                    self.task_state_machine.update_step(TaskStep.PUTTING_TO_BOTTLE_OPENER_500_AND_250_SUCCESS, "双手把手上的瓶子放到目标开瓶器上成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已双手把手上的瓶子放到目标开瓶器上成功")
                    
                    # 步骤8,9：等待关瓶完成命令（并行执行）
                    print(f"\n======================================================================")
                    print(f"【步骤8】等待500ml开瓶器完成关瓶...")
                    print(f"【步骤9】等待250ml开瓶器完成关瓶...")
                    print(f"请使用以下命令发送500ml完成信号:")
                    print(f"curl -X POST http://localhost:8848 -d @test_commands/PICK_FROM_BOTTLE_OPENER_500_command.json")
                    print(f"======================================================================\n")
                    # 清除之前的事件状态
                    self.pick_from_opener_500_event.clear()
                    self.pick_from_opener_250_event.clear()
                    self.pick_from_opener_500_data = None
                    self.pick_from_opener_250_data = None
                    
                    # 用于存储步骤8等待结果
                    step8_result_holder = {"success": False}
                    # 用于存储步骤9等待结果
                    step9_result_holder = {"success": False}
                    def execute_step8():
                        """在线程中执行步骤8"""
                        # 步骤8: 等待500ml开瓶器完成关瓶
                        if self.pick_from_opener_500_event.wait(timeout=600):  # 10分钟超时
                            if self.pick_from_opener_500_data and self.pick_from_opener_500_data.get("success"):
                                step8_result_holder["success"] = True
                                logger.info("命令处理器", "500ml开瓶器完成关瓶信号已接收")
                                print(f"✓ 500ml开瓶器完成关瓶信号已接收")
                            else:
                                self.task_state_machine.set_error("500ml开瓶器完成关瓶信号为失败")
                                logger.error("命令处理器", "500ml开瓶器完成关瓶信号为失败")
                                return
                        else:
                            self.task_state_machine.set_error("等待500ml开瓶器完成关瓶信号超时")
                            logger.error("命令处理器", "等待500ml开瓶器完成关瓶信号超时")
                            return
                        step8_result_holder["success"] = True
                    
                    # 启动步骤8线程
                    step8_thread = threading.Thread(target=execute_step8, daemon=True)
                    step8_thread.start()

                    # 步骤9: 等待250ml开瓶器完成关瓶
                    if self.pick_from_opener_250_event.wait(timeout=600):  # 10分钟超时
                        if self.pick_from_opener_250_data and self.pick_from_opener_250_data.get("success"):
                            step9_result_holder["success"] = True
                            logger.info("命令处理器", "250ml开瓶器完成关瓶信号已接收")
                            print(f"✓ 250ml开瓶器完成关瓶信号已接收")
                        else:
                            self.task_state_machine.set_error("250ml开瓶器完成关瓶信号为失败")
                            logger.error("命令处理器", "250ml开瓶器完成关瓶信号为失败")
                            return
                    else:
                        self.task_state_machine.set_error("等待250ml开瓶器完成关瓶信号超时")
                        logger.error("命令处理器", "等待250ml开瓶器完成关瓶信号超时")
                        return
                    step9_result_holder["success"] = True

                    # 等待步骤8完成
                    step8_thread.join(timeout=10)  # 等待步骤8完成，最多10秒
                    
                    # 检查步骤8是否成功
                    if not step8_result_holder["success"]:
                        self.task_state_machine.set_error("步骤8执行失败")
                        logger.error("命令处理器", "步骤8执行失败")
                        return
                    
                    # 步骤10：双手从开瓶器提起瓶子
                    #input("raising_from_bottle_opener_bothhand")
                    self.task_state_machine.update_step(TaskStep.RAISING_FROM_BOTTLE_OPENER_500_AND_250, "双手从开瓶器提起瓶子")
                    logger.info("命令处理器", "双手从开瓶器提起瓶子")
                    print(f"步骤10: 双手从开瓶器提起瓶子")
                    raise_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "pick_from_bottle_opener_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "bottle_opener_250"
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "bottle_opener_500"
                            } 
                        }
                    )
                    if not raise_result:
                        self.task_state_machine.set_error("双手从开瓶器提起瓶子失败")
                        logger.error("命令处理器", "双手从开瓶器提起瓶子失败")
                        return
                    print(f"✓ 步骤10完成: 双手从开瓶器提起瓶子成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已双手从开瓶器提起瓶子成功")

                    # 步骤11：双手把手上的瓶子放到后部暂存区
                    #input("put_down_back_temp_bothhand")
                    self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_BACK_TEMP_500_AND_250, "双手把手上的瓶子放到后部暂存区")
                    logger.info("命令处理器", "双手把手上的瓶子放到后部暂存区")
                    print(f"步骤11: 双手把手上的瓶子放到后部暂存区")
                    put_down_result = robot.send_service_request(
                        robot.get_robot_service(),
                        "put_down_back_temp_bothhand",
                        extra_params={
                            "right_hand": {
                                "type": glass_bottle_250_info["bottle_type"],
                                "target_pose": "back_temp_250_" + str(glass_bottle_250_info["slot_index"])
                            },
                            "left_hand": {
                                "type": glass_bottle_500_info["bottle_type"],
                                "target_pose": "back_temp_500_" + str(glass_bottle_500_info["slot_index"])
                            }
                        }
                    )
                    if not put_down_result:
                        self.task_state_machine.set_error("双手把手上的瓶子放到后部暂存区失败")
                        logger.error("命令处理器", "双手把手上的瓶子放到后部暂存区失败")
                        return
                    print(f"✓ 步骤11完成: 双手把手上的瓶子放到后部暂存区成功")
                    logger.info("命令处理器", f"瓶子 {glass_bottle_250_info['bottle_id']} 和 {glass_bottle_500_info['bottle_id']} 已双手把手上的瓶子放到后部暂存区成功")
                    # 标记瓶子为已分液
                    storage_mgr.set_bottle_state(robot_id, glass_bottle_500_info["bottle_type"], glass_bottle_500_info["slot_index"], BottleState.SPLIT_DONE)
                    storage_mgr.set_bottle_state(robot_id, glass_bottle_250_info["bottle_type"], glass_bottle_250_info["slot_index"], BottleState.SPLIT_DONE)
                    logger.info("命令处理器", f"瓶子 {glass_bottle_500_info['bottle_id']} 和 {glass_bottle_250_info['bottle_id']} 已标记为已分液")
            
            # 步骤15： 导航到500ml分液完成暂存区（分液任务点位）
            # 使用topic触发导航 
            #input("navigating to 500ml split done area split...")
            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "split_liquid_area"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return_scan_and_store_bottles_loop_press_button
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到500ml分液完成暂存区（分液任务点位）(后退进入)失败")
                logger.error("命令处理器", "导航到500ml分液完成暂存区（分液任务点位）(后退进入)失败")
                return
            
            # 步骤16： 把后部暂存区所有分液完成的500ml瓶子放到500ml分液完成暂存区
            #input("putting down 500ml split done area...")
            self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_500ML_SPLIT_DONE_AREA, "把后部暂存区所有分液完成的500ml瓶子放到500ml分液完成暂存区")
            logger.info("命令处理器", "把后部暂存区所有分液完成的500ml瓶子放到500ml分液完成暂存区")
            print(f"步骤16: 把后部暂存区所有分液完成的500ml瓶子放到500ml分液完成暂存区")
            # 遍历所有已分液的500ml瓶子
            all_bottles_in_storage = []
            bottle_type = "glass_bottle_500"
            print(f"storage_mgr.get_storage(robot_id)['{bottle_type}']: {storage_mgr.get_storage(robot_id)[bottle_type]}")
            for slot_index, slot in enumerate(storage_mgr.get_storage(robot_id)[bottle_type]):
                bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                # 检查 bottle_info 是否存在且状态为已分液
                if bottle_info and bottle_info.get("bottle_state") == BottleState.SPLIT_DONE:
                    all_bottles_in_storage.append(bottle_info)
            print(f"all_bottles_in_storage: {all_bottles_in_storage}")
            self._put_bottles_two_hands_loop(robot, storage_mgr, "500ml_split_done_area", all_bottles_in_storage, robot_id)

            # 步骤17： 导航到250ml分液完成暂存区（分液任务点位）
            # 使用topic触发导航
            #input("navigating to 250ml split done area split...")
            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "split_liquid_area"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return
            navigation_publish_result = robot.publish_topic(
                topic_name="/navigation_control",
                msg_type="std_msgs/String",
                msg_data={"data": NavigationPose.SPLIT_DONE_250ML_AREA_SPLIT_BACK}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到250ml分液完成暂存区（分液任务点位）(后退进入)失败")
                logger.error("命令处理器", "导航到250ml分液完成暂存区（分液任务点位）(后退进入)失败")
                return

            # 步骤18： 把后部暂存区所有分液完成的250ml瓶子放到250ml分液完成暂存区
            #input("putting down 250ml split done area...")
            self.task_state_machine.update_step(TaskStep.PUTTING_DOWN_TO_250ML_SPLIT_DONE_AREA, "把后部暂存区所有分液完成的250ml瓶子放到250ml分液完成暂存区")
            logger.info("命令处理器", "把后部暂存区所有分液完成的250ml瓶子放到250ml分液完成暂存区")
            print(f"步骤18: 把后部暂存区所有分液完成的250ml瓶子放到250ml分液完成暂存区")
            # 遍历所有已分液的250ml瓶子
            all_bottles_in_storage = []
            bottle_type = "glass_bottle_250"
            for slot_index, slot in enumerate(storage_mgr.get_storage(robot_id)[bottle_type]):
                bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                # 检查 bottle_info 是否存在且状态为已分液
                if bottle_info and bottle_info.get("bottle_state") == BottleState.SPLIT_DONE:
                    all_bottles_in_storage.append(bottle_info)
            print(f"all_bottles_in_storage: {all_bottles_in_storage}")
            self._put_bottles_two_hands_loop(robot, storage_mgr, "250ml_split_done_area", all_bottles_in_storage, robot_id)


            # 导航准备位置
            navigation_prepare_result = robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare",
                extra_params={
                    "area": "split_liquid_area"
                }
            )
            if not navigation_prepare_result:
                self.task_state_machine.set_error("导航准备位置失败")
                logger.error("命令处理器", "导航准备位置失败")
                return
                
            # 导航到分液任务home点位
            '''navigation_publish_result = robot.publish_topic(
                topic_name="/navigation_control",
                msg_type="std_msgs/String",
                msg_data={"data": NavigationPose.HOME_ROBOT_B}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到分液任务home点位失败")
                logger.error("命令处理器", "导航到分液任务home点位失败")
                return'''

            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "分液任务完成")
            self.task_state_machine.complete_task(True, "分液流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")
            
        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")
    
    def handle_enter_id(self, cmd_data: Dict) -> Dict:
        """处理ENTER_ID命令"""
        params = cmd_data.get("params", {})
        bottle_id = params.get("bottle_id")
        object_type = params.get("type")
        
        logger.info("命令处理器", f"ENTER_ID - {bottle_id}")
        
        # 标记瓶子已扫码
        self.bottle_manager.mark_scanned(bottle_id)
        
        return {
            "success": True,
            "message": "ID录入成功",
            "bottle_id": bottle_id
        }
    
    def handle_scan_qrcode_enter_id(self, cmd_data: Dict) -> Dict:
        """
        处理SCAN_QRCODE_ENTER_ID命令
        接收HTTP发送的瓶子ID信息，触发等待事件
        """
        params = cmd_data.get("params", {})
        success = params.get("success")
        qrcode_id = params.get("qrcode_id")
        object_type = params.get("type")
        bottle_id = str(object_type) + "_" + str(qrcode_id)
        task = params.get("task")
        
        logger.info("命令处理器", f"SCAN_QRCODE_ENTER_ID - bottle_id: {bottle_id}, type: {object_type}, task: {task}")
        
        if not bottle_id or not object_type:
            logger.error("命令处理器", "SCAN_QRCODE_ENTER_ID缺少必要参数")
            return make_error_response(
                ErrorCode.MISSING_PARAMS,
                "缺少bottle_id或type参数"
            )
        
        # 保存数据并触发事件
        self.scan_enter_id_data = {
            "success": success,
            "bottle_id": bottle_id,
            "type": object_type,
            "task": task
        }
        self.scan_enter_id_event.set()
        
        logger.info("命令处理器", f"瓶子ID已录入并触发事件: {bottle_id}")
        
        return {
            "success": success,
            "message": "瓶子ID已录入，SCAN_QRCODE流程继续",
            "bottle_id": bottle_id,
            "qrcode_id": qrcode_id,
            "type": object_type
        }
    
    def handle_pick_from_bottle_opener_500(self, cmd_data: Dict) -> Dict:
        """
        处理PICK_FROM_BOTTLE_OPENER_500命令
        接收HTTP发送的500ml开瓶器完成信号
        """
        params = cmd_data.get("params", {})
        success = params.get("success", False)
        
        logger.info("命令处理器", f"PICK_FROM_BOTTLE_OPENER_500 - success: {success}")
        
        # 保存数据并触发事件
        self.pick_from_opener_500_data = {"success": success}
        self.pick_from_opener_500_event.set()
        
        return {
            "success": True,
            "message": "500ml开瓶器完成信号已接收"
        }
    
    def handle_pick_from_bottle_opener_250(self, cmd_data: Dict) -> Dict:
        """
        处理PICK_FROM_BOTTLE_OPENER_250命令
        接收HTTP发送的250ml开瓶器完成信号
        """
        params = cmd_data.get("params", {})
        success = params.get("success", False)
        
        logger.info("命令处理器", f"PICK_FROM_BOTTLE_OPENER_250 - success: {success}")
        
        # 保存数据并触发事件
        self.pick_from_opener_250_data = {"success": success}
        self.pick_from_opener_250_event.set()
        
        return {
            "success": True,
            "message": "250ml开瓶器完成信号已接收"
        }

    def handle_pick_from_bottle_opener(self, cmd_data: Dict) -> Dict:
        """
        处理PICK_FROM_BOTTLE_OPENER命令。

        该命令用于 bottle_opener 外部通知后的继续信号，消息内容与
        PICK_FROM_BOTTLE_OPENER_250 一致，因此复用同一组等待事件和数据。
        """
        params = cmd_data.get("params", {})
        success = params.get("success", False)

        logger.info("命令处理器", f"PICK_FROM_BOTTLE_OPENER - success: {success}")

        self.pick_from_opener_250_data = {"success": success}
        self.pick_from_opener_250_event.set()

        return {
            "success": True,
            "message": "开瓶器完成信号已接收"
        }
    
    def handle_ws_scanner_notify(self, cmd_data: Dict) -> Dict:
        """
        处理 scanner 通知（可通过 WebSocket 或 HTTP 两种传输方式发送）

        同步阻塞模式：
          1. 要求必须有运行中的任务（START_WORKING 之后）
          2. 清除 scan_enter_id_event（重置等待状态）
          3. 将状态机切换为 WAITING_ID_INPUT
          4. 阻塞等待 SCAN_QRCODE_ENTER_ID 命令到来（HTTP/WS 均可触发）
          5. 收到后将 ID 录入结果一并回传给调用方

        因 command_callback 通过 run_in_executor 运行在线程池，
        此处 event.wait() 阻塞的是工作线程，不会冻结 asyncio 事件循环。

        请求示例:
            {"cmd_id": "001", "cmd_type": "scanner"}

        触发继续的命令:
            curl -X POST http://localhost:<port> -d @test_commands/SCAN_QRCODE_ENTER_ID_command.json
        """
        cmd_id = cmd_data.get("cmd_id")
        logger.info("命令处理器", f"收到 scanner 通知 (cmd_id={cmd_id})")

        # 必须有运行中的任务，与其他任务类命令保持一致
        if not self.is_robot_busy():
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                "没有运行中的任务，请先发送 START_WORKING 命令",
                cmd_id=cmd_id,
                current_status=state.get("status"),
                current_step=state.get("current_step", {}).get("description")
            )

        # 重置事件，准备等待本次触发
        self.scan_enter_id_event.clear()
        self.scan_enter_id_data = None

        # 更新状态机 → WAITING_ID_INPUT
        self.task_state_machine.update_step(
            TaskStep.WAITING_ID_INPUT,
            "scanner通知：等待发送SCAN_QRCODE_ENTER_ID"
        )

        print(f"\n>>> scanner通知已收到，状态机 → WAITING_ID_INPUT，开始等待 SCAN_QRCODE_ENTER_ID ...")
        print(f"    curl -X POST http://localhost:{HTTP_SERVER_PORT} -d @test_commands/SCAN_QRCODE_ENTER_ID_command.json\n")

        # 阻塞等待 HTTP 发来 SCAN_QRCODE_ENTER_ID（超时 150 秒）
        timeout_sec = 150
        triggered = self.scan_enter_id_event.wait(timeout=timeout_sec)

        if not triggered:
            logger.error("命令处理器", f"scanner: 等待 SCAN_QRCODE_ENTER_ID 超时（{timeout_sec}s）")
            self.task_state_machine.set_error(f"等待 SCAN_QRCODE_ENTER_ID 超时（{timeout_sec}s）")
            return make_error_response(
                ErrorCode.TASK_TIMEOUT,
                f"等待 SCAN_QRCODE_ENTER_ID 超时（{timeout_sec}s）",
                cmd_id=cmd_id
            )

        # 取出 HTTP 回传的数据
        enter_id_data = self.scan_enter_id_data or {}
        logger.info("命令处理器", f"scanner: 收到 SCAN_QRCODE_ENTER_ID 数据: {enter_id_data}")
        print(f"✓ scanner 收到 ID 录入结果: {enter_id_data}")

        # 更新状态机
        self.task_state_machine.update_step(
            TaskStep.ID_INPUT_SUCCESS,
            "ID录入成功"
        )

        return make_success_response(
            message="scanner通知已处理，SCAN_QRCODE_ENTER_ID 已收到",
            cmd_id=cmd_id,
            current_step="ID_INPUT_SUCCESS",
            enter_id_result=enter_id_data
        )

    def handle_ws_bottle_opener_notify(self, cmd_data: Dict) -> Dict:
        """
        处理 bottle_opener 通知（可通过 WebSocket 或 HTTP 两种传输方式发送）

        同步阻塞模式：
          1. 要求必须有运行中的任务（START_WORKING 之后）
          2. 清除 pick_from_opener_250_event（重置等待状态）
          3. 将状态机切换为 PUTTING_DOWN_TO_BOTTLE_OPENER（同时控制 250ml/500ml 两台）
          4. 阻塞等待 PICK_FROM_BOTTLE_OPENER 命令到来（HTTP/WS 均可触发）
          5. 收到后将结果一并回传给调用方

        因 command_callback 通过 run_in_executor 运行在线程池，
        此处 event.wait() 阻塞的是工作线程，不会冻结 asyncio 事件循环。

        请求示例:
            {"cmd_id": "001", "cmd_type": "bottle_opener"}

        触发继续的命令:
            curl -X POST http://localhost:<port> -d @test_commands/PICK_FROM_BOTTLE_OPENER_command.json
        """
        cmd_id = cmd_data.get("cmd_id")
        logger.info("命令处理器", f"收到 bottle_opener 通知 (cmd_id={cmd_id})")

        # 必须有运行中的任务，与其他任务类命令保持一致
        if not self.is_robot_busy():
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.TASK_NOT_FOUND,
                "没有运行中的任务，请先发送 START_WORKING 命令",
                cmd_id=cmd_id,
                current_status=state.get("status"),
                current_step=state.get("current_step", {}).get("description")
            )

        # 重置事件，准备等待本次触发
        self.pick_from_opener_250_event.clear()
        self.pick_from_opener_250_data = None

        # 更新状态机 → PUTTING_DOWN_TO_BOTTLE_OPENER（同时控制 250ml/500ml 两台）
        self.task_state_machine.update_step(
            TaskStep.PUTTING_DOWN_TO_BOTTLE_OPENER_SUCCESS,
            "bottle_opener通知：等待发送PICK_FROM_BOTTLE_OPENER"
        )

        print(f"\n>>> bottle_opener通知已收到，状态机 → PUTTING_DOWN_TO_BOTTLE_OPENER_SUCCESS，开始等待 PICK_FROM_BOTTLE_OPENER_command ...")
        print(f"    curl -X POST http://localhost:{HTTP_SERVER_PORT} -d @test_commands/PICK_FROM_BOTTLE_OPENER_command.json\n")

        # 阻塞等待 HTTP 发来 PICK_FROM_BOTTLE_OPENER（超时 300 秒）
        timeout_sec = 300
        triggered = self.pick_from_opener_250_event.wait(timeout=timeout_sec)

        if not triggered:
            logger.error("命令处理器", f"bottle_opener: 等待 PICK_FROM_BOTTLE_OPENER_command 超时（{timeout_sec}s）")
            self.task_state_machine.set_error(f"等待 PICK_FROM_BOTTLE_OPENER_command 超时（{timeout_sec}s）")
            return make_error_response(
                ErrorCode.TASK_TIMEOUT,
                f"等待 PICK_FROM_BOTTLE_OPENER_command 超时（{timeout_sec}s）",
                cmd_id=cmd_id
            )

        # 取出 HTTP 回传的数据
        opener_data = self.pick_from_opener_250_data or {}
        logger.info("命令处理器", f"bottle_opener: 收到 PICK_FROM_BOTTLE_OPENER_command 数据: {opener_data}")
        print(f"✓ bottle_opener 收到开瓶器完成信号: {opener_data}")

        # 更新状态机
        self.task_state_machine.update_step(
            TaskStep.BOTTLE_OPENER_OPEN_SUCCESS,
            "开瓶器开瓶成功"
        )

        return make_success_response(
            message="bottle_opener通知已处理，PICK_FROM_BOTTLE_OPENER_command 已收到",
            cmd_id=cmd_id,
            current_step="PICKING_FROM_BOTTLE_OPENER",
            opener_result=opener_data
        )

    def handle_refill_empty_bottles(self, cmd_data: Dict) -> Dict:
        """
        处理REFILL_EMPTY_BOTTLES命令
        导航到空瓶区抓取空瓶并放置到后部暂存区
        
        请求参数:
            robot_id: 机器人ID (可选，默认为 "robot_a")
            timeout: 超时时间 (可选，默认60秒)
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")
        timeout = params.get("timeout", 60.0)
        task_id = cmd_data.get("cmd_id")
        
        logger.info("命令处理器", f"REFILL_EMPTY_BOTTLES - robot_id: {robot_id}")
        
        # 获取机器人
        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(
                ErrorCode.ROBOT_NOT_CONNECTED,
                f"机器人 {robot_id} 未连接"
            )
        
        # 检查机器人电量状态
        battery_check = self.check_battery_availability(robot_id, "REFILL_EMPTY_BOTTLES")
        if battery_check["error_response"]:
            return battery_check["error_response"]
        
        # 如果需要先返回home点位
        if battery_check["need_go_home"]:
            if not self._navigate_to_home_before_task(robot_id, battery_check["home_pose"]):
                return make_error_response(
                    ErrorCode.NAVIGATION_FAILED,
                    f"机器人 {robot_id} 返回home点位失败，无法执行任务"
                )
        
        # 检查机器人是否正忙
        if self.is_robot_busy(robot_id):
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                f"机器人 {robot_id} 正忙，无法执行新任务",
                current_task_id=state.get("cmd_id"),
                current_status=state.get("status"),
                current_step=state.get("current_step", {}).get("description")
            )
        
        # 初始化任务状态
        self.task_state_machine.start_task(task_id, robot_id)
        
        # 异步执行
        thread = threading.Thread(
            target=self._execute_refill_empty_bottles_async,
            args=(task_id, robot_id, timeout),
            daemon=True
        )
        thread.start()
        
        return {
            "success": True,
            "message": "补充空瓶任务已启动",
            "task_id": task_id
        }
    
    def _execute_refill_empty_bottles_async(self, task_id: str, robot_id: str, timeout: float):
        """
        异步执行补充空瓶任务
        
        流程：
        1. 导航到空瓶区
        2. 抓取空瓶并放置到后部暂存区（循环直到无更多瓶子或暂存区满）
        """
        robot = self.robots.get(robot_id)
        if not robot:
            self.task_state_machine.set_error(f"机器人 {robot_id} 未连接")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")
            
            # 获取存储管理器
            storage_mgr = get_storage_manager()
            
            # 通过topic触发导航
            navigation_publish_result = robot.publish_topic(
                topic_name="/navigation_control",
                msg_type="std_msgs/String",
                msg_data={"data": NavigationPose.EMPTY_BOTTLE_AREA_SPLIT}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到空瓶区（分液任务点位）失败")
                logger.error("命令处理器", "导航到空瓶区（分液任务点位）失败")
                return
            
            print("✓ 已到达空瓶区")
            logger.info("命令处理器", "已到达空瓶区")
            
            # 步骤2: 抓取空瓶并放置到后部暂存区
            print("\n" + "="*70)
            print("【步骤2】抓取空瓶并放置到后部暂存区...")
            print("="*70)
            
            scan_store_result = self._scan_and_store_bottles_loop(robot, storage_mgr, StationArea.EMPTY_BOTTLE_AREA, robot_id)
            if not scan_store_result:
                # 错误已在内部设置
                return
            
            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "补充空瓶任务完成")
            self.task_state_machine.complete_task(True, "补充空瓶流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")
            print("\n" + "="*70)
            print("✅ 补充空瓶任务完成")
            print("="*70)
            
        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")
    
    def handle_bottle_get(self, cmd_data: Dict) -> Dict:
        """处理BOTTLE_GET命令 - 获取样品瓶信息"""
        params = cmd_data.get("params", {})
        bottle_id = params.get("bottle_id")
        pose_name = params.get("pose_name")
        detail_params = params.get("detail_params", True)
        
        logger.info("命令处理器", f"BOTTLE_GET - bottle_id: {bottle_id}, pose: {pose_name}")
        
        result_data = {}
        
        if bottle_id:
            # 查询指定瓶子
            if detail_params:
                result_data = self.bottle_manager.get_bottle_detail(bottle_id)
            else:
                result_data = {"bottle_id": bottle_id}
        
        elif pose_name:
            # 查询指定点位的所有瓶子
            bottles = self.bottle_manager.get_bottles_by_pose(pose_name)
            if detail_params:
                result_data = {
                    "pose_name": pose_name,
                    "bottles": [b.to_dict() for b in bottles]
                }
            else:
                result_data = {
                    "pose_name": pose_name,
                    "bottle_ids": [b.bottle_id for b in bottles]
                }
        
        else:
            # 查询所有瓶子
            all_bottles = self.bottle_manager.get_all_bottles()
            if detail_params:
                result_data = {
                    "total_count": len(all_bottles),
                    "bottles": [b.to_dict() for b in all_bottles.values()]
                }
            else:
                result_data = {
                    "total_count": len(all_bottles),
                    "bottle_ids": list(all_bottles.keys())
                }
        
        return {
            "success": True,
            "message": "查询成功",
            "data": result_data
        }
    
    def handle_get_station_counter(self, cmd_data: Dict) -> Dict:
        """
        处理GET_STATION_COUNTER命令 - 查询暂存区瓶子数量
        
        可查询的暂存区：
        - waiting_split_area: 分液台待分液区
        - split_done_250ml_area: 250ml分液完成暂存区
        - split_done_500ml_area: 500ml分液完成暂存区
        
        注意：暂存区与机器人无关，是独立的物理位置
        
        请求参数:
            area: 暂存区名称 (可选，不提供则返回所有暂存区数据)
            action: 操作类型 (可选，"reset"可重置计数器)
        """
        params = cmd_data.get("params", {})
        area = params.get("area")
        action = params.get("action")
        
        station_counter = get_station_counter()
        
        # 如果是重置操作
        if action == "reset":
            station_counter.reset(area)
            return {
                "success": True,
                "message": f"计数器已重置" + (f" (area={area})" if area else ""),
                "data": station_counter.get_all_counts()
            }
        
        # 查询操作
        if area:
            # 查询指定区域
            count = station_counter.get_count(area)
            return {
                "success": True,
                "message": "查询成功",
                "data": {
                    "area": area,
                    "count": count
                }
            }
        else:
            # 查询所有区域数据
            counts = station_counter.get_all_counts()
            return {
                "success": True,
                "message": "查询成功",
                "data": counts
            }
    
    def handle_transfer_to_chromatograph(self, cmd_data: Dict) -> Dict:
        """
        处理TRANSFER_TO_CHROMATOGRAPH命令
        从250ml分液完成暂存区拿取瓶子，运到色谱仪暂存位
        
        请求参数:
            robot_id: 机器人ID (可选，默认为 "robot_a")
        """
        params = cmd_data.get("params", {})
        robot_id = params.get("robot_id", "robot_a")
        task_id = cmd_data.get("cmd_id")
        
        logger.info("命令处理器", f"TRANSFER_TO_CHROMATOGRAPH - robot_id: {robot_id}")
        
        # 获取机器人
        robot = self.robots.get(robot_id)
        if not robot:
            return make_error_response(
                ErrorCode.ROBOT_NOT_CONNECTED,
                f"机器人 {robot_id} 未连接"
            )
        
        # 检查机器人电量状态
        battery_check = self.check_battery_availability(robot_id, "TRANSFER_TO_CHROMATOGRAPH")
        if battery_check["error_response"]:
            return battery_check["error_response"]
        
        # 如果需要先返回home点位
        if battery_check["need_go_home"]:
            if not self._navigate_to_home_before_task(robot_id, battery_check["home_pose"]):
                return make_error_response(
                    ErrorCode.NAVIGATION_FAILED,
                    f"机器人 {robot_id} 返回home点位失败，无法执行任务"
                )
        
        # 检查机器人是否正忙
        if self.is_robot_busy(robot_id):
            state = self.task_state_machine.get_state()
            return make_error_response(
                ErrorCode.ROBOT_BUSY,
                f"机器人 {robot_id} 正忙，无法执行新任务",
                current_task_id=state.get("cmd_id"),
                current_status=state.get("status")
            )
        # 订阅导航状态topic
        '''robot.subscribe_topic(
                topic_name=ROSTopic.NAVIGATION_STATUS,
                msg_type=ROSTopicMessageType.NAVIGATION_STATUS,
                throttle_rate=0,
                queue_length=1
            )
        navigation_publish_result = robot.publish_topic(
                topic_name="/navigation_control",
                msg_type="std_msgs/String",
                msg_data={"data": NavigationPose.SPLIT_DONE_250ML_AREA_SPLIT_BACK}
            )
        if not navigation_publish_result:
            self.task_state_machine.set_error("发布导航命令失败")
            logger.error("命令处理器", "发布导航命令失败")
            return
        # 等待导航完成
        waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
        if not waiting_navigation_status_result:
            self.task_state_machine.set_error("导航到待分液区（分液任务点位）(前进进入)失败")
            logger.error("命令处理器", "导航到待分液区（分液任务点位）失败")
            return'''
        '''battery_state = robot.get_topic_message(ROSTopic.BATTERY_STATE, ROSTopicMessageType.BATTERY_STATE, sleep_time=1)
        print(f"battery_state: {battery_state}")'''
        '''charging_status = None
        while charging_status is None:
            print("waiting for charging status...")
            charging_status = robot.get_topic_message(ROSTopic.CHARGING_STATUS_TOPIC, ROSTopicMessageType.CHARGING_STATUS)
        print(f"charging_status: {charging_status}")'''


        # 检查250ml分液完成暂存区是否有瓶子
        '''# 在250ml分液完成区增加一个瓶子
        station_counter = get_station_counter()
        station_counter.increment(StationCounter.SPLIT_DONE_250ML_AREA)'''
        '''station_counter = get_station_counter()
        available_count = station_counter.get_count(StationCounter.SPLIT_DONE_250ML_AREA)
        if available_count == 0:
            return make_error_response(
                ErrorCode.RESOURCE_INSUFFICIENT,
                f"250ml分液完成暂存区瓶子不足，当前: {available_count}"
            )'''

        # 启动任务
        self.task_state_machine.start_task(task_id, robot_id)
        
        # 启动异步执行线程
        thread = threading.Thread(
            target=self._execute_transfer_to_chromatograph_async_action,
            args=(task_id, robot_id),
            daemon=True
        )
        thread.start()
        
        return make_success_response(
            "TRANSFER_TO_CHROMATOGRAPH任务已启动",
            robot_id=robot_id,
            note="使用 GET_TASK_STATE 命令查询任务状态"
        )

    
    def _execute_transfer_to_chromatograph_async_action(self, task_id: str, robot_id: str = "robot_a"):
        """
        异步执行TRANSFER_TO_CHROMATOGRAPH任务（后台线程）
        
        参数:
            task_id: 任务ID
            robot_id: 执行任务的机器人ID
        """
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")

            # 导航到250ml分液完成暂存区（转运任务点位）
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_250ML_SPLIT_DONE_AREA_TRANSFER, "导航到250ml分液完成暂存区（转运任务点位）")
            goal = build_navigation_goal(
                NavigationPose.SPLIT_DONE_250ML_AREA_TRANSFER,  # 目标点
                distance_tolerance = 0.06,
                heading_tolerance = 0.08,   # 弧度
                translation_enable = True,
                translation_heading = 0.0
            )
            def on_feedback(fb):
                print(f"状态: {fb.state.name}")
            result = send_navigation_action(robot, goal, feedback_callback=on_feedback)
            if not result:
                self.task_state_machine.set_error("导航到250ml分液完成暂存区（转运任务点位）失败")
                print("导航到250ml分液完成暂存区（转运任务点位）失败")
                logger.error("命令处理器", "导航到250ml分液完成暂存区（转运任务点位）失败")
                return

            print(f"✓ 步骤1完成: 导航到250ml分液完成暂存区（转运任务点位）")

            # 单个导航点位操作动作：从250ml分液完成暂存区抓取瓶子
            self.task_state_machine.update_step(TaskStep.ACTION_SCAN_AND_STORE_BOTTLES_SPLIT_DONE_250ML_AREA, "从250ml分液完成暂存区抓取瓶子")
            scan_store_result = robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                "SPLIT_DONE_250ML_AREA_TRANSFER"
            )
            if not scan_store_result:
                self.task_state_machine.set_error("从250ml分液完成暂存区抓取瓶子失败")
                logger.error("命令处理器", "从250ml分液完成暂存区抓取瓶子失败")
                return
            print(f"✓ 步骤2完成: 从250ml分液完成暂存区抓取瓶子")

            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "转运任务完成")
            self.task_state_machine.complete_task(True, "转运流程结束")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")

        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")

    def _execute_transfer_to_chromatograph_async(self, task_id: str, robot_id: str):
        """
        异步执行TRANSFER_TO_CHROMATOGRAPH任务
        
        流程：
        1. 导航到250ml分液完成暂存区
        2. 抓取瓶子
        3. 导航到色谱仪
        4. 放置瓶子到色谱仪暂存位
        """
        robot = self.get_robot(robot_id)
        if robot is None:
            self.task_state_machine.set_error(f"机器人 {robot_id} 不存在")
            return
        
        try:
            logger.info("命令处理器", f"任务 {task_id} 开始执行 (机器人: {robot_id})")
            # 初始化后部暂存区
            storage_mgr = get_storage_manager()
                
            # 步骤1: 导航到250ml分液完成暂存区（转运任务点位）
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_250ML_SPLIT_DONE_AREA_TRANSFER, "导航到250ml分液完成暂存区（转运任务点位）")
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.SPLIT_DONE_250ML_AREA_TRANSFER}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到250ml分液完成暂存区（转运任务点位）失败")
                logger.error("命令处理器", "导航到250ml分液完成暂存区（转运任务点位）失败")
                return
            print(f"✓ 步骤1完成: 导航到250ml分液完成暂存区（转运任务点位）")
            
            # 步骤2: 大步骤：从250ml分液完成暂存区抓取瓶子
            bottle_msg = {
                "bottle_id": "bottle_to_chromatograph",
                "object_type": "glass_bottle_250",
                "task": "transfer_to_chromatograph"
            }
            self._store_bottles_loop(robot, storage_mgr, robot_id, StationCounter.SPLIT_DONE_250ML_AREA, bottle_msg)
            
            '''# 步骤3: 导航到色谱仪
            self.task_state_machine.update_step(TaskStep.NAVIGATING_TO_CHROMATOGRAPH, "导航到色谱仪")
            navigation_publish_result = robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": NavigationPose.CHROMATOGRAPH}
            )
            if not navigation_publish_result:
                self.task_state_machine.set_error("发布导航命令失败")
                logger.error("命令处理器", "发布导航命令失败")
                return
            
            # 等待导航完成
            waiting_navigation_status_result = self._wait_for_navigation_finished(robot)
            if not waiting_navigation_status_result:
                self.task_state_machine.set_error("导航到色谱仪失败")
                logger.error("命令处理器", "导航到色谱仪失败")
                return
            print(f"✓ 步骤3完成: 导航到色谱仪")
            
            # 步骤4: 把后部暂存区瓶子放到色谱仪暂存位
            # 遍历所有已分液的250ml瓶子
            all_bottles_in_storage = []
            for bottle_type, slots in storage_mgr.get_storage(robot_id).items():
                if bottle_type == "glass_bottle_250":
                    for slot_index, slot in enumerate(slots):
                        bottle_info = storage_mgr.get_bottle_info(robot_id, bottle_type, slot_index)
                        # 检查 bottle_info 是否存在且状态为已分液
                        if bottle_info and bottle_info.get("bottle_state") == BottleState.SPLIT_DONE:
                            all_bottles_in_storage.append(bottle_info)
            self._put_bottles_two_hands_loop(robot, back_temp_storage, "chromatograph", all_bottles_in_storage, robot_id)'''
            
            # 任务完成
            self.task_state_machine.update_step(TaskStep.COMPLETED, "转移到色谱仪任务完成")
            #self.task_state_machine.complete_task(True, f"成功转移 {len(all_bottles_in_storage)} 个瓶子到色谱仪")
            logger.info("命令处理器", f"任务 {task_id} 执行完成")
            
        except Exception as e:
            logger.exception_occurred("命令处理器", f"任务 {task_id} 执行异常", e)
            self.task_state_machine.set_error(f"执行异常: {str(e)}")
    
    # ==================== 系统控制命令（TJSH 覆盖）====================

    def handle_reset_system(self, cmd_data: Dict) -> Dict:
        """
        TJSH 特有重置逻辑：先清理本项目特有事件，再执行通用重置。

        TJSH 额外步骤（在 super() 之前）：
          - 清除 scan_enter_id_event / pick_from_opener_* 事件及其数据
        """
        if self._const_flow is not None:
            try:
                self._const_flow.shutdown()
            except Exception:
                pass
        # TJSH 特有事件清理
        self.scan_enter_id_event.clear()
        self.pick_from_opener_500_event.clear()
        self.pick_from_opener_250_event.clear()
        self.scan_enter_id_data = None
        self.pick_from_opener_500_data = None
        self.pick_from_opener_250_data = None
        # 通用重置（断开机器人、重置状态机、重载配置等）
        return super().handle_reset_system(cmd_data)
    
    def _execute_pickup_sequence(self, bottle: Any) -> bool:
        """执行拾取序列（内部辅助方法）"""
        # 抓取
        grab_result = self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "grab_object",
            extra_params={
                "strawberry": {
                    "type": bottle.object_type,
                    "target_pose": bottle.target_pose,
                    "hand": bottle.hand
                }
            }
        )
        
        if not grab_result:
            return False
        
        # 转腰
        self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "turn_waist",
            extra_params={"angle": "180", "obstacle_avoidance": True}
        )
        
        # 放置到后部平台
        back_pose = f"back_temp_{bottle.object_type.split('_')[-1]}_001"
        put_result = self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "put_object",
            extra_params={
                "strawberry": {
                    "type": bottle.object_type,
                    "target_pose": back_pose,
                    "hand": bottle.hand,
                    "safe_pose": "preset"
                }
            }
        )
        
        # 转回
        self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "turn_waist",
            extra_params={"angle": "0", "obstacle_avoidance": True}
        )
        
        if put_result:
            self.bottle_manager.place_bottle(bottle.bottle_id, back_pose)
        
        return put_result
    
    def _execute_putdown_sequence(self, bottle: Any, release_pose: str) -> bool:
        """执行放下序列（内部辅助方法）"""
        # 转腰到背面
        self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "turn_waist",
            extra_params={"angle": "180", "obstacle_avoidance": True}
        )
        
        # 从后部平台抓取
        grab_result = self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "grab_object",
            extra_params={
                "strawberry": {
                    "type": bottle.object_type,
                    "target_pose": bottle.location,
                    "hand": bottle.hand
                }
            }
        )
        
        if not grab_result:
            return False
        
        # 转回正面
        self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "turn_waist",
            extra_params={"angle": "0", "obstacle_avoidance": True}
        )
        
        # 放置到目标点位
        put_result = self.robot_a.send_service_request(
            self.robot_a.get_robot_service(),
            "put_object",
            extra_params={
                "strawberry": {
                    "type": bottle.object_type,
                    "target_pose": release_pose,
                    "hand": bottle.hand,
                    "safe_pose": "preset"
                }
            }
        )
        
        if put_result:
            self.bottle_manager.remove_bottle_from_pose(bottle.bottle_id, bottle.location)
            self.bottle_manager.place_bottle(bottle.bottle_id, release_pose)
        
        return put_result

    def _scan_and_store_bottles_loop(self, robot, storage_mgr, target_area: str, robot_id: str) -> bool:
        """
        扫码并放置到后部暂存区流程（循环处理所有瓶子）
        
        该方法封装了完整的瓶子扫码和存储流程，包括：
        1. CV检测瓶子
        2. 抓取瓶子
        3. 放置到扫描转盘
        4. 等待ID录入
        5. 从扫描转盘取回瓶子
        6. 放置到后部暂存区
        7. 转回正面
        
        参数:
            robot: 机器人控制器实例
            storage_mgr: 存储管理器实例
            robot_id: 机器人ID（用于区分不同机器人的暂存区）
            from_waiting_split_area: 是否从待分液区抓取（如果是则减少待分液区计数）
        
        返回:
            bool: 流程是否成功完成（True=成功/无更多瓶子，False=出错）
        """
        back_temp_storage = storage_mgr.get_storage(robot_id)
        station_counter = get_station_counter()
        
        while True:
            # 检查暂存区是否全部已满
            if check_storage_is_full(back_temp_storage):
                self.task_state_machine.set_error("暂存区已满")
                logger.error("命令处理器", "暂存区已满")
                return
            
            # 瓶子类型依照瓶子区域决定
            if target_area == StationArea.WAITING_SPLIT_AREA:
                object_type = "glass_bottle_500"
            elif target_area == StationArea.EMPTY_BOTTLE_AREA:
                object_type = "glass_bottle_250"
            elif target_area == StationArea.SPLIT_DONE_500ML_AREA:
                object_type = "glass_bottle_500"
            elif target_area == StationArea.SPLIT_DONE_250ML_AREA:
                object_type = "glass_bottle_250"
            else:
                self.task_state_machine.set_error("未知目标区域")
                logger.error("命令处理器", "未知目标区域")
                return False
            
            # 检查对应类型暂存区是否已满
            empty_storage_index = storage_mgr.get_empty_slot_index(robot_id, object_type)
            if empty_storage_index is None:
                self.task_state_machine.set_error(f"{object_type}暂存区已满")
                logger.error("命令处理器", f"{object_type}暂存区已满")
                return
            
            # 视觉检测瓶子
            #input("cv detecting bottle...")
            self.task_state_machine.update_step(TaskStep.CV_DETECTING, "视觉检测瓶子")
            cv_detect_result = robot.send_service_request(
                robot.get_robot_service(),
                "cv_detect",
                extra_params={
                    "waist": "turing"
                }
            )
            if not cv_detect_result:
                self.task_state_machine.set_error("视觉检测瓶子失败")
                logger.error("命令处理器", "视觉检测瓶子失败")
                return
            else:
                self.task_state_machine.update_step(TaskStep.CV_DETECTING_SUCCESS, "视觉检测瓶子成功")
                logger.info("命令处理器", "视觉检测瓶子成功")
            
            # 步骤: 抓取瓶子
            #input("pick bottle from scan table...")
            self.task_state_machine.update_step(TaskStep.GRABBING_BOTTLE, f"抓取瓶子 ({object_type})")
            grab_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_object",
                extra_params={
                    "type": object_type,
                }
            )
            
            if not grab_result:
                self.task_state_machine.set_error("抓取瓶子失败")
                logger.error("命令处理器", "抓取失败")
                return False
            
            
            # 步骤: 把瓶子放在旋转平台上
            #input("putting bottle to scan table...")
            self.task_state_machine.update_step(TaskStep.PUT_TO_SCAN_MACHINE, "放置到扫描转盘")
            scan_result = robot.send_service_request(
                robot.get_robot_service(),
                "scan",
                extra_params={
                    "type": object_type,
                }
            )
            if not scan_result:
                self.task_state_machine.set_error("放置到扫描转盘失败")
                logger.error("命令处理器", "放置到扫描转盘失败")
                return False
            
            # 步骤: 等待ID录入
            self.scan_enter_id_event.clear()
            self.scan_enter_id_data = None
            
            print("\n" + "="*70)
            print("【等待ID录入】")
            print("请使用以下命令发送:")
            print(f"curl -X POST http://localhost:{HTTP_SERVER_PORT} -d @test_commands/SCAN_QRCODE_ENTER_ID_command.json")
            print("="*70 + "\n")
            
            logger.info("命令处理器", "等待SCAN_QRCODE_ENTER_ID消息...")
            self.task_state_machine.update_step(TaskStep.WAITING_ID_INPUT, "等待ID录入")
            
            if self.scan_enter_id_event.wait(timeout=150):
                if self.scan_enter_id_data:
                    scan_qrcode_enter_id_result = self.scan_enter_id_data.get("success")
                    bottle_id = self.scan_enter_id_data.get("bottle_id")
                    object_type_scan = self.scan_enter_id_data.get("type")
                    task = self.scan_enter_id_data.get("task") # 获取到任务是，能否解析这个任务，然后安排机器人接下来做的事情，比如“这个瓶子需要先去分液，然后再去色谱仪检测，等待检测结果出来后会有新的指令”，这个任务能被解析成一系列任务代码。
                    self.task_state_machine.update_step(TaskStep.ID_INPUT_SUCCESS, "ID录入成功")
                    logger.info("命令处理器", f"接收到瓶子信息: {bottle_id}, 类型: {object_type_scan}, 任务: {task}")
                    print(f"✓ 已接收到瓶子ID: {bottle_id}")
                else:
                    self.task_state_machine.set_error("接收数据异常")
                    logger.error("命令处理器", "接收到事件但数据为空")
                    return False
            else:
                self.task_state_machine.set_error("等待扫码ID录入超时")
                logger.error("命令处理器", "等待SCAN_QRCODE_ENTER_ID消息超时")
                return False
            
            if not scan_qrcode_enter_id_result:
                self.task_state_machine.set_error("扫描二维码ID录入失败")
                logger.error("命令处理器", "扫描二维码ID录入失败")
                return False
            
            print("✓ ID录入完成")

            # 机器人识别瓶子类型出错，需要更新暂存区位置
            if object_type != object_type_scan:
                object_type = object_type_scan
                empty_storage_index = storage_mgr.get_empty_slot_index(robot_id, object_type)
                if empty_storage_index is None:
                    self.task_state_machine.set_error(f"{object_type}暂存区已满")
                    logger.error("命令处理器", f"{object_type}暂存区已满")
                    return False

            # 步骤: 把瓶子从旋转平台拿回来
            #input("picking bottle from scan table...")
            scan_back_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_scan_back",
                extra_params={
                    "type": object_type,
                }

            )
            if not scan_back_result:
                self.task_state_machine.set_error("从扫描转盘取回失败")
                logger.error("命令处理器", "从扫描转盘取回失败")
                return False
            
            # 步骤: 放置到后部暂存区
            if object_type == "glass_bottle_250":
                target_pose = f"back_temp_250_" + str(empty_storage_index)
            elif object_type == "glass_bottle_500":
                target_pose = f"back_temp_500_" + str(empty_storage_index)
            else:
                print(f"未知瓶子类型: {object_type}")
                logger.error("命令处理器", f"未知瓶子类型: {object_type}")
                return False
            self.task_state_machine.update_step(TaskStep.PUTTING_TO_BACK, f"放置到后部暂存区 slot_{empty_storage_index}")
            #input("putting bottle to back storage...")
            put_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_object_back",
                extra_params={
                    "type": object_type,
                    "target_pose": target_pose
                }
            )
            
            if not put_result:
                self.task_state_machine.set_error("放置到后部暂存区失败")
                logger.error("命令处理器", "放置失败")
                return False
            else:
                # 更新暂存区状态
                storage_mgr.update_slot(robot_id, object_type, empty_storage_index, bottle_id)
                storage_mgr.set_bottle_state(robot_id, object_type, empty_storage_index, BottleState.NOT_SPLIT)
                # 抓取完成，目标计数区域减少一瓶
                station_counter.decrement(target_area)
                print(f"station_counter: {station_counter}")
                # 记录已扫描的瓶子
                self.task_state_machine.add_scanned_bottle(bottle_id, object_type, empty_storage_index, task)
                logger.info("命令处理器", f"瓶子 {bottle_id} 已放置到 {object_type}[{empty_storage_index}]")
            
            # 步骤: 转回正面
            #input("turning back front...")
            self.task_state_machine.update_step(TaskStep.TURNING_BACK_FRONT, "转回正面")
            turn_waist_result = robot.send_service_request(
                robot.get_robot_service(),
                "back_to_front",
                extra_params={
                    "area": "waiting_split_area"
                }
            )
            if not turn_waist_result:
                self.task_state_machine.set_error("转回正面失败")
                logger.error("命令处理器", "转腰失败")
                return False
            
            # 继续下一个瓶子（循环）

    def _put_bottles_two_hands_loop(self, robot, storage_mgr, target_area: str, all_bottles_in_storage: list, robot_id: str) -> bool:
        """
        双手抓放瓶子到分液台流程（循环处理所有瓶子）
        
        该方法封装了从后部暂存区取瓶子并放置到分液台的完整流程：
        1. 转腰到后部暂存区
        2. 右手抓起瓶子
        3. 左手抓起瓶子（如果有）
        4. 转腰到正面
        5. 右手放下瓶子
        6. 左手放下瓶子（如果有）
        
        采用双手协同操作，每次最多处理2个瓶子，提高效率约40%。
        
        参数:
            robot: 机器人控制器实例
            storage_mgr: 存储管理器实例
            robot_id: 机器人ID（用于区分不同机器人的暂存区）
            all_bottles_in_storage: 需要放置的瓶子列表，每个元素包含 bottle_id, slot_index, bottle_type
            target_area: 目标计数区域
        
        返回:
            bool: 流程是否成功完成（True=成功，False=出错）
        """
        total_bottles = len(all_bottles_in_storage)
        if total_bottles == 0:
            logger.info("命令处理器", "暂存区没有瓶子需要放置")
            return True
        
        visual_pose_counter = 0  # 桌面放置位置计数器
        station_counter = get_station_counter()
        
        # 按对处理瓶子（每次最多2个）
        i = 0
        while i < total_bottles:
            # 获取右手瓶子信息（必定有）
            right_bottle = all_bottles_in_storage[i]
            right_bottle_id = right_bottle["bottle_id"]
            right_slot_index = right_bottle["slot_index"]
            right_bottle_type = right_bottle["bottle_type"]
            
            if right_bottle_type == "glass_bottle_250":
                right_target_pose = "back_temp_250_" + str(right_slot_index)
            elif right_bottle_type == "glass_bottle_500":
                right_target_pose = "back_temp_500_" + str(right_slot_index)
            else:
                print(f"未知瓶子类型: {right_bottle_type}")
                logger.error("命令处理器", f"未知瓶子类型: {right_bottle_type}")
                return False
            
            # 检查是否有左手瓶子（下一个）
            has_left_bottle = (i + 1) < total_bottles
            left_bottle_id = None
            left_slot_index = None
            left_bottle_type = None
            left_target_pose = None
            
            if has_left_bottle:
                left_bottle = all_bottles_in_storage[i + 1]
                left_bottle_id = left_bottle["bottle_id"]
                left_slot_index = left_bottle["slot_index"]
                left_bottle_type = left_bottle["bottle_type"]
                
                if left_bottle_type == "glass_bottle_250":
                    left_target_pose = "back_temp_250_" + str(left_slot_index)
                elif left_bottle_type == "glass_bottle_500":
                    left_target_pose = "back_temp_500_" + str(left_slot_index)
                else:
                    print(f"未知瓶子类型: {left_bottle_type}")
                    logger.error("命令处理器", f"未知瓶子类型: {left_bottle_type}")
                    return False
            
            # 打印处理信息
            if has_left_bottle:
                print(f"\n处理第 {i+1}-{i+2}/{total_bottles} 个瓶子 (双手):")
                print(f"  右手: {right_bottle_id} ({right_bottle_type}, 槽位{right_slot_index})")
                print(f"  左手: {left_bottle_id} ({left_bottle_type}, 槽位{left_slot_index})")
            else:
                print(f"\n处理第 {i+1}/{total_bottles} 个瓶子 (单手):")
                print(f"  右手: {right_bottle_id} ({right_bottle_type}, 槽位{right_slot_index})")
            
            self.task_state_machine.update_step(
                TaskStep.PUTTING_DOWN_BOTTLE, 
                f"放下瓶子 {i+1}{'-'+str(i+2) if has_left_bottle else ''}/{total_bottles}"
            )
            
            # === 步骤1: 转腰到后部暂存区 ===
            turn_waist_result = robot.send_service_request(
                robot.get_robot_service(),
                "turning_waist",
                extra_params={
                    "area": "waiting_split_area_back"
                }
            )
            if not turn_waist_result:
                self.task_state_machine.set_error("转腰到后部暂存区失败")
                logger.error("命令处理器", "转腰到后部暂存区失败")
                return False
            
            # === 步骤2: 右手抓起瓶子 ===
            pick_right_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_from_back_temp",
                extra_params={
                    "right_hand": {
                        "type": right_bottle_type,
                        "target_pose": right_target_pose
                    }
                }
            )
            if not pick_right_result:
                self.task_state_machine.set_error(f"右手抓取瓶子 {right_bottle_id} 失败")
                logger.error("命令处理器", f"右手抓取瓶子 {right_bottle_id} 失败")
                return False
            print(f"  ✓ 右手抓取 {right_bottle_id} 完成")
            
            # === 步骤3: 左手抓起瓶子（如果有） ===
            if has_left_bottle:
                pick_left_result = robot.send_service_request(
                    robot.get_robot_service(),
                    "pick_from_back_temp",
                    extra_params={
                        "left_hand": {
                            "type": left_bottle_type,
                            "target_pose": left_target_pose
                        }
                    }
                )
                if not pick_left_result:
                    self.task_state_machine.set_error(f"左手抓取瓶子 {left_bottle_id} 失败")
                    logger.error("命令处理器", f"左手抓取瓶子 {left_bottle_id} 失败")
                    return False
                print(f"  ✓ 左手抓取 {left_bottle_id} 完成")
            
            # === 步骤4: 转腰到正面 ===
            turn_waist_result = robot.send_service_request(
                robot.get_robot_service(),
                "turning_waist",
                extra_params={
                    "area": "waiting_split_area_front"
                }
            )
            if not turn_waist_result:
                self.task_state_machine.set_error("转腰到正面失败")
                logger.error("命令处理器", "转腰到正面失败")
                return False
            
            # === 步骤5: 右手放下瓶子 ===
            put_right_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_down_split_table",
                extra_params={
                    "right_hand": {
                        "type": right_bottle_type,
                        "area": "waiting_split_area",
                        "target_pose": "visual_pose_" + str(visual_pose_counter % 4)
                    }
                }
            )
            if not put_right_result:
                self.task_state_machine.set_error(f"右手放下瓶子 {right_bottle_id} 失败")
                logger.error("命令处理器", f"右手放下瓶子 {right_bottle_id} 失败")
                return False
            
            # 更新右手瓶子的暂存区状态
            storage_mgr.update_slot(robot_id, right_bottle_type, right_slot_index, 0)
            station_counter.increment(StationCounter.WAITING_SPLIT_AREA)
            visual_pose_counter += 1
            print(f"  ✓ 右手放下 {right_bottle_id} 完成")
            logger.info("命令处理器", f"瓶子 {right_bottle_id} 放置完成")
            
            # === 步骤6: 左手放下瓶子（如果有） ===
            if has_left_bottle:
                put_left_result = robot.send_service_request(
                    robot.get_robot_service(),
                    "put_down_split_table",
                    extra_params={
                        "left_hand": {
                            "type": left_bottle_type,
                            "area": "waiting_split_area",
                            "target_pose": "visual_pose_" + str(visual_pose_counter % 4)
                        }
                    }
                )
                if not put_left_result:
                    self.task_state_machine.set_error(f"左手放下瓶子 {left_bottle_id} 失败")
                    logger.error("命令处理器", f"左手放下瓶子 {left_bottle_id} 失败")
                    return False
                
                # 更新左手瓶子的暂存区状态
                storage_mgr.update_slot(robot_id, left_bottle_type, left_slot_index, 0)
                # 分液台待分液区增加一瓶
                # 如果target_area值在StationCounter中存在，则增加计数
                if target_area in StationCounter.__dict__:
                    station_counter = get_station_counter()
                    station_counter.increment(target_area)
                visual_pose_counter += 1
                print(f"  ✓ 左手放下 {left_bottle_id} 完成")
                logger.info("命令处理器", f"瓶子 {left_bottle_id} 放置完成")
            
            # 移动到下一对瓶子
            i += 2 if has_left_bottle else 1
        
        print(f"\n✓ 所有瓶子放置完成，共 {total_bottles} 个")
        logger.info("命令处理器", f"所有瓶子放置完成，共 {total_bottles} 个")
        return True

    def _scan_and_store_bottles_loop_press_button(self, robot, storage_mgr, target_area: str, robot_id: str) -> bool:
        """
        扫码并放置到后部暂存区流程（循环处理所有瓶子）
        
        该方法封装了完整的瓶子扫码和存储流程，包括：
        1. CV检测瓶子
        2. 抓取瓶子
        3. 放置到扫描转盘
        4. 按下按钮并等待ID录入
        5. 从扫描转盘取回瓶子
        6. 放置到后部暂存区
        7. 转回正面
        
        参数:
            robot: 机器人控制器实例
            storage_mgr: 存储管理器实例
            robot_id: 机器人ID（用于区分不同机器人的暂存区）
        
        返回:
            bool: 流程是否成功完成（True=成功/无更多瓶子，False=出错）
        """
        back_temp_storage = storage_mgr.get_storage(robot_id)
        station_counter = get_station_counter()
        while True:
            # 检查暂存区是否全部已满
            if check_storage_is_full(back_temp_storage):
                self.task_state_machine.set_error("暂存区已满")
                logger.error("命令处理器", "暂存区已满")
                return
            #input("CV_detect")
            # 步骤: CV检测
            self.task_state_machine.update_step(TaskStep.CV_DETECTING, "视觉检测瓶子")
            cv_detect_result = robot.send_service_request(
                robot.get_robot_service(),
                "cv_detect",
                extra_params={
                    "waist": "not_turing" # 不转腰
                }
            )
            # 新协议：object_pose / object_type 从 values.return_params (JSON 字符串) 中解析
            object_pose, object_type = _parse_cv_detect_return_params(robot.last_service_return_params)
            
            if not cv_detect_result:
                logger.info("命令处理器", "检测不到更多瓶子，扫码任务完成")
                self.task_state_machine.update_step(TaskStep.CV_DETECTING_EMPTY, "视觉检测瓶子已抓完")
                return  # 没有更多瓶子，成功完成
            else:
                self.task_state_machine.update_step(TaskStep.CV_DETECTING_SUCCESS, "视觉检测瓶子成功")
                logger.info("命令处理器", "视觉检测瓶子成功")
            
            # 检查对应类型暂存区是否已满
            empty_storage_index = storage_mgr.get_empty_slot_index(robot_id, object_type)
            if empty_storage_index is None:
                self.task_state_machine.set_error(f"{object_type}暂存区已满")
                logger.error("命令处理器", f"{object_type}暂存区已满")
                return
            #input("grab_object_scan_table")
            # 步骤: 抓取瓶子
            self.task_state_machine.update_step(TaskStep.GRABBING_BOTTLE, f"抓取瓶子 ({object_type})")
            grab_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_object",
                extra_params={
                    "type": "glass_bottle_500"
                }
            )
            
            if not grab_result:
                self.task_state_machine.set_error("抓取瓶子失败")
                logger.error("命令处理器", "抓取失败")
                return False
            #input("scan")
            # 步骤: 把瓶子放在旋转平台上
            self.task_state_machine.update_step(TaskStep.PUT_TO_SCAN_MACHINE, "放置到扫描转盘")
            scan_result = robot.send_service_request(
                robot.get_robot_service(),
                "scan",
                extra_params={
                    "type": "glass_bottle_500"
                }
            )
            if not scan_result:
                self.task_state_machine.set_error("放置到扫描转盘失败")
                logger.error("命令处理器", "放置到扫描转盘失败")
                return False
            
            # 步骤: 按下按钮让平台旋转（异步）+ 等待ID录入（并行执行）
            self.scan_enter_id_event.clear()
            self.scan_enter_id_data = None
            # 储存按钮变量结果
            press_button_result_holder = {"result": None, "completed": False}
                
            def press_button_async():
                """异步执行按下按钮动作"""
                try:
                    logger.info("命令处理器", "开始按下按钮让平台旋转...")
                    #input("press_button")
                    result = self.robot_a.send_service_request(
                        "/get_strawberry_service",
                        task="press_button"
                    )
                    press_button_result_holder["result"] = result
                    press_button_result_holder["completed"] = True
                    if result:
                        logger.info("命令处理器", "按下按钮成功，平台开始旋转")
                        print("✓ 按下按钮成功，平台开始旋转")
                    else:
                        logger.error("命令处理器", "按下按钮失败")
                        print("✗ 按下按钮失败")
                except Exception as e:
                    logger.exception_occurred("命令处理器", "按下按钮异常", e)
                    press_button_result_holder["result"] = False
                    press_button_result_holder["completed"] = True
            
            # 启动按钮线程
            press_button_thread = threading.Thread(target=press_button_async, daemon=True)
            press_button_thread.start()
            logger.info("命令处理器", "按下按钮线程已启动，同时开始等待ID录入")

            print("\n" + "="*70)
            print("【并行执行中】")
            print("  1. 平台旋转中...")
            print("  2. 等待HTTP发送SCAN_QRCODE_ENTER_ID消息...")
            print("请使用以下命令发送:")
            print(f"curl -X POST http://localhost:{HTTP_SERVER_PORT} -d @test_commands/SCAN_QRCODE_ENTER_ID_command.json")
            print("="*70 + "\n")
            
            # 等待ID录入事件（与按钮动作并行）
            logger.info("命令处理器", "等待SCAN_QRCODE_ENTER_ID消息...")
            self.task_state_machine.update_step(TaskStep.WAITING_ID_INPUT, "按下按钮+等待ID录入")
            
            if self.scan_enter_id_event.wait(timeout=150):
                if self.scan_enter_id_data:
                    scan_qrcode_enter_id_result = self.scan_enter_id_data.get("success")
                    bottle_id = self.scan_enter_id_data.get("bottle_id")
                    object_type_scan = self.scan_enter_id_data.get("type")
                    task = self.scan_enter_id_data.get("task")
                    self.task_state_machine.update_step(TaskStep.ID_INPUT_SUCCESS, "按下按钮+ID录入成功")
                    logger.info("命令处理器", f"接收到瓶子信息: {bottle_id}, 类型: {object_type_scan}, 任务: {task}")
                    print(f"✓ 已接收到瓶子ID: {bottle_id}")
                else:
                    self.task_state_machine.set_error("接收数据异常")
                    logger.error("命令处理器", "接收到事件但数据为空")
                    return
            else:
                self.task_state_machine.set_error("等待扫码ID录入超时")
                logger.error("命令处理器", "等待SCAN_QRCODE_ENTER_ID消息超时")
                return
            
            # 等待按钮线程完成（如果还没完成的话）
            if press_button_thread.is_alive():
                logger.info("命令处理器", "ID已录入，等待按钮动作完成...")
                print("等待平台旋转完成...")
                press_button_thread.join(timeout=60)  # 最多等60秒
            
            # 检查按钮结果
            if not press_button_result_holder["completed"]:
                self.task_state_machine.set_error("按压按钮超时")
                logger.error("命令处理器", "按压按钮超时")
                return
                
            if not press_button_result_holder["result"]:
                self.task_state_machine.set_error("按压按钮失败")
                logger.error("命令处理器", "按压按钮失败")
                return
            
            if not scan_qrcode_enter_id_result:
                self.task_state_machine.set_error("扫描二维码ID录入失败")
                logger.error("命令处理器", "扫描二维码ID录入失败")
                bottle_id = "unknown"
                object_type_scan = "unknown"
                task = "unknown"
                return
            
            print("✓ 按钮动作和ID录入都已完成")

            # 机器人识别瓶子类型出错，需要更新暂存区位置
            if object_type == None:
                object_type = object_type_scan
            if object_type != object_type_scan:
                object_type = object_type_scan
                empty_storage_index = storage_mgr.get_empty_slot_index(robot_id, object_type)
                if empty_storage_index is None:
                    self.task_state_machine.set_error(f"{object_type}识别结果和指定结果不一样")
                    logger.error("命令处理器", f"{object_type}识别结果和指定结果不一样")
                    return False
            
            # 步骤：释放按钮
            release_button_result = robot.send_service_request(
                robot.get_robot_service(),
                "release_button"
            )
            if not release_button_result:
                self.task_state_machine.set_error("释放按钮失败")
                logger.error("命令处理器", "释放按钮失败")
                return False
            
            # 步骤: 把瓶子从旋转平台拿回来
            scan_back_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_scan_back",
                extra_params={
                    "type": object_type,
                }
            )
            if not scan_back_result:
                self.task_state_machine.set_error("从扫描转盘取回失败")
                logger.error("命令处理器", "从扫描转盘取回失败")
                return False
            
            # 步骤: 放置到后部暂存区
            if object_type == "glass_bottle_250":
                target_pose = f"back_temp_250_" + str(empty_storage_index)
            elif object_type == "glass_bottle_500":
                target_pose = f"back_temp_500_" + str(empty_storage_index)
            else:
                print(f"未知瓶子类型: {object_type}")
                logger.error("命令处理器", f"未知瓶子类型: {object_type}")
                return False
            self.task_state_machine.update_step(TaskStep.PUTTING_TO_BACK, f"放置到后部暂存区 slot_{empty_storage_index}")
            #input("put_object_back")
            put_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_object_back",
                extra_params={
                    "type": object_type,
                    "target_pose": target_pose
                }
            )
            
            if not put_result:
                self.task_state_machine.set_error("放置到后部暂存区失败")
                logger.error("命令处理器", "放置失败")
                return False
            else:
                # 更新暂存区状态
                storage_mgr.update_slot(robot_id, object_type, empty_storage_index, bottle_id)
                storage_mgr.set_bottle_state(robot_id, object_type, empty_storage_index, BottleState.NOT_SPLIT)
                # 抓取完成，目标计数区域减少一瓶
                station_counter.decrement(target_area)
                # 记录已扫描的瓶子
                self.task_state_machine.add_scanned_bottle(bottle_id, object_type, empty_storage_index, task)
                logger.info("命令处理器", f"瓶子 {bottle_id} 已放置到 {object_type}[{empty_storage_index}]")
            
            # 步骤: 转回正面
            #input("back_to_front")
            self.task_state_machine.update_step(TaskStep.TURNING_BACK_FRONT, "转回正面")
            turn_waist_result = robot.send_service_request(
                robot.get_robot_service(),
                "back_to_front",
                extra_params={
                    "area": "scan_area"
                }
            )
            if not turn_waist_result:
                self.task_state_machine.set_error("转回正面失败")
                logger.error("命令处理器", "转腰失败")
                return False
            
            # 继续下一个瓶子（循环）

    def _store_bottles_loop(self, robot, storage_mgr, robot_id: str, target_area: str, bottle_msg: Dict) -> bool:
        """
        不扫码直接放置到后部暂存区流程（循环处理所有瓶子）
        
        该方法封装了完整的瓶子扫码和存储流程，包括：
        1. CV检测瓶子
        2. 抓取瓶子
        3. 放置到后部暂存区
        4. 转回正面
        
        参数:
            robot: 机器人控制器实例
            storage_mgr: 存储管理器实例
            robot_id: 机器人ID（用于区分不同机器人的暂存区）
            from_waiting_split_area: 是否从待分液区抓取（如果是则减少待分液区计数）
        
        返回:
            bool: 流程是否成功完成（True=成功/无更多瓶子，False=出错）
        """
        back_temp_storage = storage_mgr.get_storage(robot_id)
        station_counter = get_station_counter()
        available_count = station_counter.get_count(StationCounter.SPLIT_DONE_250ML_AREA)
        while available_count>0:
            # 检查暂存区是否全部已满
            if check_storage_is_full(back_temp_storage):
                self.task_state_machine.set_error("暂存区已满")
                logger.error("命令处理器", "暂存区已满")
                return
            
            # 步骤: CV检测
            #input("cv_detect")
            self.task_state_machine.update_step(TaskStep.CV_DETECTING, "视觉检测瓶子")
            cv_detect_result = robot.send_service_request(
                robot.get_robot_service(),
                "cv_detect",
                extra_params={
                    "waist": "not_turing"
                }
            )
            # 新协议：object_pose / object_type 从 values.return_params (JSON 字符串) 中解析
            object_pose, object_type = _parse_cv_detect_return_params(robot.last_service_return_params)
            
            if not cv_detect_result:
                logger.info("命令处理器", "检测不到更多瓶子，扫码任务完成")
                self.task_state_machine.update_step(TaskStep.CV_DETECTING_EMPTY, "视觉检测瓶子已抓完")
                return  # 没有更多瓶子，成功完成
            else:
                self.task_state_machine.update_step(TaskStep.CV_DETECTING_SUCCESS, "视觉检测瓶子成功")
                logger.info("命令处理器", "视觉检测瓶子成功")
            
            # 检查视觉识别结果是否和指定结果一样
            if object_type == None:
                object_type = bottle_msg.get("object_type")
            if object_type != bottle_msg.get("object_type"):
                self.task_state_machine.set_error(f"视觉识别结果和指定结果不一样，识别结果: {object_type}，指定结果: {bottle_msg.get('object_type')}")
                logger.error("命令处理器", f"视觉识别结果和指定结果不一样，识别结果: {object_type}，指定结果: {bottle_msg.get('object_type')}")
                return False
            
            # 模拟瓶子ID和类型
            bottle_id = bottle_msg.get("bottle_id")
            object_type = bottle_msg.get("object_type")
            task = bottle_msg.get("task")

            # 检查对应类型暂存区是否已满
            empty_storage_index = storage_mgr.get_empty_slot_index(robot_id, object_type)
            if empty_storage_index is None:
                self.task_state_machine.set_error(f"{object_type}暂存区已满")
                logger.error("命令处理器", f"{object_type}暂存区已满")
                return
            
            # 步骤: 抓取瓶子
            #input("grab_object_scan_table")
            self.task_state_machine.update_step(TaskStep.GRABBING_BOTTLE, f"抓取瓶子 ({object_type})")
            grab_result = robot.send_service_request(
                robot.get_robot_service(),
                "pick_object",
                extra_params={
                    "type": object_type
                }
            )
            
            if not grab_result:
                self.task_state_machine.set_error("抓取瓶子失败")
                logger.error("命令处理器", "抓取失败")
                return False
            
            # 步骤: 放置到后部暂存区
            if object_type == "glass_bottle_250":
                target_pose = f"back_temp_250_" + str(empty_storage_index)
            elif object_type == "glass_bottle_500":
                target_pose = f"back_temp_500_" + str(empty_storage_index)
            else:
                print(f"未知瓶子类型: {object_type}")
                logger.error("命令处理器", f"未知瓶子类型: {object_type}")
                return False
            
            #input("put_object_back")
            self.task_state_machine.update_step(TaskStep.PUTTING_TO_BACK, f"放置到后部暂存区 slot_{empty_storage_index}")
            put_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_object_back",
                extra_params={
                    "type": object_type,
                    "target_pose": target_pose
                }
            )
            
            if not put_result:
                self.task_state_machine.set_error("放置到后部暂存区失败")
                logger.error("命令处理器", "放置失败")
                return False
            else:
                # 更新暂存区状态
                storage_mgr.update_slot(robot_id, object_type, empty_storage_index, bottle_id)
                storage_mgr.set_bottle_state(robot_id, object_type, empty_storage_index, BottleState.NOT_SPLIT)
                # 抓取完成，目标计数区域减少一瓶
                station_counter.decrement(target_area)
                # 记录已扫描的瓶子
                self.task_state_machine.add_scanned_bottle(bottle_id, object_type, empty_storage_index, task)
                logger.info("命令处理器", f"瓶子 {bottle_id} 已放置到 {object_type}[{empty_storage_index}]")
            
            # 步骤: 转回正面
            #input("back_to_front")
            self.task_state_machine.update_step(TaskStep.TURNING_BACK_FRONT, "转回正面")
            turn_waist_result = robot.send_service_request(
                robot.get_robot_service(),
                "back_to_front",
                extra_params={
                    "area": "scan_area"
                }
            )
            if not turn_waist_result:
                self.task_state_machine.set_error("转回正面失败")
                logger.error("命令处理器", "转腰失败")
                return False
            
            # 继续下一个瓶子（循环）

    def _put_bottles_loop(self, robot, storage_mgr, target_area: str, all_bottles_in_storage: List[Dict], robot_id: str) -> bool:
        """
        把后部暂存区瓶子放到指定位置流程（循环处理所有瓶子）(这里特定为已分液完成的瓶子)
        
        参数:
            robot: 机器人控制器实例
            storage_mgr: 存储管理器实例
            target_area: 目标计数区域
            robot_id: 机器人ID
        """        
        # 遍历所有瓶子，执行放置动作
        for i, bottle_info in enumerate(all_bottles_in_storage):
            #input("press enter to continue...")
            bottle_id = bottle_info["bottle_id"]
            slot_index = bottle_info["slot_index"]
            bottle_type = bottle_info["bottle_type"]
            
            print(f"\n处理第 {i+1}/{len(all_bottles_in_storage)} 个瓶子:")
            print(f"  瓶子ID: {bottle_id}")
            print(f"  类型: {bottle_type}")
            print(f"  槽位: {slot_index}")
            
            self.task_state_machine.update_step(
                TaskStep.PUTTING_DOWN_BOTTLE, 
                f"放下瓶子 {i+1}/{len(all_bottles_in_storage)}: {bottle_id}"
            )

            # 步骤: 放置到后部暂存区
            if bottle_type == "glass_bottle_250":
                target_pose = f"back_temp_250_" + str(slot_index)
            elif bottle_type == "glass_bottle_500":
                target_pose = f"back_temp_500_" + str(slot_index)
            else:
                print(f"未知瓶子类型: {bottle_type}")
                logger.error("命令处理器", f"未知瓶子类型: {bottle_type}")
                return False
            
            # 执行放置动作
            #input("putting down bottle to split table..." + f"back_temp_{bottle_type}_" + str(slot_index))
            put_down_result = robot.send_service_request(
                robot.get_robot_service(),
                "put_down_split_table",
                extra_params={
                    "target_pose": target_pose
                }
            )
            if not put_down_result:
                error_msg = f"放置瓶子 {bottle_id} 失败"
                logger.error("命令处理器", error_msg)
                self.task_state_machine.set_error(error_msg)
                return
            else:
                # 更新暂存区状态
                storage_mgr.update_slot(robot_id, bottle_type, slot_index, 0)
                # 分液台待分液区增加一瓶
                # 如果target_area值在StationCounter中存在，则增加计数
                if target_area in StationCounter.__dict__:
                    station_counter = get_station_counter()
                    station_counter.increment(target_area)
                
            print(f"✓ 瓶子 {bottle_id} 放置完成")
            logger.info("命令处理器", f"瓶子 {bottle_id} 放置完成")

# 全局命令处理器实例
_cmd_handler = None

def init_cmd_handler(robots: Dict[str, RobotController] = None, started: bool = False):
    """
    初始化命令处理器

    参数:
        robots  : 机器人字典，key 为 robot_id，value 为 RobotController 实例。
        started : 是否已完成 START_WORKING 激活。
                  传 True 时（main.py 在 START_WORKING 后重新初始化），
                  新实例的 start_working_event 会被置为已触发，
                  使后续命令不会被休眠门卫拦截。
                  传 False（默认）时新实例保持休眠状态。
    """
    global _cmd_handler
    _cmd_handler = CmdHandler(robots)
    robot_count = len(robots) if robots else 0

    if started:
        _cmd_handler.start_working_event.set()

    logger.info(
        "命令处理器",
        f"命令处理器初始化完成，管理 {robot_count} 台机器人"
        + ("（系统已激活）" if started else "（休眠模式）"),
    )

def get_cmd_handler():
    """获取命令处理器实例"""
    global _cmd_handler
    if _cmd_handler is None:
        raise RuntimeError("命令处理器未初始化，请先调用init_cmd_handler")
    return _cmd_handler

def get_empty_storage_index(storage: Dict[str, List], object_type: str, robot_id: str = "robot_a") -> Optional[int]:
    """
    获取指定类型暂存区中第一个空位的索引
    注意：此函数保留是为了兼容性，建议使用 storage_manager.get_empty_slot_index()
    """
    storage_mgr = get_storage_manager()
    return storage_mgr.get_empty_slot_index(robot_id, object_type)

def check_storage_is_full(storage: Dict[str, List]) -> bool:
    """
    检查暂存区是否已满（兼容函数）
    注意：此函数直接检查传入的storage字典，不使用robot_id
    """
    for bottle_type, slots in storage.items():
        for slot in slots:
            if slot == 0:
                return False
    return True