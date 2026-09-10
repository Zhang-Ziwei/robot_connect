import asyncio
import websockets
import json
import threading
import time
from dataclasses import dataclass, field
from infrastructure.constants import (
    ROSTopic, ROSService, get_main_ros_service,
    ROSTopicMessageType, ROSServiceMessageType,
    ROBOT_WS_RECONNECT_MAX_ATTEMPTS, ROBOT_WS_RECONNECT_INTERVAL,
    ROBOT_WS_PING_INTERVAL, ROBOT_WS_PING_TIMEOUT
)
from infrastructure.error_logger import get_error_logger
logger = get_error_logger()


@dataclass
class ServiceTaskResult:
    """
    send_service_request_task 的返回值。

    字段对应新 service 契约的 Response：
        bool   success       — 操作是否成功
        str    error_msg     — 失败时的错误信息（成功时为空字符串）
        str    return_params — 返回参数（通常是 JSON 字符串，用法与 extra_params 对称）

    真值判断（bool(result)）等价于 result.success，方便用 ``if not result:`` 做失败分支。
    """
    success: bool = False
    error_msg: str = ""
    return_params: str = ""

    def __bool__(self):
        return self.success

    def parse_return_params(self) -> dict:
        """将 return_params 字符串解析为 dict；为空或解析失败时返回空 dict。"""
        if not self.return_params:
            return {}
        try:
            return json.loads(self.return_params)
        except (json.JSONDecodeError, TypeError):
            return {}


def _get_connection_config():
    """获取机器人连接配置（优先使用外部配置）"""
    try:
        from infrastructure.config_loader import get_robot_connection_config
        external_config = get_robot_connection_config()
        if external_config:
            return {
                "reconnect_max_attempts": external_config.get("reconnect_max_attempts", ROBOT_WS_RECONNECT_MAX_ATTEMPTS),
                "reconnect_interval": external_config.get("reconnect_interval", ROBOT_WS_RECONNECT_INTERVAL),
                "ping_interval": external_config.get("ping_interval", ROBOT_WS_PING_INTERVAL),
                "ping_timeout": external_config.get("ping_timeout", ROBOT_WS_PING_TIMEOUT),
            }
    except ImportError:
        pass
    except Exception:
        pass
    
    # 使用默认常量配置
    return {
        "reconnect_max_attempts": ROBOT_WS_RECONNECT_MAX_ATTEMPTS,
        "reconnect_interval": ROBOT_WS_RECONNECT_INTERVAL,
        "ping_interval": ROBOT_WS_PING_INTERVAL,
        "ping_timeout": ROBOT_WS_PING_TIMEOUT,
    }

class RobotController:
    def __init__(self, host, port=None, robot_type=None, max_retry_attempts=None, retry_interval=5, navigation_map=None):
        self.host = host
        self.port = port  # None表示不使用端口（WiFi连接时）
        self.robot_type = robot_type
        self.robot_name = str(robot_type or "robot").replace("_", " ").title()
        self.connected = False
        self.websocket = None
        self.mutex = threading.Lock()
        self.loop = None
        self.thread = None
        # 重连配置
        self.max_retry_attempts = max_retry_attempts  # None表示无限重试
        self.retry_interval = retry_interval  # 重试间隔（秒）
        self.retry_count = 0  # 当前重试次数
        self._stop_reconnect = False  # 停止重连标志
        # Topic订阅相关
        self.subscribed_topics = {}  # {topic_name: msg_type}
        self.topic_messages = {}  # {topic_name: latest_message}
        self._topic_listener_started = False  # 监听器是否已启动
        # 服务请求响应队列
        self.service_response_future = None  # 用于存储等待的服务响应
        # 导航地图（连接成功后自动下发）
        self.navigation_map = navigation_map  # 配置的地图名，如 "test1"；None 表示不下发
        # 最近一次 send_service_request 返回的字段（新协议：success/error_msg/return_params）
        # 供调用方在需要时查询（例如 CV 检测需要取 return_params 解析 object_pose/type）
        self.last_service_success = False       # values.success
        self.last_service_error_msg = ""        # values.error_msg
        self.last_service_return_params = ""    # values.return_params（字符串，通常是 JSON）
        # 导航地图下发状态跟踪（用于启动流程中"等地图就绪再激活系统"）
        self._nav_map_ready_event = threading.Event()  # set 后表示一次 set_navigation_map 流程已结束
        self._nav_map_ready_success = False             # 最近一次下发是否成功
        self._nav_map_ready_message = ""                # 最近一次下发的消息
        self._last_reconnect_error = ""
    
    def _get_address_str(self):
        """获取地址字符串（用于显示）"""
        if self.port:
            return f"{self.host}:{self.port}"
        else:
            return self.host
    
    def _get_uri(self):
        """获取WebSocket URI"""
        if self.port:
            return f"ws://{self.host}:{self.port}/"
        else:
            return f"ws://{self.host}/"
    
    def get_robot_service(self) -> str:
        """
        获取该机器人对应的主ROS服务名称
        
        根据机器人类型返回对应的服务:
        - Robot A -> STRAWBERRY_SERVICE
        - Robot B -> CHEM_PROJECT_SERVICE
        
        Returns:
            str: ROS服务名称
        """
        if self.robot_type:
            return get_main_ros_service(self.robot_type)
        # 默认返回 STRAWBERRY_SERVICE
        return ROSService.STRAWBERRY_SERVICE
        
    def connect(self):
        """连接到机器人WebSocket服务，支持自动重试"""
        attempt = 0
        # 不在这里清掉 _stop_reconnect：RESET_SYSTEM 已经要求停机时，
        # 业务层误调 connect() 不能把停机标志冲掉。START_WORKING 会新建实例。
        
        while True:
            # 检查是否应该停止重连
            if self._stop_reconnect:
                print(f"⏹ {self.robot_name} 重连已被停止")
                return False
            
            with self.mutex:
                if self.connected:
                    print(f"✓ {self.robot_name} 已连接")
                    self.retry_count = 0  # 重置重试计数
                    return True
                
                attempt += 1
                self.retry_count = attempt
                
                # 检查是否超过最大重试次数
                if self.max_retry_attempts is not None and attempt > self.max_retry_attempts:
                    error_msg = f"连接失败：已达到最大重试次数 ({self.max_retry_attempts})"
                    print(f"✗ {self.robot_name} {error_msg}")
                    # 记录错误日志
                    get_error_logger().connection_failed(
                        self.robot_name, self.host, self.port, error_msg
                    )
                    return False
                
                # 显示重试信息
                if attempt == 1:
                    print(f"\n{'='*60}")
                    print(f"正在连接 {self.robot_name} ({self._get_address_str()})...")
                    if self.max_retry_attempts is None:
                        print(f"重试策略：无限重试，间隔 {self.retry_interval} 秒")
                    else:
                        print(f"重试策略：最多 {self.max_retry_attempts} 次，间隔 {self.retry_interval} 秒")
                    print(f"{'='*60}\n")
                else:
                    print(f"\n[重试 {attempt}/{self.max_retry_attempts if self.max_retry_attempts else '∞'}] 尝试连接 {self.robot_name}...")
                
                # 清理之前的连接
                if self.loop and self.loop.is_running():
                    self.loop.call_soon_threadsafe(self.loop.stop())
                if self.thread and self.thread.is_alive():
                    self.thread.join(timeout=2)
                
                # 重置topic监听器标志（重连时需要重新启动）
                self._topic_listener_started = False
                
                # 清空之前的topic订阅记录和消息缓存
                self.subscribed_topics.clear()
                self.topic_messages.clear()
                
                # 创建新的事件循环并在单独的线程中运行
                self.loop = asyncio.new_event_loop()
                self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
                self.thread.start()
                
                # 等待连接完成
                start_time = time.time()
                timeout = 10  # 10秒超时
                while not self.connected and (time.time() - start_time) < timeout:
                    time.sleep(0.1)
                
                # 检查是否连接成功
                if self.connected:
                    print(f"✓ {self.robot_name} 连接成功！")
                    # 记录连接成功
                    get_error_logger().connection_success(
                        self.robot_name, self.host, self.port, attempt
                    )
                    self.retry_count = 0
                    # 触发"连接后自动下发导航地图"（后台线程，不阻塞 connect 返回）
                    self._schedule_auto_set_navigation_map()
                    return True
            
            # 连接失败，等待后重试
            print(f"✗ {self.robot_name} 连接失败")
            if self.max_retry_attempts is None or attempt < self.max_retry_attempts:
                print(f"⏳ 等待 {self.retry_interval} 秒后重试...")
                # 分段等待，以便能够响应停止信号
                for _ in range(int(self.retry_interval * 10)):
                    if self._stop_reconnect:
                        print(f"⏹ {self.robot_name} 重连已被停止")
                        return False
                    time.sleep(0.1)
            else:
                return False
    
    def _schedule_auto_set_navigation_map(self):
        """
        连接/重连成功后调度自动下发导航地图。
        
        在独立线程中执行，不阻塞 connect() 返回：
        1. 读取 navigation_map 运行时配置（robot_config.json.navigation_map 优先）
        2. 若关闭或未配置 navigation_map 名，直接返回
        3. 调用 navigation_utils.set_navigation_map（内部会先查询 + 等空闲）
        
        避免循环导入：在函数内部懒加载。
        """
        try:
            from infrastructure.constants import get_navigation_map_runtime_config
        except ImportError:
            return
        
        cfg = get_navigation_map_runtime_config()
        # 重置事件：本轮下发尚未完成
        self._nav_map_ready_event.clear()
        self._nav_map_ready_success = False
        self._nav_map_ready_message = ""
        
        if not cfg.get("auto_set_on_connect", True):
            # 关闭时也标记为"就绪"（无需等待），避免 wait_for_navigation_map_ready 永远卡住
            self._nav_map_ready_success = True
            self._nav_map_ready_message = "auto_set_on_connect=false，跳过"
            self._nav_map_ready_event.set()
            return
        if not self.navigation_map:
            self._nav_map_ready_success = True
            self._nav_map_ready_message = "未配置 navigation_map，跳过"
            self._nav_map_ready_event.set()
            return
        
        def _worker():
            try:
                # 给监听器/订阅一点时间稳定
                time.sleep(1.0)
                from hardware.navigation_utils import set_navigation_map
                ok, msg = set_navigation_map(
                    self,
                    map_name=self.navigation_map,
                    wait_idle_timeout=cfg.get("wait_idle_timeout", 60),
                    service_timeout=cfg.get("service_timeout", 30),
                    skip_if_same=cfg.get("skip_if_same", True),
                )
                self._nav_map_ready_success = ok
                self._nav_map_ready_message = msg or ""
                if ok:
                    print(f"🗺 {self.robot_name} 已下发导航地图: {self.navigation_map}")
                    # 可选：地图设置成功后自动执行导航定位
                    try:
                        from infrastructure.constants import get_navigation_localization_runtime_config
                        from hardware.navigation_utils import set_navigation_localization
                        loc_cfg = get_navigation_localization_runtime_config()
                        if loc_cfg.get("auto_set_after_map", False):
                            print(f"📍 {self.robot_name} 开始自动导航定位...")
                            loc_ok, loc_msg = set_navigation_localization(
                                self,
                                map_name=self.navigation_map,
                                method=loc_cfg.get("method", "auto"),
                                map_path=loc_cfg.get("map_path", ""),
                                x_pos=loc_cfg.get("x_pos", 0.0),
                                y_pos=loc_cfg.get("y_pos", 0.0),
                                z_pos=loc_cfg.get("z_pos", 0.0),
                                x_ori=loc_cfg.get("x_ori", 0.0),
                                y_ori=loc_cfg.get("y_ori", 0.0),
                                z_ori=loc_cfg.get("z_ori", 0.0),
                                w_ori=loc_cfg.get("w_ori", 0.0),
                                check_timeout=float(loc_cfg.get("check_timeout", 30)),
                                retry_interval=float(loc_cfg.get("retry_interval", 10)),
                                max_retries=int(loc_cfg.get("max_retries", 5)),
                            )
                            if not loc_ok:
                                get_error_logger().error(self.robot_name, f"自动导航定位失败: {loc_msg}")
                        else:
                            print(f"⚡ {self.robot_name} 导航定位功能已禁用（可在项目 robot_config.json 的 navigation_localization.auto_set_after_map 中启用）")
                    except Exception as loc_e:
                        get_error_logger().exception_occurred(self.robot_name, "自动设置导航定位", loc_e)
                else:
                    print(f"⚠ {self.robot_name} 导航地图下发未成功: {msg}")
            except Exception as e:
                self._nav_map_ready_success = False
                self._nav_map_ready_message = f"{type(e).__name__}: {e}"
                get_error_logger().exception_occurred(
                    self.robot_name, "自动下发导航地图", e
                )
            finally:
                # 无论成功失败都置位，唤醒等待方
                self._nav_map_ready_event.set()
        
        t = threading.Thread(target=_worker, daemon=True, name=f"{self.robot_name}-set-map")
        t.start()
    
    def wait_for_navigation_map_ready(self, timeout: float = 120.0) -> bool:
        """
        阻塞等待"连接后自动下发导航地图"流程结束。
        
        用于 main.py 启动流程中，确保地图已就绪再激活系统，
        防止任务调度早于地图加载，触发导航失败。
        
        参数:
            timeout: 最长等待秒数。超时仍未结束则返回 False。
        
        返回:
            bool: True = 流程完成（无论是否真下发了地图，只要 _schedule_auto_set_navigation_map
                  已走完就视为"就绪"）；False = 超时未完成。
        
        备注:
            - 下发本身是否成功可通过 _nav_map_ready_success 查询（仅供诊断）。
            - 若未连接 / 未启用 auto_set_on_connect / 未配置 navigation_map，
              事件会立即 set，调用者无感知立刻返回。
        """
        return self._nav_map_ready_event.wait(timeout=timeout)
    
    def stop_reconnect(self):
        """停止重连尝试"""
        self._stop_reconnect = True
        print(f"⏹ {self.robot_name} 设置停止重连标志")
    
    def reset_reconnect(self):
        """重置重连标志，允许重新连接"""
        self._stop_reconnect = False
    
    def _run_event_loop(self):
        """在单独线程中运行事件循环"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._async_connect())
        # 保持事件循环运行以处理后续的异步操作
        self.loop.run_forever()
    
    async def _async_connect(self):
        """异步连接到WebSocket服务器"""
        import socket
        
        # 先进行网络诊断
        print(f"\n=== {self.robot_name} 网络诊断 ===")
        print(f"目标地址: {self._get_address_str()}")
        
        # 1. 检查 DNS 解析（如果是域名）
        try:
            ip_addr = socket.gethostbyname(self.host)
            print(f"✓ DNS 解析成功: {self.host} -> {ip_addr}")
        except socket.gaierror as e:
            print(f"✗ DNS 解析失败: {e}")
            get_error_logger().connection_failed(
                self.robot_name, self.host, self.port, f"DNS解析失败: {e}"
            )
        
        # 2. 检查 TCP 连接（仅在指定端口时）
        if self.port:
            print(f"正在测试 TCP 连接到 {self._get_address_str()}...")
            tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_socket.settimeout(5)
            try:
                tcp_socket.connect((self.host, int(self.port)))
                print(f"✓ TCP 连接成功")
                tcp_socket.close()
            except socket.timeout:
                error_msg = "TCP 连接超时 - 可能被防火墙阻止或服务未运行"
                print(f"✗ {error_msg}")
                get_error_logger().connection_failed(
                    self.robot_name, self.host, self.port, error_msg
                )
                self.connected = False
                return
            except ConnectionRefusedError:
                error_msg = f"连接被拒绝 - 端口 {self.port} 上没有服务监听"
                print(f"✗ {error_msg}")
                get_error_logger().connection_failed(
                    self.robot_name, self.host, self.port, error_msg
                )
                self.connected = False
                return
            except Exception as e:
                error_msg = f"TCP 连接失败: {type(e).__name__}: {e}"
                print(f"✗ {error_msg}")
                get_error_logger().exception_occurred(
                    self.robot_name, "TCP连接", e
                )
                self.connected = False
                return
        else:
            print(f"跳过 TCP 连接测试（WiFi模式，无端口号）")
        
        # 3. 尝试 WebSocket 连接
        print(f"正在建立 WebSocket 连接...")
        try:
            print(f"WebSocket URI: {self._get_uri()}")
            self.websocket = await self._open_websocket()
            self.connected = True
            print(f"✓ {self.robot_name} 已成功连接到 {self._get_address_str()}")
            
        except Exception as e:
            print(f"✗ WebSocket 连接失败:")
            print(f"   错误类型: {type(e).__name__}")
            print(f"   错误信息: {str(e)}")
            import traceback
            traceback.print_exc()
            # 记录WebSocket连接失败
            get_error_logger().exception_occurred(
                self.robot_name, "WebSocket连接", e
            )
            self.connected = False
    
    def is_connected(self):
        """检查连接状态"""
        return self.connected
    
    def send_service_request(
        self,
        service,
        action="",
        type=-1,
        maxtime=600,
        extra_params=None,
    ):
        """
        发送服务请求到机器人，支持自动重连。

        适用于旧 service 契约（action 字段）：
            Request:  action, extra_params
            Response: bool success

        如需使用新契约（task / area / return_params），请改用
        send_service_request_task()。
        """
        if extra_params is None:
            extra_params = {}
        if isinstance(extra_params, str):
            extra_params_str = extra_params
        else:
            extra_params_str = json.dumps(extra_params, ensure_ascii=False)

        return self._do_send_service_request(
            service=service,
            args={"action": action, "extra_params": extra_params_str},
            type=type,
            maxtime=maxtime,
            log_action=action,
        )

    def send_service_request_task(
        self,
        service,
        task="",
        area="",
        maxtime=600,
        extra_params=None,
    ) -> "ServiceTaskResult":
        """
        发送服务请求到机器人（新 service 契约），支持自动重连。

        新 service 契约:
            Request:
                string task
                string area
                string extra_params
            Response:
                bool   success
                string error_msg
                string return_params

        返回 ServiceTaskResult 对象，bool(result) == result.success。
        result.parse_return_params() 可将 return_params 解析为 dict。
        """
        if extra_params is None:
            extra_params = {}
        if isinstance(extra_params, str):
            extra_params_str = extra_params
        else:
            extra_params_str = json.dumps(extra_params, ensure_ascii=False)

        ok = self._do_send_service_request(
            service=service,
            args={"task": task, "area": area, "extra_params": extra_params_str},
            type=-1,
            maxtime=maxtime,
            log_action=task,
        )
        return ServiceTaskResult(
            success=ok,
            error_msg=self.last_service_error_msg,
            return_params=self.last_service_return_params,
        )

    def _do_send_service_request(self, service, args: dict, type: int, maxtime: int, log_action: str) -> bool:
        """
        内部公共实现：连接检查、监听器启动、发送请求、等待结果。
        由 send_service_request 和 send_service_request_task 共用。
        """
        # 如果连接已断开，先尝试重连
        if not self.connected:
            print(f"⚠ {self.robot_name} 连接已断开，尝试重新连接...")
            get_error_logger().warning(self.robot_name, "发送请求前检测到连接断开，尝试重连")
            if not self.connect():
                print(f"✗ {self.robot_name} 重连失败")
                get_error_logger().error(self.robot_name, "重连失败，无法发送请求")
                return False
        with self.mutex:
            # 详细的连接状态检查
            if not self.websocket:
                print(f"✗ {self.robot_name} WebSocket 对象为空，尝试重连...")
                self.connected = False
                self.mutex.release()
                result = self.connect()
                self.mutex.acquire()
                if not result:
                    return False

            if not self.loop:
                print(f"✗ {self.robot_name} 事件循环未初始化，尝试重连...")
                self.connected = False
                self.mutex.release()
                result = self.connect()
                self.mutex.acquire()
                if not result:
                    return False

            if not self.loop.is_running():
                print(f"✗ {self.robot_name} 事件循环未运行，尝试重连...")
                self.connected = False
                self.mutex.release()
                result = self.connect()
                self.mutex.acquire()
                if not result:
                    return False

            print(f"✓ {self.robot_name} 连接状态正常，准备发送请求")

            # 确保消息监听器已启动（用于接收服务响应）
            if not hasattr(self, '_topic_listener_started') or not self._topic_listener_started:
                self._topic_listener_started = True
                self._listener_ready = False
                print(f"[DEBUG] {self.robot_name} 启动消息监听器（用于接收服务响应）")
                asyncio.run_coroutine_threadsafe(
                    self._unified_message_listener(),
                    self.loop
                )
                wait_count = 0
                while not getattr(self, '_listener_ready', False) and wait_count < 10:
                    time.sleep(0.1)
                    wait_count += 1
                if self._listener_ready:
                    print(f"[DEBUG] {self.robot_name} 消息监听器已就绪")
                else:
                    print(f"[WARNING] {self.robot_name} 消息监听器可能未就绪，继续发送请求")

            try:
                request = {"op": "call_service", "service": service, "args": args}
                if type != -1:
                    request["args"]["strawberry"] = {"type": type}

                request_str = json.dumps(request, indent=4)
                print(f"{self.robot_name} sending request:\n{request_str}")

                print(f"[DEBUG] 提交异步任务到事件循环...")
                future = asyncio.run_coroutine_threadsafe(
                    self._async_send_and_receive(request_str, maxtime),
                    self.loop
                )
                # logger.info(f"[{self.robot_name}] 打印请求: {request_str}")

                print(f"[DEBUG] 等待响应（超时{maxtime}秒）...")
                result = future.result(maxtime)
                print(f"[DEBUG] 收到响应结果: {result}")
                # logger.info(f"[{self.robot_name}] 收到响应结果: {result}")

                if result:
                    get_error_logger().request_success(self.robot_name, service, log_action)
                else:
                    get_error_logger().request_failed(self.robot_name, service, log_action, "机器人返回失败")

                return result

            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                print(f"✗ {self.robot_name} 网络异常: {type(e).__name__}: {str(e)}")
                get_error_logger().exception_occurred(self.robot_name, "发送请求", e)
                self.connected = False
                print(f"⚠ {self.robot_name} 检测到网络异常，尝试重连...")
                self.mutex.release()
                reconnect_result = self.connect()
                self.mutex.acquire()
                if reconnect_result:
                    print(f"✓ {self.robot_name} 重连成功，请重试发送请求")
                else:
                    print(f"✗ {self.robot_name} 重连失败")
                return False
            except Exception as e:
                print(f"✗ {self.robot_name} 请求处理异常: {type(e).__name__}: {str(e)}")
                get_error_logger().exception_occurred(self.robot_name, "发送请求", e)
                return False

    async def _async_send_and_receive(self, request_str, maxtime=120):
        """
        异步发送请求并等待响应。
        
        新协议响应格式（chem_project_service / get_strawberry_service 等）：
            {
                "op": "service_response",
                "service": "...",
                "values": {
                    "success": bool,          # 操作是否成功
                    "error_msg": str,         # 失败时的错误信息
                    "return_params": str      # 返回参数（通常为 JSON 字符串）
                },
                "result": bool                # rosbridge 层调用是否成功
            }
        
        判定规则：operation_success = response.result AND values.success
        
        副作用：
            self.last_service_success       ← values.success
            self.last_service_error_msg     ← values.error_msg
            self.last_service_return_params ← values.return_params
            （调用方如需 return_params，请在本方法返回后立即读取对应属性）
        
        返回:
            bool: True=操作成功，False=操作失败/通信/解析异常
        """
        # 先重置上次调用残留，避免脏读
        self.last_service_success = False
        self.last_service_error_msg = ""
        self.last_service_return_params = ""
        
        try:
            # 创建一个Future用于接收响应
            self.service_response_future = asyncio.Future()
            
            print(f"[DEBUG] 发送消息到机器人...")
            await self.websocket.send(request_str)
            print(f"[DEBUG] 消息已发送，等待机器人响应（最长{maxtime}秒）...")
            
            # 通过Future等待统一监听器传递的响应
            response_str = await asyncio.wait_for(self.service_response_future, timeout=maxtime)
            print(f"✓ {self.robot_name} 收到响应:\n{response_str}")
            
            response = json.loads(response_str)
            
            # rosbridge 顶层 result
            result_value = response.get("result", False)
            values = response.get("values")
            
            # rosbridge 调用失败时 values 可能是错误字符串（例如 service 不存在）
            # 属于业务层失败，不是连接问题，直接返回 False
            if not isinstance(values, dict):
                if result_value is False:
                    err = str(values) if values is not None else "(无 values 字段)"
                    self.last_service_error_msg = err
                    print(f"✗ {self.robot_name} 服务调用失败: {err}")
                    get_error_logger().error(self.robot_name, f"服务调用失败: {err}")
                else:
                    print(f"⚠ {self.robot_name} values 非字典格式: {type(values).__name__}={values}")
                return False
            
            # 兼容两种服务响应协议：
            #   新协议字段 "success" (bool)
            #   旧/ROS原生字段 "finish" (bool) — chem_project_service 等使用
            # 优先取 "success"；若不存在则回退到 "finish"
            success_value = bool(values.get("success", values.get("finish", False)))
            error_msg     = values.get("error_msg", "") or ""
            return_params = values.get("return_params", "") or ""
            
            self.last_service_success       = success_value
            self.last_service_error_msg     = error_msg
            self.last_service_return_params = return_params
            
            print(f"[DEBUG] result={result_value}, success={success_value}, "
                  f"error_msg={error_msg!r}, return_params={return_params!r}")
            
            operation_success = bool(result_value) and success_value
            if operation_success:
                print(f"✓ {self.robot_name} 操作成功完成")
                return True
            else:
                reason = error_msg or f"result={result_value}, success={success_value}"
                print(f"✗ {self.robot_name} 操作未完成: {reason}")
                get_error_logger().error(self.robot_name, f"操作未完成: {reason}")
                return False
                
        except asyncio.TimeoutError:
            error_msg = f"读取超时（{maxtime}秒）"
            print(f"✗ {self.robot_name} {error_msg}")
            get_error_logger().error(self.robot_name, error_msg)
            self.connected = False  # 标记为断开
            return False
        except websockets.exceptions.ConnectionClosed as e:
            error_msg = f"WebSocket连接已关闭: {e}"
            print(f"✗ {self.robot_name} {error_msg}")
            get_error_logger().error(self.robot_name, error_msg)
            self.connected = False  # 标记为断开
            return False
        except (ConnectionError, OSError) as e:
            # 真正的网络/IO 异常 → 标记断开
            error_msg = f"网络异常: {type(e).__name__}: {str(e)}"
            print(f"✗ {self.robot_name} {error_msg}")
            get_error_logger().exception_occurred(self.robot_name, "异步通信", e)
            self.connected = False
            return False
        except Exception as e:
            # 业务层/解析异常 → 不标记断开（连接依然可用）
            error_msg = f"异步通信错误: {type(e).__name__}: {str(e)}"
            print(f"✗ {self.robot_name} {error_msg}")
            get_error_logger().exception_occurred(self.robot_name, "异步通信", e)
            return False
    
    def call_service(self, service_name, args=None, timeout=30.0):
        """
        通用 ROS 服务调用（不同于 send_service_request 的业务特化版本）。
        
        直接发送 {"op": "call_service", "service": ..., "args": ...}，
        返回 rosbridge 的完整响应字典。
        
        参数:
            service_name : 服务名，如 "/zj_humanoid/navigation/set_map"
            args         : 服务请求参数字典（可为空）
            timeout      : 等待响应的超时时间（秒）
        
        返回:
            dict: rosbridge 响应完整字典，形如
                  {"op":"service_response","service":"...","values":{...},"result":true}
            None: 连接失败或超时
        """
        args = args or {}
        
        if not self.connected:
            print(f"⚠ {self.robot_name} 连接已断开，尝试重连后调用 {service_name}")
            if not self.connect():
                return None
        
        with self.mutex:
            if not self.websocket or not self.loop or not self.loop.is_running():
                print(f"✗ {self.robot_name} 连接不可用，无法调用 {service_name}")
                return None
            
            # 确保统一监听器已启动
            if not getattr(self, '_topic_listener_started', False):
                self._topic_listener_started = True
                self._listener_ready = False
                asyncio.run_coroutine_threadsafe(
                    self._unified_message_listener(), self.loop
                )
                wait_count = 0
                while not getattr(self, '_listener_ready', False) and wait_count < 10:
                    time.sleep(0.1)
                    wait_count += 1
            
            request = {
                "op": "call_service",
                "service": service_name,
                "args": args,
            }
            request_str = json.dumps(request, ensure_ascii=False)
            print(f"{self.robot_name} call_service → {service_name} args={args}")
            
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_call_service_generic(request_str, timeout),
                    self.loop
                )
                return future.result(timeout + 5)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                print(f"✗ {self.robot_name} 调用 {service_name} 网络异常: {e}")
                get_error_logger().exception_occurred(self.robot_name, "call_service", e)
                self.connected = False
                return None
            except Exception as e:
                print(f"✗ {self.robot_name} 调用 {service_name} 异常: {type(e).__name__}: {e}")
                get_error_logger().exception_occurred(self.robot_name, "call_service", e)
                return None
    
    async def _async_call_service_generic(self, request_str, timeout):
        """通用 service 调用的异步实现"""
        try:
            self.service_response_future = asyncio.Future()
            await self.websocket.send(request_str)
            response_str = await asyncio.wait_for(self.service_response_future, timeout=timeout)
            response = json.loads(response_str)
            print(f"✓ {self.robot_name} service_response: {response_str[:300]}")
            return response
        except asyncio.TimeoutError:
            print(f"✗ {self.robot_name} service_response 超时 ({timeout}s)")
            return None
        except websockets.exceptions.ConnectionClosed as e:
            print(f"✗ {self.robot_name} service 调用期间连接关闭: {e}")
            self.connected = False
            raise
    
    def subscribe_topic(self, topic_name, msg_type="std_msgs/String", throttle_rate=0, queue_length=1, compression="none"):
        """
        订阅ROS topic
        
        参数:
            topic_name: topic名称，如 "/navigation_status"
            msg_type: 消息类型，如 "std_msgs/String" 或 "NavigationStatus"
            throttle_rate: 节流速率（毫秒），0表示不节流
            queue_length: 队列长度
            compression: 压缩方式，可选值：
                - "none": 不压缩（默认）
                - "png": 对图像数据使用PNG压缩
                - "cbor": 使用CBOR编码（适合二进制数据）
                - "cbor-raw": 使用CBOR原始编码
        
        返回:
            bool: 订阅是否成功
        """
        if not self.connected:
            print(f"✗ {self.robot_name} 未连接，无法订阅topic")
            return False
        
        try:
            # 构建订阅请求
            subscribe_request = {
                "op": "subscribe",
                "topic": topic_name,
                "type": msg_type,
                "throttle_rate": throttle_rate,
                "queue_length": queue_length
            }
            
            # 如果指定了压缩方式（对于图像等大数据消息）
            if compression and compression != "none":
                subscribe_request["compression"] = compression
            
            request_str = json.dumps(subscribe_request)
            print(f"{self.robot_name} 订阅topic: {topic_name}")
            print(f"订阅请求: {request_str}")
            
            # 发送订阅请求
            future = asyncio.run_coroutine_threadsafe(
                self.websocket.send(request_str),
                self.loop
            )
            future.result(5)  # 5秒超时
            
            # 记录订阅
            self.subscribed_topics[topic_name] = msg_type
            self.topic_messages[topic_name] = None
            
            # 启动统一消息接收循环（如果尚未启动）
            if not hasattr(self, '_topic_listener_started') or not self._topic_listener_started:
                self._topic_listener_started = True
                print(f"[DEBUG] {self.robot_name} 启动统一消息监听器")
                asyncio.run_coroutine_threadsafe(
                    self._unified_message_listener(),
                    self.loop
                )
            else:
                print(f"[DEBUG] {self.robot_name} 消息监听器已在运行")
            
            print(f"✓ {self.robot_name} 成功订阅topic: {topic_name}")
            return True
            
        except Exception as e:
            print(f"✗ {self.robot_name} 订阅topic失败: {str(e)}")
            get_error_logger().exception_occurred(self.robot_name, f"订阅topic {topic_name}", e)
            return False
    
    def publish_topic(self, topic_name, msg_type="std_msgs/String", msg_data=None):
        """
        发布消息到ROS topic（通过rosbridge）
        
        参数:
            topic_name: topic名称，如 "/navigation_control"
            msg_type: 消息类型，如 "std_msgs/String"
            msg_data: 消息数据，字典格式
                      对于std_msgs/String: {"data": "your_string"}
        
        返回:
            bool: 发布是否成功
        """
        if not self.connected:
            print(f"✗ {self.robot_name} 未连接，无法发布消息")
            return False
        
        if msg_data is None:
            msg_data = {}
        
        try:
            # 构建发布请求（rosbridge协议）
            publish_request = {
                "op": "publish",
                "topic": topic_name,
                "type": msg_type,
                "msg": msg_data
            }
            
            request_str = json.dumps(publish_request)
            print(f"{self.robot_name} 发布消息到topic: {topic_name}")
            print(f"消息类型: {msg_type}")
            print(f"消息内容: {msg_data}")
            
            # 发送发布请求
            future = asyncio.run_coroutine_threadsafe(
                self.websocket.send(request_str),
                self.loop
            )
            future.result(5)  # 5秒超时
            
            print(f"✓ {self.robot_name} 成功发布消息到topic: {topic_name}")
            return True
            
        except Exception as e:
            print(f"✗ {self.robot_name} 发布消息失败: {str(e)}")
            get_error_logger().exception_occurred(self.robot_name, f"发布topic {topic_name}", e)
            return False
    
    def unsubscribe_topic(self, topic_name):
        """
        取消订阅ROS topic
        
        参数:
            topic_name: topic名称
        
        返回:
            bool: 取消订阅是否成功
        """
        if not self.connected:
            print(f"✗ {self.robot_name} 未连接")
            return False
        
        try:
            # 构建取消订阅请求
            unsubscribe_request = {
                "op": "unsubscribe",
                "topic": topic_name
            }
            
            request_str = json.dumps(unsubscribe_request)
            print(f"{self.robot_name} 取消订阅topic: {topic_name}")
            
            # 发送取消订阅请求
            future = asyncio.run_coroutine_threadsafe(
                self.websocket.send(request_str),
                self.loop
            )
            future.result(5)  # 5秒超时
            
            # 从记录中移除
            if topic_name in self.subscribed_topics:
                del self.subscribed_topics[topic_name]
            if topic_name in self.topic_messages:
                del self.topic_messages[topic_name]
            
            print(f"✓ {self.robot_name} 成功取消订阅topic: {topic_name}")
            return True
            
        except Exception as e:
            print(f"✗ {self.robot_name} 取消订阅topic失败: {str(e)}")
            get_error_logger().exception_occurred(self.robot_name, f"取消订阅topic {topic_name}", e)
            return False
    
    async def _unified_message_listener(self):
        """
        统一消息监听器（异步）
        处理所有websocket消息：topic消息和服务响应
        支持网络异常时自动重连
        """
        print(f"[DEBUG] {self.robot_name} 启动统一消息监听器")
        
        # 标记监听器已就绪
        self._listener_ready = True
        reconnect_attempts = 0
        
        # 从配置加载重连参数
        conn_config = _get_connection_config()
        max_reconnect_attempts = conn_config["reconnect_max_attempts"]  # 最大重连尝试次数，None为无限
        reconnect_interval = conn_config["reconnect_interval"]  # 重连间隔（秒）
        
        while not self._stop_reconnect:
            try:
                while self.connected and self.websocket:
                    try:
                        # 接收消息
                        message_str = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
                        message = json.loads(message_str)
                        
                        # 重置重连计数（成功接收消息）
                        reconnect_attempts = 0
                        
                        # 检查消息类型
                        op = message.get("op")
                        
                        if op == "publish":
                            # Topic消息
                            topic_name = message.get("topic")
                            msg_data = message.get("msg")
                            
                            if topic_name in self.subscribed_topics:
                                # 存储最新消息
                                self.topic_messages[topic_name] = msg_data
                        
                        elif op in ("action_feedback", "action_result"):
                            # Action 反馈或结果 —— 交给 Action 处理器
                            self._handle_action_message(message)
                        
                        else:
                            # 服务响应或其他消息
                            if self.service_response_future and not self.service_response_future.done():
                                # 将响应传递给等待的协程
                                self.service_response_future.set_result(message_str)
                        
                    except asyncio.TimeoutError:
                        # 超时是正常的，继续循环
                        continue
                    except websockets.exceptions.ConnectionClosed as e:
                        print(f"[DEBUG] {self.robot_name} WebSocket连接已关闭: {e}")
                        raise  # 抛出以触发重连
                    except Exception as e:
                        error_str = str(e)
                        # 检查是否是网络相关错误，需要重连
                        if "keepalive" in error_str.lower() or "ping" in error_str.lower() or \
                           "connection" in error_str.lower() or "closed" in error_str.lower():
                            print(f"[DEBUG] {self.robot_name} 网络异常，准备重连: {e}")
                            raise  # 抛出以触发重连
                        else:
                            print(f"[DEBUG] {self.robot_name} 消息监听器错误: {e}")
                            # 如果有等待的future，设置异常
                            if self.service_response_future and not self.service_response_future.done():
                                self.service_response_future.set_exception(e)
                            # 非网络错误，短暂等待后继续
                            await asyncio.sleep(0.5)
                            continue
                
                # 连接已断开，尝试重连
                if not self._stop_reconnect:
                    raise Exception("连接已断开")
                    
            except Exception as e:
                # 连接异常，尝试重连
                if self._stop_reconnect:
                    print(f"[DEBUG] {self.robot_name} 收到停止信号，退出监听器")
                    break
                
                reconnect_attempts += 1
                
                # 检查是否超过最大重连次数
                if max_reconnect_attempts is not None and reconnect_attempts > max_reconnect_attempts:
                    print(f"✗ {self.robot_name} 监听器重连失败：已达最大重试次数 ({max_reconnect_attempts})")
                    get_error_logger().connection_failed(
                        self.robot_name, self.host, self.port,
                        f"监听器重连失败，已重试 {max_reconnect_attempts} 次"
                    )
                    break

                # 前 3 次详细打，之后大约每分钟一条，避免 mock HTTP 400 时刷屏
                log_this = reconnect_attempts <= 3 or reconnect_attempts % 12 == 0
                cap = max_reconnect_attempts or "∞"
                if log_this:
                    omitted = "" if reconnect_attempts <= 3 else "，同类失败已省略"
                    print(
                        f"⚠ {self.robot_name} 连接异常 ({e})，"
                        f"{reconnect_interval}秒后尝试重连 [{reconnect_attempts}/{cap}]{omitted}"
                    )
                
                self.connected = False
                
                waited = 0.0
                while waited < float(reconnect_interval):
                    if self._stop_reconnect:
                        print(f"[DEBUG] {self.robot_name} 收到停止信号，退出监听器")
                        break
                    step = min(0.2, float(reconnect_interval) - waited)
                    await asyncio.sleep(step)
                    waited += step
                if self._stop_reconnect:
                    break
                
                if await self._async_reconnect():
                    print(f"✓ {self.robot_name} 监听器重连成功")
                    # 重新订阅之前的topics
                    await self._resubscribe_topics()
                    # 重连后重新下发导航地图
                    self._schedule_auto_set_navigation_map()
                elif log_this:
                    err = getattr(self, "_last_reconnect_error", "") or "未知原因"
                    print(f"✗ {self.robot_name} 监听器重连失败: {err}")
        
        print(f"[DEBUG] {self.robot_name} 统一消息监听器已停止")
        self._topic_listener_started = False
        self._listener_ready = False
    
    async def _open_websocket(self, quiet: bool = False):
        """
        建立 WebSocket。必须先带 rosbridge_v2：mock_rosbridge 要求该子协议，
        缺了会被 websockets 服务端直接 HTTP 400 拒掉。
        """
        uri = self._get_uri()
        conn_config = _get_connection_config()
        kwargs = {
            "ping_interval": conn_config["ping_interval"],
            "ping_timeout": conn_config["ping_timeout"],
            "close_timeout": 10,
        }
        try:
            ws = await websockets.connect(uri, subprotocols=["rosbridge_v2"], **kwargs)
            if not quiet:
                print("✓ WebSocket 连接成功 (使用 rosbridge_v2 协议)")
            return ws
        except Exception:
            if not quiet:
                print("rosbridge_v2 协议失败，尝试标准 WebSocket...")
            ws = await websockets.connect(uri, **kwargs)
            if not quiet:
                print("✓ WebSocket 连接成功 (标准协议)")
            return ws

    async def _async_reconnect(self) -> bool:
        """
        异步重新连接WebSocket
        
        Returns:
            bool: 是否连接成功
        """
        if self._stop_reconnect:
            return False
        try:
            if self.websocket:
                try:
                    await self.websocket.close()
                except Exception:
                    pass
            self.websocket = await self._open_websocket(quiet=True)
            self.connected = True
            self._last_reconnect_error = ""
            return True
        except Exception as e:
            self._last_reconnect_error = str(e)
            self.connected = False
            return False
    
    async def _resubscribe_topics(self):
        """
        重新订阅之前的topics
        """
        if not self.subscribed_topics:
            return
        
        print(f"[DEBUG] {self.robot_name} 重新订阅 {len(self.subscribed_topics)} 个topics...")
        
        # 复制订阅列表，因为订阅过程可能会修改它
        topics_to_subscribe = dict(self.subscribed_topics)
        self.subscribed_topics.clear()
        
        for topic_name, msg_type in topics_to_subscribe.items():
            try:
                subscribe_msg = {
                    "op": "subscribe",
                    "topic": topic_name,
                    "type": msg_type,
                    "throttle_rate": 0,
                    "queue_length": 1
                }
                await self.websocket.send(json.dumps(subscribe_msg))
                self.subscribed_topics[topic_name] = msg_type
                print(f"  ✓ 重新订阅: {topic_name}")
            except Exception as e:
                print(f"  ✗ 重新订阅失败 {topic_name}: {e}")
    
    def get_topic_message(self, topic_name, msg_type=ROSTopicMessageType.NAVIGATION_STATUS, sleep_time=3):
        """
        获取topic的最新消息
        
        参数:
            topic_name: topic名称
        
        返回:
            dict: 最新的消息数据，如果没有则返回None
        """
        msg = self.topic_messages.get(topic_name)
        if msg is None:
            # 检查是否已订阅
            if topic_name not in self.subscribed_topics:
                print(f"[DEBUG] {self.robot_name} topic {topic_name} 未订阅")
                # 重新订阅topic
                logger.info("机器人控制器", f"重新订阅topic: {topic_name}")
                print(f"[DEBUG] 开始重新订阅topic: {topic_name}")
                
                subscribe_success = self.subscribe_topic(
                    topic_name=topic_name,
                    msg_type=msg_type,
                    throttle_rate=0,
                    queue_length=1
                )
                
                if subscribe_success:
                    logger.info("机器人控制器", "重新订阅成功")
                    print("✓ Topic重新订阅成功")
                    print(f"[DEBUG] 等待{sleep_time}秒让topic消息开始传输...")
                    time.sleep(sleep_time)  # 等待订阅生效并接收第一条消息
                else:
                    logger.error("机器人控制器", "重新订阅失败")
                    print("✗ Topic重新订阅失败")
                    return None
            '''else:
                print(f"[DEBUG] {self.robot_name} topic {topic_name} 已订阅但没有收到消息")'''
        return msg
    
    # ==================== ROS Action 调用方法 ====================

    def send_action(
        self,
        action_name: str,
        action_type: str,
        goal: dict,
        timeout: float = 300.0,
        feedback_callback=None,
        retry_on_disconnect: bool = True,
    ) -> dict:
        """
        发送 ROS Action 目标并等待结果（支持断线重连）。

        复用已建立的 WebSocket 连接，通过 _unified_message_listener 接收反馈和结果。

        参数:
            action_name       : Action 服务器名称，如 "/zj_humanoid/navigation/task_info"
            action_type       : Action 消息类型，如 "navigation/NavigationAction"
            goal              : Goal 数据字典
            timeout           : 超时时间（秒）
            feedback_callback : 反馈回调函数，接收原始 feedback dict
            retry_on_disconnect: 断线时是否重连后重试

        返回:
            dict:
                - success: bool
                - state: str  (SUCCEEDED/FAILED/CANCELLED/ABORTED/TIMEOUT)
                - result: dict  原始结果数据
                - error: str    错误信息

        示例:
            result = robot.send_action(
                action_name="/zj_humanoid/navigation/task_info",
                action_type="navigation/NavigationAction",
                goal={"waypoints": [...], "task_type": {"value": 0}},
                timeout=120,
            )
            if result["success"]:
                print("Action 执行成功")
        """
        if not self.connected:
            if retry_on_disconnect:
                print(f"⚠ {self.robot_name} 连接已断开，尝试重连...")
                if not self.connect():
                    return {"success": False, "state": "ABORTED", "result": None, "error": "重连失败"}
            else:
                return {"success": False, "state": "ABORTED", "result": None, "error": "未连接"}

        self._ensure_listener_started()

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_send_action(action_name, action_type, goal, timeout, feedback_callback),
                self.loop
            )
            return future.result(timeout + 15)
        except Exception as e:
            error_msg = f"Action 执行异常: {e}"
            print(f"✗ {self.robot_name} {error_msg}")
            get_error_logger().exception_occurred(self.robot_name, "发送Action", e)
            if retry_on_disconnect and not self.connected:
                print(f"⚠ {self.robot_name} 检测到连接异常，尝试重连...")
                self.connect()
            return {"success": False, "state": "ABORTED", "result": None, "error": error_msg}

    async def _async_send_action(
        self,
        action_name: str,
        action_type: str,
        goal: dict,
        timeout: float,
        feedback_callback,
    ) -> dict:
        """异步发送 Action 并等待结果（内部）"""
        result = {"success": False, "state": "ABORTED", "result": None, "error": None}

        try:
            action_msg = {
                "op": "publish",
                "action": action_name,
                "action_type": action_type,
                "args": goal,
            }

            print(f"📤 {self.robot_name} 发送 Action: {action_name}")
            await self.websocket.send(json.dumps(action_msg))

            # 用 get_event_loop().create_future() 创建 Future，
            # 不用 asyncio.wait_for 包裹，避免超时后 Future 被取消导致结果丢失
            loop = asyncio.get_event_loop()
            action_future = loop.create_future()
            self._action_response_future = action_future
            self._action_feedback_callback = feedback_callback
            self._current_action_name = action_name

            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    print(f"⏱️ {self.robot_name} Action 超时，发送取消")
                    await self._async_cancel_action(action_name)
                    result = {"success": False, "state": "TIMEOUT", "result": None, "error": "执行超时"}
                    break

                if action_future.done():
                    if action_future.cancelled():
                        result = {"success": False, "state": "ABORTED", "result": None, "error": "Future 被取消"}
                    else:
                        result = action_future.result()
                    break

                # 让出事件循环，让监听器有机会接收并处理消息
                await asyncio.sleep(0.1)

        except websockets.exceptions.ConnectionClosed as e:
            error_msg = f"连接断开: {e}"
            print(f"✗ {self.robot_name} {error_msg}")
            self.connected = False
            result = {"success": False, "state": "ABORTED", "result": None, "error": error_msg}
        except Exception as e:
            error_msg = f"Action 异常: {e}"
            print(f"✗ {self.robot_name} {error_msg}")
            result = {"success": False, "state": "ABORTED", "result": None, "error": error_msg}
        finally:
            self._action_response_future = None
            self._action_feedback_callback = None
            self._current_action_name = None

        return result

    def cancel_action(self, action_name: str) -> bool:
        """
        取消正在执行的 ROS Action。

        示例:
            robot.cancel_action("/zj_humanoid/navigation/task_info")
        """
        if not self.connected:
            print(f"✗ {self.robot_name} 未连接，无法取消 Action")
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_cancel_action(action_name),
                self.loop
            )
            return future.result(5)
        except Exception as e:
            print(f"✗ {self.robot_name} 取消 Action 失败: {e}")
            return False

    async def _async_cancel_action(self, action_name: str) -> bool:
        """异步发送取消 Action 消息"""
        try:
            cancel_msg = {"op": "cancel_action", "action": action_name}
            await self.websocket.send(json.dumps(cancel_msg))
            print(f"🛑 {self.robot_name} 已发送取消 Action: {action_name}")
            return True
        except Exception as e:
            print(f"✗ {self.robot_name} 取消 Action 失败: {e}")
            return False

    def _ensure_listener_started(self):
        """确保统一消息监听器已启动"""
        if not getattr(self, '_topic_listener_started', False):
            self._topic_listener_started = True
            self._listener_ready = False
            print(f"[DEBUG] {self.robot_name} 启动消息监听器")
            asyncio.run_coroutine_threadsafe(
                self._unified_message_listener(),
                self.loop
            )
            wait_count = 0
            while not getattr(self, '_listener_ready', False) and wait_count < 20:
                time.sleep(0.1)
                wait_count += 1

    def _handle_action_message(self, message: dict) -> bool:
        """
        处理 action_feedback / action_result 消息（由 _unified_message_listener 调用）。

        返回 True 表示已处理，False 表示不是当前等待的 Action 消息。
        """
        op = message.get("op", "")
        action_name = message.get("action", "")

        current_action = getattr(self, '_current_action_name', None)
        if not current_action or action_name != current_action:
            return False

        if op == "action_feedback":
            raw_feedback = message.get("values", {})
            state_val = raw_feedback.get("state", {}).get("value", 0)
            state_name = self._get_action_state_name(state_val)
            print(f"📶 {self.robot_name} Action 反馈: state={state_name}")

            callback = getattr(self, '_action_feedback_callback', None)
            if callback:
                try:
                    callback(raw_feedback)
                except Exception as e:
                    print(f"[WARNING] {self.robot_name} Action 反馈回调异常: {e}")
            return True

        elif op == "action_result":
            values = message.get("values", {})
            state_val = values.get("state", {}).get("value", 0)
            state_name = self._get_action_state_name(state_val)
            # print(f"🏁 {self.robot_name} Action 完成: state={state_name}")

            success = (state_val == 2)  # SUCCEEDED = 2
            result = {
                "success": success,
                "state": state_name,
                "result": values,
                "error": None if success else f"Action 状态: {state_name}",
            }

            action_future = getattr(self, '_action_response_future', None)
            if action_future and not action_future.done():
                action_future.set_result(result)
            return True

        return False

    def _get_action_state_name(self, state_val: int) -> str:
        """将整数状态值转为可读名称"""
        return {
            0: "NONE",
            1: "RUNNING",
            2: "SUCCEEDED",
            3: "FAILED",
            4: "CANCELLED",
            5: "ABORTED",
        }.get(state_val, f"UNKNOWN({state_val})")

    # ==================== 连接关闭 ====================

    def close(self):
        """关闭连接并停止事件循环 / 监听重连。未连接时也要停线程，否则会一直刷重连日志。"""
        self.stop_reconnect()
        websocket = None
        loop = None
        thread = None
        with self.mutex:
            self.connected = False
            websocket = self.websocket
            self.websocket = None
            loop = self.loop
            thread = self.thread

        if websocket is not None and loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(websocket.close(), loop)
                future.result(2)
            except Exception as e:
                print(f"{self.robot_name} close error: {str(e)}")
            print(f"{self.robot_name} disconnected from {self.host}:{self.port}")

        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=3.0)
