"""
电池监控模块
自动检测机器人电池电量并在低电量时触发充电

功能：
- 定期检测所有已连接机器人的电池电量
- 电量低于阈值时，等待当前任务完成后前往充电
- 电量恢复后继续接收新任务

复用功能：
- robot_controller.py: subscribe_topic, get_topic_message, send_service_request
- task_state_machine.py: 获取任务状态
"""

import threading
import time
from typing import Dict, Optional, Callable
from infrastructure.constants import (
    ROSTopic,
    ROSTopicMessageType,
    ENABLE_AUTO_CHARGING,
    REQUIRE_BATTERY_INFO_ON_STARTUP,
    BATTERY_INFO_WAIT_TIMEOUT,
    BATTERY_CHECK_INTERVAL,
    BATTERY_LOW_THRESHOLD,
    BATTERY_TOPIC,
    CHARGING_STATION_POSE,
    CHARGING_DONE_HOME_POSES,
    CHARGING_DONE_HOME_POSE_DEFAULT,
    CHARGING_DONE_THRESHOLD,
    CHARGING_ACCEPT_TASK_THRESHOLD,
    CHARGING_STATUS_CHECK_DELAY,
    CHARGING_STATUS_MAX_RETRIES,
    CHARGING_STATUS_NOT_CHARGING,
    CHARGING_STATUS_CHARGING,
)
from infrastructure.error_logger import get_error_logger

# 尝试从外部配置加载自动充电参数
def _get_charging_config():
    """获取充电配置（优先使用外部配置）"""
    try:
        from infrastructure.config_loader import get_auto_charging_config
        external_config = get_auto_charging_config()
        if external_config:
            return {
                "enabled": external_config.get("enabled", ENABLE_AUTO_CHARGING),
                "require_battery_on_startup": external_config.get("require_battery_on_startup", REQUIRE_BATTERY_INFO_ON_STARTUP),
                "battery_wait_timeout": external_config.get("battery_wait_timeout", BATTERY_INFO_WAIT_TIMEOUT),
                "check_interval": external_config.get("check_interval", BATTERY_CHECK_INTERVAL),
                "low_threshold": external_config.get("low_threshold", BATTERY_LOW_THRESHOLD),
                "charging_done_threshold": external_config.get("charging_done_threshold", CHARGING_DONE_THRESHOLD),
                "charging_accept_task_threshold": external_config.get("charging_accept_task_threshold", CHARGING_ACCEPT_TASK_THRESHOLD),
                "charging_status_check_delay": external_config.get("charging_status_check_delay", CHARGING_STATUS_CHECK_DELAY),
                "charging_status_max_retries": external_config.get("charging_status_max_retries", CHARGING_STATUS_MAX_RETRIES),
                "charging_done_home_poses": external_config.get("charging_done_home_poses", CHARGING_DONE_HOME_POSES),
            }
    except ImportError:
        pass
    except Exception:
        pass
    
    # 回退到默认常量
    return {
        "enabled": ENABLE_AUTO_CHARGING,
        "require_battery_on_startup": REQUIRE_BATTERY_INFO_ON_STARTUP,
        "battery_wait_timeout": BATTERY_INFO_WAIT_TIMEOUT,
        "check_interval": BATTERY_CHECK_INTERVAL,
        "low_threshold": BATTERY_LOW_THRESHOLD,
        "charging_done_threshold": CHARGING_DONE_THRESHOLD,
        "charging_accept_task_threshold": CHARGING_ACCEPT_TASK_THRESHOLD,
        "charging_status_check_delay": CHARGING_STATUS_CHECK_DELAY,
        "charging_status_max_retries": CHARGING_STATUS_MAX_RETRIES,
        "charging_done_home_poses": CHARGING_DONE_HOME_POSES,
    }

logger = get_error_logger()


class RobotBatteryState:
    """单个机器人的电池状态"""
    PENDING = "pending"         # 等待获取电量信息
    NORMAL = "normal"           # 正常工作
    LOW_BATTERY = "low_battery" # 低电量，等待任务完成
    CHARGING = "charging"       # 充电中
    
    def __init__(self, robot_id: str):
        self.robot_id = robot_id
        self.state = self.PENDING  # 初始状态为等待电量信息
        self.percentage = None  # 电池百分比，None表示未获取
        self.last_check_time = 0
        self.subscribed = False
        self.battery_info_received = False  # 是否已收到电量信息


class BatteryMonitor:
    """
    电池监控器
    
    监控所有机器人的电池状态，在低电量时触发充电流程
    
    配置优先级：
    1. 外部配置文件 robot_config.json 的 auto_charging 部分
    2. constants.py 中的默认常量
    """
    
    def __init__(self):
        self.robots: Dict = {}  # {robot_id: RobotController}
        self.battery_states: Dict[str, RobotBatteryState] = {}  # {robot_id: RobotBatteryState}
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._task_state_callback: Optional[Callable] = None  # 获取任务状态的回调
        self._battery_info_ready_event = threading.Event()  # 电量信息就绪事件
        self._cmd_handler = None  # 命令处理器引用，用于复用导航等待方法
        
        # 加载配置（优先使用外部配置文件）
        self._config = _get_charging_config()
        self._enabled = self._config["enabled"]
        self._require_battery_on_startup = self._config["require_battery_on_startup"]
        self._battery_wait_timeout = self._config["battery_wait_timeout"]
        self._check_interval = self._config["check_interval"]
        self._low_threshold = self._config["low_threshold"]
        self._charging_done_threshold = self._config["charging_done_threshold"]
        self._charging_accept_task_threshold = self._config["charging_accept_task_threshold"]
        self._charging_status_check_delay = self._config["charging_status_check_delay"]
        self._charging_status_max_retries = self._config["charging_status_max_retries"]
        self._charging_done_home_poses = self._config["charging_done_home_poses"]  # 按机器人分配的home点位
        
    def set_robots(self, robots: Dict):
        """设置要监控的机器人"""
        self.robots = robots
        # 为每个机器人创建电池状态
        for robot_id in robots.keys():
            if robot_id not in self.battery_states:
                self.battery_states[robot_id] = RobotBatteryState(robot_id)
                logger.info("电池监控", f"添加机器人 {robot_id} 到监控列表")
    
    def set_task_state_callback(self, callback: Callable):
        """设置获取任务状态的回调函数"""
        self._task_state_callback = callback
    
    def set_cmd_handler(self, cmd_handler):
        """设置命令处理器引用，用于复用导航等待方法"""
        self._cmd_handler = cmd_handler
    
    def reload_config(self):
        """
        重新加载配置
        用于在系统重置时更新配置参数，无需重启
        """
        logger.info("电池监控", "正在重新加载配置...")
        
        # 重新获取配置
        self._config = _get_charging_config()
        self._enabled = self._config["enabled"]
        self._require_battery_on_startup = self._config["require_battery_on_startup"]
        self._battery_wait_timeout = self._config["battery_wait_timeout"]
        self._check_interval = self._config["check_interval"]
        self._low_threshold = self._config["low_threshold"]
        self._charging_done_threshold = self._config["charging_done_threshold"]
        self._charging_accept_task_threshold = self._config["charging_accept_task_threshold"]
        self._charging_status_check_delay = self._config["charging_status_check_delay"]
        self._charging_status_max_retries = self._config["charging_status_max_retries"]
        self._charging_done_home_poses = self._config["charging_done_home_poses"]
        
        # 重置电池信息就绪事件（下次START_WORKING时重新获取）
        self._battery_info_ready_event.clear()
        
        # 重置所有机器人的电池状态为PENDING
        for robot_id in self.battery_states:
            self.battery_states[robot_id] = RobotBatteryState(robot_id)
        
        logger.info("电池监控", f"配置已重新加载 - 启用: {self._enabled}, 低电量阈值: {self._low_threshold*100:.0f}%, 充电完成阈值: {self._charging_done_threshold*100:.0f}%")
        print(f"✓ 电池监控配置已重新加载")
    
    def can_accept_task(self, robot_id: str) -> tuple:
        """
        检查指定机器人是否可以接收新任务
        
        参数:
            robot_id: 机器人ID
            
        返回:
            (can_accept, need_go_home, reason)
            - can_accept: 是否可以接收任务
            - need_go_home: 是否需要先返回home点
            - reason: 如果不能接收任务，返回原因
        """
        if not self._enabled:
            # 充电功能禁用时，直接允许接收任务
            return (True, False, None)
        
        state = self.battery_states.get(robot_id)
        if not state:
            # 未监控的机器人，直接允许接收任务
            return (True, False, None)
        
        if state.state != RobotBatteryState.CHARGING:
            # 非充电状态，直接允许接收任务
            return (True, False, None)
        
        # 充电状态下检查电量
        if state.percentage is None:
            return (False, False, f"{robot_id} 正在充电中，电量信息未获取")
        
        if state.percentage < self._charging_accept_task_threshold:
            # 电量低于可接收任务阈值，拒绝任务
            return (False, False, 
                    f"{robot_id} 正在充电中 (电量: {state.percentage*100:.1f}%)，"
                    f"需等待充电至 {self._charging_accept_task_threshold*100:.0f}% 才能接收新任务")
        
        # 电量高于可接收任务阈值但低于充电完成阈值，需要先返回home
        return (True, True, None)
    
    def get_robot_home_pose(self, robot_id: str) -> str:
        """获取指定机器人的home点位"""
        return self._charging_done_home_poses.get(robot_id, CHARGING_DONE_HOME_POSE_DEFAULT)
    
    def get_charging_accept_task_threshold(self) -> float:
        """获取可接收任务的电量阈值"""
        return self._charging_accept_task_threshold
    
    def start(self):
        """启动电池监控"""
        if not self._enabled:
            logger.info("电池监控", "自动充电功能已禁用")
            print("⚡ 自动充电功能已禁用（可在robot_config.json或constants.py中启用）")
            # 即使禁用自动充电，也标记电量信息已就绪（跳过等待）
            self._battery_info_ready_event.set()
            return
        
        if self._running:
            logger.warning("电池监控", "监控器已在运行")
            return
        
        self._running = True
        self._stop_event.clear()
        self._battery_info_ready_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        
        logger.info("电池监控", "电池监控器已启动")
        print(f"⚡ 电池监控器已启动")
        print(f"   检测间隔: {self._check_interval}秒")
        print(f"   低电量阈值: {self._low_threshold*100:.0f}%")
        print(f"   可接收任务阈值: {self._charging_accept_task_threshold*100:.0f}%")
        print(f"   充电完成阈值: {self._charging_done_threshold*100:.0f}%")
        print(f"   充电完成返回点位（按机器人分配）:")
        for robot_id, home_pose in self._charging_done_home_poses.items():
            print(f"      {robot_id}: {home_pose}")
        
        # 如果需要在启动时等待电量信息
        if self._require_battery_on_startup:
            print(f"⏳ 等待获取机器人电量信息...")
            self._wait_for_initial_battery_info()
    
    def stop(self):
        """停止电池监控"""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)
        
        # 取消所有电池topic订阅
        for robot_id, robot in self.robots.items():
            if robot_id in self.battery_states and self.battery_states[robot_id].subscribed:
                try:
                    robot.unsubscribe_topic(BATTERY_TOPIC)
                except:
                    pass
        
        logger.info("电池监控", "电池监控器已停止")
        print("⚡ 电池监控器已停止")
    
    def _wait_for_initial_battery_info(self):
        """等待获取所有机器人的初始电量信息"""
        if not self._require_battery_on_startup:
            self._battery_info_ready_event.set()
            return
        
        logger.info("电池监控", "开始等待机器人电量信息...")
        
        timeout = self._battery_wait_timeout if self._battery_wait_timeout > 0 else None
        start_time = time.time()
        
        # 等待电量信息就绪事件
        result = self._battery_info_ready_event.wait(timeout=timeout)
        
        elapsed = time.time() - start_time
        
        if result:
            logger.info("电池监控", f"电量信息获取完成，耗时 {elapsed:.1f} 秒")
        else:
            # 超时，但仍然允许继续（标记为就绪）
            logger.warning("电池监控", f"等待电量信息超时 ({timeout}秒)，继续运行")
            print(f"⚠️ 等待电量信息超时，部分机器人可能无法获取电量")
            self._battery_info_ready_event.set()
    
    def is_battery_info_ready(self, robot_id: str = None) -> bool:
        """
        检查电量信息是否已就绪
        
        参数:
            robot_id: 指定机器人ID，为None则检查所有机器人
        
        返回:
            bool: 电量信息是否已就绪
        """
        if not self._enabled or not self._require_battery_on_startup:
            return True
        
        if robot_id:
            state = self.battery_states.get(robot_id)
            if not state:
                return True  # 未配置的机器人默认就绪
            return state.battery_info_received
        
        return self._battery_info_ready_event.is_set()
    
    def wait_for_battery_info(self, timeout: float = None) -> bool:
        """
        等待电量信息就绪
        
        参数:
            timeout: 超时时间（秒），None表示无限等待
        
        返回:
            bool: 是否成功获取到电量信息
        """
        if not self._enabled or not self._require_battery_on_startup:
            return True
        return self._battery_info_ready_event.wait(timeout=timeout)
    
    def _monitor_loop(self):
        """监控循环"""
        logger.info("电池监控", "开始监控循环")
        
        # 首次启动时订阅所有机器人的电池topic
        self._subscribe_all_battery_topics()
        
        # 如果需要等待电量信息，先快速检测几次
        if self._require_battery_on_startup and not self._battery_info_ready_event.is_set():
            logger.info("电池监控", "快速检测电量信息...")
            for _ in range(30):  # 最多检测30次，每次1秒
                if self._stop_event.is_set():
                    break
                try:
                    self._check_all_batteries()
                except Exception as e:
                    logger.exception_occurred("电池监控", "检测电池状态", e)
                
                if self._battery_info_ready_event.is_set():
                    break
                time.sleep(1)
        
        while self._running and not self._stop_event.is_set():
            try:
                self._check_all_batteries()
            except Exception as e:
                logger.exception_occurred("电池监控", "检测电池状态", e)
            
            # 等待下一次检测
            self._stop_event.wait(timeout=self._check_interval)
        
        logger.info("电池监控", "监控循环已结束")
    
    def _subscribe_all_battery_topics(self):
        """订阅所有机器人的电池topic"""
        for robot_id, robot in self.robots.items():
            if robot_id not in self.battery_states:
                self.battery_states[robot_id] = RobotBatteryState(robot_id)
            
            state = self.battery_states[robot_id]
            if not state.subscribed:
                try:
                    success = robot.subscribe_topic(
                        topic_name=BATTERY_TOPIC,
                        msg_type=ROSTopicMessageType.BATTERY_STATE,  # 标准电池消息类型
                        throttle_rate=0,
                        queue_length=1
                    )
                    if success:
                        state.subscribed = True
                        logger.info("电池监控", f"{robot_id} 已订阅电池状态topic")
                    else:
                        logger.warning("电池监控", f"{robot_id} 订阅电池状态topic失败")
                except Exception as e:
                    logger.exception_occurred("电池监控", f"{robot_id} 订阅电池topic", e)
    
    def _check_all_batteries(self):
        """检测所有机器人的电池状态"""
        current_time = time.time()
        all_battery_received = True
        
        for robot_id, robot in self.robots.items():
            if not robot or not robot.is_connected():
                all_battery_received = False
                continue
            
            state = self.battery_states.get(robot_id)
            if not state:
                all_battery_received = False
                continue
            
            # 获取电池状态
            battery_info = robot.get_topic_message(ROSTopic.BATTERY_STATE, ROSTopicMessageType.BATTERY_STATE)
            if battery_info:
                # 解析电池百分比
                percentage = battery_info.get("percentage", 1.0)
                state.percentage = percentage
                state.last_check_time = current_time
                
                # 首次收到电量信息
                if not state.battery_info_received:
                    state.battery_info_received = True
                    logger.info("电池监控", f"{robot_id} 首次获取到电量信息: {percentage*100:.1f}%")
                    print(f"✅ {robot_id} 电量信息已获取: {percentage*100:.1f}%")
                else:
                    logger.info("电池监控", f"{robot_id} 电量: {percentage*100:.1f}%")
                    print(f"⚡ {robot_id} 电量: {percentage*100:.1f}%")
                
                # 根据电量状态进行处理
                self._handle_battery_state(robot_id, robot, state)
            else:
                if not state.battery_info_received:
                    all_battery_received = False
                logger.warning("电池监控", f"{robot_id} 无法获取电池状态")
        
        # 检查是否所有机器人都已获取到电量信息
        if all_battery_received and not self._battery_info_ready_event.is_set():
            self._battery_info_ready_event.set()
            logger.info("电池监控", "所有机器人电量信息已获取")
            print("✅ 所有机器人电量信息已获取，系统就绪")
    
    def _handle_battery_state(self, robot_id: str, robot, state: RobotBatteryState):
        """处理电池状态"""
        percentage = state.percentage
        
        if state.state == RobotBatteryState.PENDING:
            # 等待电量信息状态 -> 收到电量信息后判断是否需要充电
            if percentage < self._low_threshold:
                logger.warning("电池监控", f"{robot_id} 启动时电量低 ({percentage*100:.1f}%)，需要先充电")
                print(f"⚠️ {robot_id} 启动时电量低 ({percentage*100:.1f}%)，需要先充电")
                state.state = RobotBatteryState.LOW_BATTERY
                # 尝试触发充电
                self._try_start_charging(robot_id, robot, state)
            else:
                logger.info("电池监控", f"{robot_id} 电量正常 ({percentage*100:.1f}%)，可以工作")
                print(f"✅ {robot_id} 电量正常 ({percentage*100:.1f}%)，可以工作")
                state.state = RobotBatteryState.NORMAL
        
        elif state.state == RobotBatteryState.NORMAL:
            # 正常状态下检测是否低电量
            if percentage < self._low_threshold:
                logger.warning("电池监控", f"{robot_id} 电量低 ({percentage*100:.1f}%)，准备充电")
                print(f"⚠️ {robot_id} 电量低 ({percentage*100:.1f}%)，等待当前任务完成后前往充电")
                state.state = RobotBatteryState.LOW_BATTERY
                # 注意：不立即触发充电，等待当前任务完成
        
        elif state.state == RobotBatteryState.LOW_BATTERY:
            # 低电量状态，等待任务完成后再充电
            self._try_start_charging(robot_id, robot, state)
        
        elif state.state == RobotBatteryState.CHARGING:
            # 充电中，检测是否充满（电量>=充电完成阈值时返回home点位）
            if percentage >= self._charging_done_threshold:
                logger.info("电池监控", f"{robot_id} 充电完成 ({percentage*100:.1f}%)，开始返回home点位")
                print(f"✅ {robot_id} 充电完成 ({percentage*100:.1f}%)，返回home点位...")
                
                # 导航到home点位
                self._navigate_to_home(robot_id, robot, state)
            else:
                # 继续充电，显示当前电量
                print(f"🔋 {robot_id} 充电中... 当前电量: {percentage*100:.1f}%，目标: {self._charging_done_threshold*100:.0f}%")
    
    def _try_start_charging(self, robot_id: str, robot, state: RobotBatteryState):
        """尝试开始充电"""
        # 检查是否还在初始电量信息获取阶段，如果是则暂不触发充电
        if not self._battery_info_ready_event.is_set():
            logger.info("电池监控", f"{robot_id} 电量低，但系统仍在获取初始电量信息，暂不触发充电")
            return
        
        # 检查当前是否有任务在执行
        if self._task_state_callback:
            task_state = self._task_state_callback()
            if task_state and task_state.get("is_running", False):
                logger.info("电池监控", f"{robot_id} 当前有任务执行中，等待完成")
                return
        
        # 立即设置状态为CHARGING，防止重复触发导航
        state.state = RobotBatteryState.CHARGING
        
        # 任务已完成或无任务，前往充电
        logger.info("电池监控", f"{robot_id} 开始前往充电桩")
        print(f"🔋 {robot_id} 前往充电桩...")
        
        # 尝试导航到充电桩并验证充电状态
        retry_count = 0
        max_retries = self._charging_status_max_retries
        check_delay = self._charging_status_check_delay
        
        while retry_count <= max_retries:
            try:
                # 使用topic发布导航命令
                robot.publish_topic(
                    topic_name="/navigation_control",
                    msg_type="std_msgs/String",
                    msg_data={"data": CHARGING_STATION_POSE}
                )
                nav_result = self._wait_for_navigation_finished(robot)
                
                if not nav_result:
                    logger.error("电池监控", f"{robot_id} 导航到充电桩失败")
                    print(f"❌ {robot_id} 导航到充电桩失败")
                    # 导航失败，恢复到LOW_BATTERY状态以便重试
                    state.state = RobotBatteryState.LOW_BATTERY
                    return
                
                logger.info("电池监控", f"{robot_id} 已到达充电桩，等待 {check_delay} 秒后检查充电状态...")
                print(f"🔋 {robot_id} 已到达充电桩，等待 {check_delay} 秒后检查充电状态...")
                
                # 等待指定时间后检查充电状态
                time.sleep(check_delay)
                
                # 检查充电状态
                charging_status = self._get_charging_status(robot)
                battery_charging_status = charging_status.get("battery_charging_status", -1)
                charge_port_connected = charging_status.get("charge_port_connected", False)
                if battery_charging_status == CHARGING_STATUS_CHARGING:
                    # 充电中，成功
                    logger.info("电池监控", f"{robot_id} 确认充电中")
                    print(f"✅ {robot_id} 确认充电中")
                    return
                
                # 充电状态为0（未充电），需要重试
                retry_count += 1
                
                if retry_count <= max_retries:
                    logger.warning("电池监控", 
                                  f"{robot_id} 充电状态异常 (status={battery_charging_status}, port_connected={charge_port_connected})，"
                                  f"重试导航 [{retry_count}/{max_retries}]")
                    print(f"⚠️ {robot_id} 充电状态异常，重试导航 [{retry_count}/{max_retries}]...")
                else:
                    # 超过最大重试次数，报错
                    self._report_charging_error(robot_id, charge_port_connected, battery_charging_status)
                    # 恢复到LOW_BATTERY状态
                    state.state = RobotBatteryState.LOW_BATTERY
                    return
                    
            except Exception as e:
                logger.exception_occurred("电池监控", f"{robot_id} 导航到充电桩", e)
                # 异常时恢复到LOW_BATTERY状态以便重试
                state.state = RobotBatteryState.LOW_BATTERY
                return
    
    def _get_charging_status(self, robot, timeout: float = 10.0) -> dict:
        """
        获取机器人充电状态
        
        参数:
            robot: 机器人实例
            timeout: 超时时间（秒），默认10秒
        
        返回:
            {
                "battery_charging_status": int,  # 0=未充电, 1=充电中, 2=充电完成
                "charge_port_connected": bool    # 充电口是否连接
            }
        """
        try:
            # 使用重试机制获取充电状态（与 cmd_handler 中的方式一致）
            start_time = time.time()
            charging_info = None
            
            while charging_info is None and (time.time() - start_time) < timeout:
                charging_info = robot.get_topic_message(ROSTopic.CHARGING_STATUS_TOPIC, ROSTopicMessageType.CHARGING_STATUS)
                if charging_info is None:
                    time.sleep(0.5)
            
            if charging_info:
                logger.info("电池监控", f"获取到充电状态: {charging_info}")
                return {
                    "battery_charging_status": charging_info.get("status").get("battery_charging_status"),
                    "charge_port_connected": charging_info.get("status").get("charge_port_connected")
                }
            
            logger.warning("电池监控", f"获取充电状态超时 ({timeout}秒)")
            return {"battery_charging_status": -1, "charge_port_connected": False}
            
        except Exception as e:
            logger.exception_occurred("电池监控", "获取充电状态", e)
            return {"battery_charging_status": -1, "charge_port_connected": False}
    
    def _report_charging_error(self, robot_id: str, charge_port_connected: bool, battery_charging_status: int):
        """
        报告充电失败错误
        
        参数:
            robot_id: 机器人ID
            charge_port_connected: 充电口是否连接
            battery_charging_status: 当前充电状态值
        """
        if charge_port_connected:
            # 插口已连接但未充电 - 充电桩问题
            error_msg = (f"❌ {robot_id} 充电失败: 充电口已连接但未开始充电 "
                        f"(status={battery_charging_status})，可能是充电桩故障")
            logger.error("电池监控", 
                        f"{robot_id} 充电桩故障: 充电口已连接(charge_port_connected=True)，"
                        f"但充电状态异常(battery_charging_status={battery_charging_status})")
        else:
            # 插口未连接 - 导航问题
            error_msg = (f"❌ {robot_id} 充电失败: 充电口未连接 "
                        f"(charge_port_connected=False)，可能是导航定位不准确")
            logger.error("电池监控", 
                        f"{robot_id} 导航定位问题: 充电口未连接(charge_port_connected=False)，"
                        f"导航到充电桩位置可能不准确")
        
        print(error_msg)
        print(f"   充电状态: battery_charging_status={battery_charging_status}")
        print(f"   充电口连接: charge_port_connected={charge_port_connected}")
    
    def _wait_for_navigation_finished(self, robot) -> bool:
        """
        等待导航完成（复用cmd_handler中的方法）
        
        参数:
            robot: 机器人实例
            
        返回:
            bool: 导航是否成功完成
        """
        if self._cmd_handler is None:
            logger.error("电池监控", "cmd_handler未设置，无法等待导航完成")
            return False
        return self._cmd_handler._wait_for_navigation_finished(robot)
    
    def _navigate_to_home(self, robot_id: str, robot, state: RobotBatteryState):
        """充电完成后导航到home点位"""
        # 立即设置状态为NORMAL，防止重复触发或误判
        state.state = RobotBatteryState.NORMAL
        
        try:
            # 根据机器人ID获取对应的home点位
            home_pose = self._charging_done_home_poses.get(robot_id, CHARGING_DONE_HOME_POSE_DEFAULT)
            logger.info("电池监控", f"{robot_id} 返回home点位: {home_pose}")
            
            # 先导航准备
            robot.send_service_request(
                robot.get_robot_service(),
                "navigation_prepare"
            )
            
            # 使用topic发布导航命令到home点位
            robot.publish_topic(
                topic_name=ROSTopic.NAVIGATION_CONTROL,
                msg_type=ROSTopicMessageType.NAVIGATION_CONTROL,
                msg_data={"data": home_pose}
            )
            result = self._wait_for_navigation_finished(robot)
            if not result:
                logger.error("电池监控", f"{robot_id} 导航到home点位失败")
                print(f"❌ {robot_id} 导航到home点位失败")
                # 即使导航失败，也恢复正常状态，避免卡住
                state.state = RobotBatteryState.NORMAL
                return
            state.state = RobotBatteryState.NORMAL
            logger.info("电池监控", f"{robot_id} 已返回home点位，恢复正常工作")
            print(f"✅ {robot_id} 已返回home点位，恢复正常工作")
        except Exception as e:
            logger.exception_occurred("电池监控", f"{robot_id} 导航到home点位", e)
            # 即使导航失败，也恢复正常状态，避免卡住
            state.state = RobotBatteryState.NORMAL
    
    def is_robot_available(self, robot_id: str) -> tuple:
        """
        检查机器人是否可用于接收新任务
        
        返回:
            tuple: (is_available, reason)
            - is_available: bool, 是否可用
            - reason: str, 不可用的原因（如果可用则为None）
              - "battery_info_pending": 电量信息未获取
              - "low_battery": 低电量状态
              - "charging_below_threshold": 充电中且电量低于可接收任务阈值
              - "charging_need_go_home": 充电中但电量已达到可接收任务阈值，需先返回home
              - None: 可正常接收任务
        """
        if not self._enabled:
            return True, None
        
        state = self.battery_states.get(robot_id)
        if not state:
            return True, None
        
        # 检查是否还在等待电量信息
        if state.state == RobotBatteryState.PENDING:
            return False, "battery_info_pending"
        
        # 检查是否低电量
        if state.state == RobotBatteryState.LOW_BATTERY:
            return False, "low_battery"
        
        # 检查是否充电中
        if state.state == RobotBatteryState.CHARGING:
            # 检查电量是否达到可接收任务阈值
            if state.percentage is None or state.percentage < self._charging_accept_task_threshold:
                return False, "charging_below_threshold"
            else:
                # 电量已达到可接收任务阈值，可以接收任务但需先返回home
                return True, "charging_need_go_home"
        
        return True, None
    
    def get_battery_status(self, robot_id: str = None) -> Dict:
        """获取电池状态"""
        if robot_id:
            state = self.battery_states.get(robot_id)
            if state:
                available, reason = self.is_robot_available(robot_id)
                return {
                    "robot_id": robot_id,
                    "percentage": state.percentage,
                    "state": state.state,
                    "battery_info_received": state.battery_info_received,
                    "available": available,
                    "unavailable_reason": reason
                }
            return None
        
        # 返回所有机器人的电池状态
        result = {}
        for robot_id, state in self.battery_states.items():
            available, reason = self.is_robot_available(robot_id)
            result[robot_id] = {
                "percentage": state.percentage,
                "state": state.state,
                "battery_info_received": state.battery_info_received,
                "available": available,
                "unavailable_reason": reason
            }
        return result


# 全局电池监控器实例
_battery_monitor: Optional[BatteryMonitor] = None


def init_battery_monitor() -> BatteryMonitor:
    """初始化电池监控器"""
    global _battery_monitor
    _battery_monitor = BatteryMonitor()
    return _battery_monitor


def get_battery_monitor() -> Optional[BatteryMonitor]:
    """获取电池监控器实例"""
    return _battery_monitor


def is_robot_available_for_task(robot_id: str) -> tuple:
    """
    检查机器人是否可用于接收新任务
    
    返回:
        tuple: (is_available, reason)
    """
    if _battery_monitor:
        return _battery_monitor.is_robot_available(robot_id)
    return True, None


def is_battery_info_ready(robot_id: str = None) -> bool:
    """
    检查电量信息是否已就绪
    
    参数:
        robot_id: 指定机器人ID，为None则检查所有机器人
    """
    if _battery_monitor:
        return _battery_monitor.is_battery_info_ready(robot_id)
    return True


def wait_for_battery_info(timeout: float = None) -> bool:
    """
    等待电量信息就绪
    
    参数:
        timeout: 超时时间（秒），None表示无限等待
    """
    if _battery_monitor:
        return _battery_monitor.wait_for_battery_info(timeout)
    return True

