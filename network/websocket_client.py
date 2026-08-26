"""
WebSocket客户端模块
作为客户端主动连接外部WebSocket服务器

功能：
- 连接到外部WebSocket服务器
- 主动发送 Client ID 进行注册（服务端使用此ID与我们通信）
- 支持 ws:// 和 wss:// 协议
- 自动重连机制
- 心跳保活
- 接收服务端推送的消息并处理

Client ID 通信流程：
1. 客户端连接服务器
2. 客户端主动发送注册消息: {"type": "register", "client_id": "xxx", "timestamp": "..."}
3. 服务端记录此 client_id，后续使用它与客户端通信
4. 客户端后续发送的所有消息都携带此 client_id

配置示例 (robot_config.json):
{
    "websocket_client": {
        "enabled": true,
        "server_host": "192.168.1.100",
        "server_port": 8091,
        "ssl_enabled": false,
        "client_id": "robot_connect_client",
        "reconnect_interval": 5,
        "reconnect_max_attempts": null,
        "heartbeat_interval": 30
    }
}
"""

import json
import asyncio
import ssl
import threading
import time
from typing import Dict, Callable, Optional, Any
from datetime import datetime

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("警告: websockets 模块未安装，WebSocket客户端功能不可用")

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()

# 默认配置
DEFAULT_CONFIG = {
    "enabled": False,
    "server_host": "127.0.0.1",
    "server_port": 8091,
    "server_path": "/",
    "ssl_enabled": False,
    "client_id": "robot_connect_client",
    "auth_type": "none",           # none, basic, bearer, token
    "auth_token": None,            # 用于 bearer/token 认证
    "auth_username": None,         # 用于 basic 认证
    "auth_password": None,         # 用于 basic 认证
    "reconnect_interval": 5,
    "reconnect_max_attempts": None,
    "heartbeat_interval": 30
}


def _get_websocket_client_config() -> Dict:
    """获取WebSocket客户端配置（优先使用外部配置）"""
    try:
        from infrastructure.config_loader import load_config
        config = load_config()
        external_config = config.get("websocket_client", {})
        if external_config:
            return {
                "enabled": external_config.get("enabled", DEFAULT_CONFIG["enabled"]),
                "server_host": external_config.get("server_host", DEFAULT_CONFIG["server_host"]),
                "server_port": external_config.get("server_port", DEFAULT_CONFIG["server_port"]),
                "server_path": external_config.get("server_path", DEFAULT_CONFIG["server_path"]),
                "ssl_enabled": external_config.get("ssl_enabled", DEFAULT_CONFIG["ssl_enabled"]),
                "client_id": external_config.get("client_id", DEFAULT_CONFIG["client_id"]),
                "auth_type": external_config.get("auth_type", DEFAULT_CONFIG["auth_type"]),
                "auth_token": external_config.get("auth_token", DEFAULT_CONFIG["auth_token"]),
                "auth_username": external_config.get("auth_username", DEFAULT_CONFIG["auth_username"]),
                "auth_password": external_config.get("auth_password", DEFAULT_CONFIG["auth_password"]),
                "reconnect_interval": external_config.get("reconnect_interval", DEFAULT_CONFIG["reconnect_interval"]),
                "reconnect_max_attempts": external_config.get("reconnect_max_attempts", DEFAULT_CONFIG["reconnect_max_attempts"]),
                "heartbeat_interval": external_config.get("heartbeat_interval", DEFAULT_CONFIG["heartbeat_interval"]),
            }
    except Exception as e:
        logger.error("WebSocket客户端", f"加载配置失败: {e}")
    
    return DEFAULT_CONFIG.copy()


class WebSocketClient:
    """
    WebSocket客户端
    主动连接外部WebSocket服务器，使用Client ID方式通信
    
    支持的认证方式 (auth_type):
    - none: 无认证（默认）
    - basic: HTTP Basic认证 (Authorization: Basic base64(user:pass))
    - bearer: Bearer Token认证 (Authorization: Bearer xxx)
    - token: URL参数认证 (?token=xxx)
    """
    
    def __init__(self):
        if not WEBSOCKETS_AVAILABLE:
            raise RuntimeError("websockets 模块未安装")
        
        self._config = _get_websocket_client_config()
        self._enabled = self._config["enabled"]
        self._server_host = self._config["server_host"]
        self._server_port = self._config["server_port"]
        self._server_path = self._config.get("server_path", "/")
        self._ssl_enabled = self._config["ssl_enabled"]
        self._client_id = self._config["client_id"]
        self._reconnect_interval = self._config["reconnect_interval"]
        self._reconnect_max_attempts = self._config["reconnect_max_attempts"]
        self._heartbeat_interval = self._config["heartbeat_interval"]
        
        # 认证配置
        self._auth_type = self._config.get("auth_type", "none")
        self._auth_token = self._config.get("auth_token")
        self._auth_username = self._config.get("auth_username")
        self._auth_password = self._config.get("auth_password")
        
        self._websocket = None
        self._connected = False
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 消息处理回调
        self._message_callback: Optional[Callable[[Dict], None]] = None
        
        # 命令响应等待
        self._pending_responses: Dict[str, asyncio.Future] = {}
        
        # 重连计数
        self._reconnect_count = 0
    
    @property
    def uri(self) -> str:
        """获取WebSocket URI（不含认证参数）"""
        protocol = "wss" if self._ssl_enabled else "ws"
        path = self._server_path if self._server_path.startswith("/") else f"/{self._server_path}"
        return f"{protocol}://{self._server_host}:{self._server_port}{path}"
    
    def _get_connect_uri(self) -> str:
        """获取带认证参数的连接URI"""
        base_uri = self.uri
        # 如果是 token 认证，添加到 URL 参数
        if self._auth_type == "token" and self._auth_token:
            separator = "&" if "?" in base_uri else "?"
            return f"{base_uri}{separator}token={self._auth_token}"
        return base_uri
    
    def _get_auth_headers(self) -> list:
        """
        获取认证HTTP头（返回列表格式，兼容websockets库）
        
        参考代码:
            credentials = f"{username}:{password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            header=[f"Authorization: Basic {encoded}"]
        """
        import base64
        headers = []
        
        if self._auth_type == "basic" and self._auth_username and self._auth_password:
            # Basic认证: base64(username:password)
            credentials = f"{self._auth_username}:{self._auth_password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers.append(f"Authorization: Basic {encoded}")
            
        elif self._auth_type == "bearer" and self._auth_token:
            # Bearer Token认证
            headers.append(f"Authorization: Bearer {self._auth_token}")
        
        return headers if headers else None
    
    async def _create_connection(self, uri: str, ssl_context):
        """
        创建WebSocket连接（支持认证和非认证模式）
        
        解耦设计：
        - 无认证时：直接使用标准连接，不传递额外参数
        - 有认证时：尝试多种方式传递认证头，兼容不同版本websockets库
        """
        auth_headers = self._get_auth_headers()
        
        # 无需认证 - 使用最简单的连接方式
        if not auth_headers:
            return await websockets.connect(
                uri,
                ssl=ssl_context,
                ping_interval=self._heartbeat_interval,
                ping_timeout=self._heartbeat_interval * 2
            )
        
        # 需要认证 - 尝试不同的参数名（兼容不同版本的websockets库）
        # 将 headers 列表转换为字典格式
        headers_dict = {}
        for header in auth_headers:
            if ": " in header:
                key, value = header.split(": ", 1)
                headers_dict[key] = value
        
        # 尝试方式1: extra_headers (websockets 10.x+)
        try:
            return await websockets.connect(
                uri,
                ssl=ssl_context,
                ping_interval=self._heartbeat_interval,
                ping_timeout=self._heartbeat_interval * 2,
                extra_headers=headers_dict
            )
        except TypeError as e:
            if "extra_headers" not in str(e):
                raise
        
        # 尝试方式2: additional_headers (某些版本)
        try:
            return await websockets.connect(
                uri,
                ssl=ssl_context,
                ping_interval=self._heartbeat_interval,
                ping_timeout=self._heartbeat_interval * 2,
                additional_headers=headers_dict
            )
        except TypeError as e:
            if "additional_headers" not in str(e):
                raise
        
        # 尝试方式3: 不传递headers，连接后再处理（最后的fallback）
        logger.warning("WebSocket客户端", "当前websockets库版本不支持headers参数，尝试无认证连接")
        return await websockets.connect(
            uri,
            ssl=ssl_context,
            ping_interval=self._heartbeat_interval,
            ping_timeout=self._heartbeat_interval * 2
        )
    
    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected and self._websocket is not None
    
    @property
    def client_id(self) -> str:
        """获取配置的client_id（主动注册时使用）"""
        return self._client_id
    
    def set_message_callback(self, callback: Callable[[Dict], None]):
        """
        设置消息处理回调函数
        当收到服务端推送的消息时调用
        
        Args:
            callback: 接收一个Dict参数的回调函数
        """
        self._message_callback = callback
    
    def start(self):
        """启动WebSocket客户端（在后台线程运行）"""
        if not self._enabled:
            logger.info("WebSocket客户端", "WebSocket客户端已禁用")
            print("ℹ️  WebSocket客户端已禁用")
            return
        
        if self._running:
            logger.warning("WebSocket客户端", "客户端已在运行中")
            return
        
        self._running = True
        self._stop_event.clear()
        
        self._thread = threading.Thread(target=self._run_client, daemon=True)
        self._thread.start()
        
        logger.info("WebSocket客户端", f"客户端启动，目标服务器: {self.uri}")
        print(f"🔌 WebSocket客户端启动，目标: {self.uri}")
    
    def stop(self):
        """停止WebSocket客户端"""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        
        # 关闭连接
        if self._loop and self._websocket:
            asyncio.run_coroutine_threadsafe(self._close_connection(), self._loop)
        
        # 等待线程结束
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        logger.info("WebSocket客户端", "客户端已停止")
        print("⏹️  WebSocket客户端已停止")
    
    def _run_client(self):
        """在独立线程中运行客户端事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._client_loop())
        except Exception as e:
            logger.exception_occurred("WebSocket客户端", "事件循环异常", e)
        finally:
            self._loop.close()
            self._loop = None
    
    async def _client_loop(self):
        """客户端主循环（包含自动重连）"""
        while self._running and not self._stop_event.is_set():
            try:
                await self._connect_and_run()
            except Exception as e:
                if self._running:
                    # logger.error("WebSocket客户端", f"连接异常: {e}")
                    print(f"⚠️  WebSocket连接异常: {e}")
            
            # 检查是否需要重连
            if self._running and not self._stop_event.is_set():
                # 检查重连次数限制
                if self._reconnect_max_attempts is not None:
                    if self._reconnect_count >= self._reconnect_max_attempts:
                        logger.error("WebSocket客户端", f"达到最大重连次数 {self._reconnect_max_attempts}，停止重连")
                        print(f"❌ 达到最大重连次数，停止重连")
                        break
                
                self._reconnect_count += 1
                logger.info("WebSocket客户端", f"将在 {self._reconnect_interval} 秒后重连 (第 {self._reconnect_count} 次)")
                print(f"🔄 将在 {self._reconnect_interval} 秒后重连...")
                
                # 等待重连间隔
                await asyncio.sleep(self._reconnect_interval)
    
    async def _connect_and_run(self):
        """连接到服务器并运行消息处理"""
        # SSL配置
        ssl_context = None
        if self._ssl_enabled:
            ssl_context = ssl.create_default_context()
            # 自签名证书需要禁用验证（生产环境应使用有效证书）
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        
        # 获取连接URI
        connect_uri = self._get_connect_uri()
        
        logger.info("WebSocket客户端", f"正在连接: {self.uri}")
        print(f"🔗 正在连接: {self.uri}")
        if self._auth_type != "none":
            print(f"   认证方式: {self._auth_type}")
        
        # 创建连接（区分认证和非认证模式）
        websocket = await self._create_connection(connect_uri, ssl_context)
        
        try:
            self._websocket = websocket
            self._connected = True
            self._reconnect_count = 0  # 重置重连计数
            
            logger.info("WebSocket客户端", "连接成功")
            print(f"✓ WebSocket连接成功: {self.uri}")
            
            # 主动发送注册消息，告知服务器我们的client_id
            register_msg = {
                "type": "register",
                "client_id": self._client_id,
                "timestamp": datetime.now().isoformat()
            }
            await websocket.send(json.dumps(register_msg, ensure_ascii=False))
            logger.info("WebSocket客户端", f"已发送注册消息，Client ID: {self._client_id}")
            print(f"   已注册 Client ID: {self._client_id}")
            
            # 等待服务器确认（可选，某些服务器可能会回复确认）
            try:
                ack_msg = await asyncio.wait_for(websocket.recv(), timeout=5)
                # 部分服务器会回空帧（0 字节 / 空字符串），直接忽略
                if not ack_msg or (isinstance(ack_msg, (str, bytes)) and len(ack_msg) == 0):
                    logger.info("WebSocket客户端", "服务端返回空帧，忽略（正常）")
                else:
                    ack_data = json.loads(ack_msg)
                    if ack_data.get("type") == "register_ack":
                        logger.info("WebSocket客户端", f"服务端已确认注册: {ack_data}")
                        print(f"   服务端确认: {ack_data.get('message', 'OK')}")
                    else:
                        # 如果不是确认消息，当作普通消息处理
                        await self._handle_message(ack_data)
            except asyncio.TimeoutError:
                # 服务器可能不回复确认，这是正常的
                logger.info("WebSocket客户端", "服务端未发送注册确认（正常）")
            except json.JSONDecodeError as e:
                # 服务端返回了非 JSON 内容（如纯文本），记录原始内容但不中断连接
                logger.warning("WebSocket客户端", f"注册确认非JSON内容，忽略: {e}")
            except Exception as e:
                logger.warning("WebSocket客户端", f"解析服务端响应失败: {e}")
            
            # 消息处理循环
            await self._message_loop(websocket)
        finally:
            # 确保连接关闭
            if websocket and not websocket.closed:
                await websocket.close()
    
    async def _message_loop(self, websocket):
        """消息处理循环"""
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    logger.warning("WebSocket客户端", f"JSON解析失败: {e}")
                except Exception as e:
                    logger.exception_occurred("WebSocket客户端", "处理消息异常", e)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("WebSocket客户端", f"连接关闭: code={e.code}, reason={e.reason}")
            print(f"⚠️  连接关闭: {e.reason or e.code}")
        finally:
            self._connected = False
            self._websocket = None
    
    async def _handle_message(self, data: Dict):
        """处理收到的消息"""
        msg_type = data.get("type")
        cmd_id = data.get("cmd_id")
        
        # 处理心跳响应
        if msg_type == "pong":
            return
        
        # 处理命令响应（如果有等待的请求）
        if cmd_id and cmd_id in self._pending_responses:
            future = self._pending_responses.pop(cmd_id)
            if not future.done():
                future.set_result(data)
            return
        
        # 调用用户设置的回调处理其他消息
        # _message_callback 内部会调用 handle_command（含 ROS topic 轮询）
        # 以及 websocket_client.send()（内部用 run_coroutine_threadsafe 提交到本事件循环）。
        # 若在 async 协程里直接同步调用，事件循环被阻塞，send() 提交的协程永远无法执行，
        # 导致 future.result(timeout=5) 精确等待 5 秒后超时 —— 这就是固定 5s 延迟的根本原因。
        # 使用 run_in_executor 将回调放到线程池执行，事件循环保持空闲，send() 可正常调度。
        if self._message_callback:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._message_callback, data)
            except Exception as e:
                logger.exception_occurred("WebSocket客户端", "消息回调异常", e)
        else:
            # 默认打印收到的消息
            logger.info("WebSocket客户端", f"收到消息: {data}")
            print(f"📨 收到服务端消息: {json.dumps(data, ensure_ascii=False)[:200]}")
    
    async def _close_connection(self):
        """关闭连接"""
        if self._websocket:
            try:
                await self._websocket.close()
            except:
                pass
    
    async def _send_async(self, data: Dict) -> bool:
        """异步发送消息"""
        if not self._connected or not self._websocket:
            logger.warning("WebSocket客户端", "未连接，无法发送消息")
            return False
        
        try:
            # 自动添加client_id
            if "client_id" not in data:
                data["client_id"] = self.client_id
            
            await self._websocket.send(json.dumps(data, ensure_ascii=False))
            return True
        except Exception as e:
            logger.error("WebSocket客户端", f"发送消息失败: {e}")
            return False
    
    def send(self, data: Dict) -> bool:
        """
        发送消息到服务器（同步接口）
        
        Args:
            data: 要发送的消息字典
            
        Returns:
            bool: 是否发送成功
        """
        if not self._loop or not self._connected:
            logger.warning("WebSocket客户端", "未连接，无法发送消息")
            return False
        
        future = asyncio.run_coroutine_threadsafe(self._send_async(data), self._loop)
        try:
            return future.result(timeout=5)
        except Exception as e:
            logger.error("WebSocket客户端", f"发送消息失败: {e}")
            return False
    
    def send_command(self, cmd_type: str, cmd_id: str = None, params: Dict = None) -> bool:
        """
        发送命令到服务器
        
        Args:
            cmd_type: 命令类型
            cmd_id: 命令ID（可选，自动生成）
            params: 命令参数
            
        Returns:
            bool: 是否发送成功
        """
        if cmd_id is None:
            cmd_id = f"{cmd_type.lower()}_{int(time.time()*1000)}"
        
        command = {
            "cmd_type": cmd_type,
            "cmd_id": cmd_id,
            "params": params or {},
            "client_id": self.client_id
        }
        
        return self.send(command)
    
    async def _send_and_wait_async(self, data: Dict, timeout: float = 30) -> Optional[Dict]:
        """异步发送消息并等待响应"""
        if not self._connected or not self._websocket:
            return None
        
        cmd_id = data.get("cmd_id")
        if not cmd_id:
            cmd_id = f"cmd_{int(time.time()*1000)}"
            data["cmd_id"] = cmd_id
        
        # 创建Future等待响应
        future = self._loop.create_future()
        self._pending_responses[cmd_id] = future
        
        try:
            # 发送消息
            if not await self._send_async(data):
                return None
            
            # 等待响应
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            logger.warning("WebSocket客户端", f"等待响应超时: cmd_id={cmd_id}")
            return None
        finally:
            self._pending_responses.pop(cmd_id, None)
    
    def send_and_wait(self, cmd_type: str, cmd_id: str = None, params: Dict = None, timeout: float = 30) -> Optional[Dict]:
        """
        发送命令并等待响应（同步接口）
        
        Args:
            cmd_type: 命令类型
            cmd_id: 命令ID
            params: 命令参数
            timeout: 超时时间（秒）
            
        Returns:
            响应数据字典，超时返回None
        """
        if not self._loop or not self._connected:
            logger.warning("WebSocket客户端", "未连接，无法发送消息")
            return None
        
        if cmd_id is None:
            cmd_id = f"{cmd_type.lower()}_{int(time.time()*1000)}"
        
        command = {
            "cmd_type": cmd_type,
            "cmd_id": cmd_id,
            "params": params or {},
            "client_id": self.client_id
        }
        
        future = asyncio.run_coroutine_threadsafe(
            self._send_and_wait_async(command, timeout), 
            self._loop
        )
        try:
            return future.result(timeout=timeout + 5)
        except Exception as e:
            logger.error("WebSocket客户端", f"发送命令失败: {e}")
            return None
    
    def reload_config(self):
        """重新加载配置"""
        self._config = _get_websocket_client_config()
        self._enabled = self._config["enabled"]
        self._server_host = self._config["server_host"]
        self._server_port = self._config["server_port"]
        self._server_path = self._config.get("server_path", "/")
        self._ssl_enabled = self._config["ssl_enabled"]
        self._client_id = self._config["client_id"]
        self._auth_type = self._config.get("auth_type", "none")
        self._auth_token = self._config.get("auth_token")
        self._auth_username = self._config.get("auth_username")
        self._auth_password = self._config.get("auth_password")
        self._reconnect_interval = self._config["reconnect_interval"]
        self._reconnect_max_attempts = self._config["reconnect_max_attempts"]
        self._heartbeat_interval = self._config["heartbeat_interval"]
        
        logger.info("WebSocket客户端", f"配置已重新加载，目标服务器: {self.uri}")


# 单例模式
_websocket_client: Optional[WebSocketClient] = None


def init_websocket_client() -> Optional[WebSocketClient]:
    """初始化WebSocket客户端"""
    global _websocket_client
    
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("WebSocket客户端", "websockets模块未安装，跳过初始化")
        return None
    
    config = _get_websocket_client_config()
    if not config.get("enabled", False):
        logger.info("WebSocket客户端", "WebSocket客户端已禁用")
        return None
    
    try:
        _websocket_client = WebSocketClient()
        return _websocket_client
    except Exception as e:
        logger.exception_occurred("WebSocket客户端", "初始化失败", e)
        return None


def get_websocket_client() -> Optional[WebSocketClient]:
    """获取WebSocket客户端实例"""
    return _websocket_client


def start_websocket_client():
    """启动WebSocket客户端"""
    if _websocket_client:
        _websocket_client.start()


def stop_websocket_client():
    """停止WebSocket客户端"""
    if _websocket_client:
        _websocket_client.stop()
