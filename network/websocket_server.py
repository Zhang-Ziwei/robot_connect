"""
WebSocket服务器模块
作为HTTP命令接收的补充方式，支持双向通信

功能：
- 接收与HTTP相同格式的JSON命令
- 支持安全的 wss 协议（SSL/TLS）
- 基于 Client ID 的定向单发
- 广播消息给所有连接的客户端
- 心跳检测保持连接

命令格式（与HTTP相同）：
{
    "cmd_type": "SCAN_QRCODE",
    "cmd_id": "unique_id",
    "params": {...}
}

响应格式：
{
    "success": true/false,
    "code": 0,
    "message": "...",
    "data": {...}
}
对于服务器（websocket_server）：
host 表示监听地址
通常设为 "0.0.0.0" = 监听所有网络接口（推荐）
设为特定 IP = 只监听那个网卡

"""

import json
import asyncio
import ssl
import uuid
import threading
from typing import Dict, Callable, Optional, Set
from datetime import datetime

try:
    import websockets
    from websockets.server import serve
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("警告: websockets 模块未安装，WebSocket服务器功能不可用")

from infrastructure.constants import (
    WEBSOCKET_SERVER_ENABLED,
    WEBSOCKET_SERVER_PORT,
    WEBSOCKET_SERVER_HOST,
    WEBSOCKET_SSL_ENABLED,
    WEBSOCKET_SSL_CERT_FILE,
    WEBSOCKET_SSL_KEY_FILE,
    WEBSOCKET_HEARTBEAT_INTERVAL,
    WEBSOCKET_MAX_CLIENTS,
    WEBSOCKET_DEFAULT_CLIENT_ID,
    ErrorCode, make_error_response, make_success_response
)
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


def _get_websocket_config():
    """获取WebSocket配置（优先使用外部配置）"""
    try:
        from infrastructure.config_loader import get_websocket_server_config
        external_config = get_websocket_server_config()
        if external_config:
            return {
                "enabled": external_config.get("enabled", WEBSOCKET_SERVER_ENABLED),
                "host": external_config.get("host", WEBSOCKET_SERVER_HOST),
                "port": external_config.get("port", WEBSOCKET_SERVER_PORT),
                "ssl_enabled": external_config.get("ssl_enabled", WEBSOCKET_SSL_ENABLED),
                "ssl_cert_file": external_config.get("ssl_cert_file", WEBSOCKET_SSL_CERT_FILE),
                "ssl_key_file": external_config.get("ssl_key_file", WEBSOCKET_SSL_KEY_FILE),
                "heartbeat_interval": external_config.get("heartbeat_interval", WEBSOCKET_HEARTBEAT_INTERVAL),
                "max_clients": external_config.get("max_clients", WEBSOCKET_MAX_CLIENTS),
                "default_client_id": external_config.get("default_client_id", WEBSOCKET_DEFAULT_CLIENT_ID),
            }
    except ImportError:
        pass
    except Exception:
        pass
    
    # 使用默认常量配置
    return {
        "enabled": WEBSOCKET_SERVER_ENABLED,
        "host": WEBSOCKET_SERVER_HOST,
        "port": WEBSOCKET_SERVER_PORT,
        "ssl_enabled": WEBSOCKET_SSL_ENABLED,
        "ssl_cert_file": WEBSOCKET_SSL_CERT_FILE,
        "ssl_key_file": WEBSOCKET_SSL_KEY_FILE,
        "heartbeat_interval": WEBSOCKET_HEARTBEAT_INTERVAL,
        "max_clients": WEBSOCKET_MAX_CLIENTS,
        "default_client_id": WEBSOCKET_DEFAULT_CLIENT_ID,
    }


class WebSocketClient:
    """WebSocket客户端信息"""
    
    def __init__(self, websocket, client_id: str = None):
        self.websocket = websocket
        self.client_id = client_id or str(uuid.uuid4())[:8]
        self.connected_at = datetime.now()
        self.last_heartbeat = datetime.now()
        self.remote_address = websocket.remote_address if hasattr(websocket, 'remote_address') else None
    
    def __repr__(self):
        return f"WebSocketClient(id={self.client_id}, addr={self.remote_address})"


class WebSocketServer:
    """
    WebSocket服务器
    
    支持：
    - 接收JSON命令（与HTTP格式相同）
    - 安全的wss协议
    - Client ID定向单发
    - 广播消息
    - 心跳检测
    """
    
    def __init__(self, host: str = None, port: int = None):
        if not WEBSOCKETS_AVAILABLE:
            raise RuntimeError("websockets 模块未安装，请运行: pip install websockets")
        
        # 加载配置
        self._config = _get_websocket_config()
        
        self.host = host or self._config["host"]
        self.port = port or self._config["port"]
        self._heartbeat_interval = self._config["heartbeat_interval"]
        self._max_clients = self._config["max_clients"]
        
        self.clients: Dict[str, WebSocketClient] = {}  # client_id -> WebSocketClient
        self.command_callback: Callable = None
        self._running = False
        self._server = None
        self._loop = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # SSL配置
        self.ssl_context = None
        ssl_enabled = self._config["ssl_enabled"]
        ssl_cert = self._config["ssl_cert_file"]
        ssl_key = self._config["ssl_key_file"]
        if ssl_enabled and ssl_cert and ssl_key:
            # 尝试多个路径（支持Docker和本地环境）
            import os
            cert_paths_to_try = [
                ssl_cert,                          # 配置的路径（Docker: /config/ssl/cert.pem）
                ssl_cert.lstrip('/'),              # 去掉开头的/ (config/ssl/cert.pem)
                "config/ssl/cert.pem",             # 本地相对路径
                "./config/ssl/cert.pem",           # 本地相对路径
            ]
            key_paths_to_try = [
                ssl_key,                           # 配置的路径（Docker: /config/ssl/key.pem）
                ssl_key.lstrip('/'),               # 去掉开头的/
                "config/ssl/key.pem",              # 本地相对路径
                "./config/ssl/key.pem",            # 本地相对路径
            ]
            
            actual_cert = None
            actual_key = None
            
            # 查找证书文件
            for path in cert_paths_to_try:
                if os.path.exists(path):
                    actual_cert = path
                    break
            
            # 查找密钥文件
            for path in key_paths_to_try:
                if os.path.exists(path):
                    actual_key = path
                    break
            
            if actual_cert and actual_key:
                try:
                    self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    self.ssl_context.load_cert_chain(actual_cert, actual_key)
                    logger.info("WebSocket服务器", f"已启用SSL/TLS加密 (证书: {actual_cert})")
                    print(f"🔒 WebSocket SSL已启用: {actual_cert}")
                except Exception as e:
                    logger.error("WebSocket服务器", f"SSL证书加载失败: {e}")
                    print(f"⚠️ SSL证书加载失败: {e}，将使用ws://")
                    self.ssl_context = None
            else:
                logger.warning("WebSocket服务器", f"SSL证书文件未找到，将使用ws://")
                print(f"⚠️ SSL证书文件未找到，将使用ws://（非加密）")
                print(f"   尝试的路径: {cert_paths_to_try[0]}")
    
    def set_command_callback(self, callback: Callable):
        """设置命令处理回调函数"""
        self.command_callback = callback
    
    async def _handle_client(self, websocket):
        """处理单个客户端连接"""
        client = None
        
        try:
            # 检查连接数限制
            if len(self.clients) >= self._max_clients:
                await websocket.close(1013, "服务器连接数已满")
                logger.warning("WebSocket服务器", f"拒绝连接：已达最大连接数 {self._max_clients}")
                return
            
            # 使用配置的默认 Client ID（固定ID模式）
            # 如果已有相同ID的连接，生成带序号的ID
            base_client_id = self._config.get("default_client_id", "client")
            client_id = base_client_id
            
            # 检查是否已存在相同ID的连接，如果存在则添加序号
            with self._lock:
                if client_id in self.clients:
                    # 生成唯一ID：base_id-1, base_id-2, ...
                    counter = 1
                    while f"{base_client_id}-{counter}" in self.clients:
                        counter += 1
                    client_id = f"{base_client_id}-{counter}"
            
            client = WebSocketClient(websocket, client_id)
            
            with self._lock:
                self.clients[client_id] = client
            
            logger.info("WebSocket服务器", f"客户端连接: {client}")
            print(f"\n✓ WebSocket客户端连接: {client_id} ({client.remote_address})")
            
            # 发送欢迎消息，包含分配的client_id
            welcome_msg = {
                "type": "connected",
                "client_id": client_id,
                "default_client_id": base_client_id,
                "message": "WebSocket连接成功",
                "server_time": datetime.now().isoformat()
            }
            await websocket.send(json.dumps(welcome_msg, ensure_ascii=False))
            
            # 消息处理循环
            async for message in websocket:
                try:
                    await self._process_message(client, message)
                except Exception as e:
                    logger.exception_occurred("WebSocket服务器", f"处理消息 (client={client_id})", e)
                    error_response = make_error_response(
                        ErrorCode.CMD_EXECUTION_ERROR,
                        f"消息处理错误: {str(e)}"
                    )
                    await websocket.send(json.dumps(error_response, ensure_ascii=False))
        
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("WebSocket服务器", f"客户端断开连接: {client.client_id if client else 'unknown'} (code={e.code})")
        except Exception as e:
            logger.exception_occurred("WebSocket服务器", "处理客户端连接", e)
        finally:
            # 清理客户端
            if client:
                with self._lock:
                    self.clients.pop(client.client_id, None)
                print(f"✗ WebSocket客户端断开: {client.client_id}")
    
    async def _process_message(self, client: WebSocketClient, message: str):
        """处理客户端消息"""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            response = make_error_response(
                ErrorCode.INVALID_JSON,
                f"JSON解析失败: {str(e)}"
            )
            await client.websocket.send(json.dumps(response, ensure_ascii=False))
            return
        
        # 更新心跳时间
        client.last_heartbeat = datetime.now()
        
        # 检查消息类型
        msg_type = data.get("type") or data.get("cmd_type")
        
        # 处理特殊消息类型
        if msg_type == "ping":
            # 心跳响应
            response = {"type": "pong", "timestamp": datetime.now().isoformat()}
            await client.websocket.send(json.dumps(response, ensure_ascii=False))
            return
        
        if msg_type == "register":
            # 客户端注册/更新client_id
            new_id = data.get("client_id")
            if new_id and new_id != client.client_id:
                with self._lock:
                    old_id = client.client_id
                    self.clients.pop(old_id, None)
                    client.client_id = new_id
                    self.clients[new_id] = client
                logger.info("WebSocket服务器", f"客户端重新注册: {old_id} -> {new_id}")
            
            response = {
                "type": "registered",
                "client_id": client.client_id,
                "message": "注册成功"
            }
            await client.websocket.send(json.dumps(response, ensure_ascii=False))
            return
        
        # 处理命令消息（与HTTP格式相同）
        if msg_type and msg_type not in ["ping", "pong", "register", "registered"]:
            # 打印接收消息
            if msg_type != "GET_TASK_STATE":
                print("\n" + "="*70)
                print(f">>> 收到WebSocket命令")
                print("="*70)
                print(f"客户端ID: {client.client_id}")
                print(f"来源: {client.remote_address}")
                print(f"命令类型: {msg_type}")
                print(f"命令ID: {data.get('cmd_id')}")
                print(f"完整数据: {json.dumps(data, ensure_ascii=False, indent=2)}")
                print("="*70 + "\n")
            
            logger.info("WebSocket服务器", f"收到命令: {msg_type} (ID: {data.get('cmd_id')}) from {client.client_id}")
            
            # 调用命令处理器
            # command_callback 内部含 time.sleep / 轮询等阻塞操作（如 _wait_for_topic_message）。
            # 若在 async 协程里直接同步调用，会冻结整个事件循环，导致心跳超时、消息积压等延迟问题。
            # 使用 run_in_executor 将其放到线程池执行，事件循环保持正常调度。
            if self.command_callback:
                try:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, self.command_callback, data)
                    if msg_type != "GET_TASK_STATE":
                        print(f"\n>>> 命令执行完成，结果: {result.get('success')}")
                        print(f"    消息: {result.get('message')}\n")
                except Exception as e:
                    print(f"\n>>> ✗ 命令执行出错: {e}\n")
                    result = make_error_response(
                        ErrorCode.CMD_EXECUTION_ERROR,
                        f"执行出错: {str(e)}"
                    )
                    logger.exception_occurred("WebSocket服务器", "执行命令", e)
            else:
                result = make_error_response(
                    ErrorCode.HANDLER_NOT_INIT,
                    "命令处理器未初始化"
                )
            
            # 移除内部HTTP状态码字段
            result.pop("_http_status", None)
            
            # 添加响应元数据
            result["_request_cmd_id"] = data.get("cmd_id")
            result["_client_id"] = client.client_id
            
            await client.websocket.send(json.dumps(result, ensure_ascii=False))
    
    async def send_to_client(self, client_id: str, message: dict) -> bool:
        """
        向指定客户端发送消息
        
        Args:
            client_id: 目标客户端ID
            message: 要发送的消息（字典）
        
        Returns:
            bool: 发送是否成功
        """
        with self._lock:
            client = self.clients.get(client_id)
        
        if not client:
            logger.warning("WebSocket服务器", f"客户端不存在: {client_id}")
            return False
        
        try:
            await client.websocket.send(json.dumps(message, ensure_ascii=False))
            logger.info("WebSocket服务器", f"消息已发送到 {client_id}")
            return True
        except Exception as e:
            logger.exception_occurred("WebSocket服务器", f"发送消息到 {client_id}", e)
            return False
    
    def send_to_client_sync(self, client_id: str, message: dict) -> bool:
        """
        同步方式向指定客户端发送消息（线程安全）
        
        Args:
            client_id: 目标客户端ID
            message: 要发送的消息（字典）
        
        Returns:
            bool: 发送是否成功
        """
        if not self._loop or not self._running:
            return False
        
        future = asyncio.run_coroutine_threadsafe(
            self.send_to_client(client_id, message),
            self._loop
        )
        try:
            return future.result(timeout=5)
        except Exception as e:
            logger.exception_occurred("WebSocket服务器", f"同步发送消息到 {client_id}", e)
            return False
    
    async def broadcast(self, message: dict, exclude_clients: Set[str] = None):
        """
        广播消息给所有连接的客户端
        
        Args:
            message: 要广播的消息（字典）
            exclude_clients: 要排除的客户端ID集合
        """
        exclude_clients = exclude_clients or set()
        message_str = json.dumps(message, ensure_ascii=False)
        
        with self._lock:
            clients = list(self.clients.values())
        
        for client in clients:
            if client.client_id not in exclude_clients:
                try:
                    await client.websocket.send(message_str)
                except Exception as e:
                    logger.warning("WebSocket服务器", f"广播到 {client.client_id} 失败: {e}")
    
    def broadcast_sync(self, message: dict, exclude_clients: Set[str] = None):
        """
        同步方式广播消息（线程安全）
        
        Args:
            message: 要广播的消息（字典）
            exclude_clients: 要排除的客户端ID集合
        """
        if not self._loop or not self._running:
            return
        
        asyncio.run_coroutine_threadsafe(
            self.broadcast(message, exclude_clients),
            self._loop
        )
    
    def get_connected_clients(self) -> list:
        """获取所有已连接客户端信息"""
        with self._lock:
            return [
                {
                    "client_id": c.client_id,
                    "remote_address": str(c.remote_address),
                    "connected_at": c.connected_at.isoformat(),
                    "last_heartbeat": c.last_heartbeat.isoformat()
                }
                for c in self.clients.values()
            ]
    
    def get_client_count(self) -> int:
        """获取当前连接客户端数量"""
        with self._lock:
            return len(self.clients)
    
    async def _run_server(self):
        """运行服务器（异步）"""
        protocol = "wss" if self.ssl_context else "ws"
        
        async with serve(
            self._handle_client,
            self.host,
            self.port,
            ssl=self.ssl_context,
            ping_interval=self._heartbeat_interval,
            # ping_timeout 设为 4 倍心跳间隔，给 ROS topic 查询等阻塞操作留足时间
            # 默认 heartbeat_interval=30，即 120 秒无响应才断连，避免执行命令期间被误踢
            ping_timeout=self._heartbeat_interval * 4
        ) as server:
            self._server = server
            logger.info("WebSocket服务器", f"服务器已启动: {protocol}://{self.host}:{self.port}")
            print(f"🌐 WebSocket服务器已启动: {protocol}://{self.host}:{self.port}")
            
            # 等待停止信号
            while self._running:
                await asyncio.sleep(1)
    
    def start(self):
        """启动WebSocket服务器（在新线程中运行）"""
        if not self._config["enabled"]:
            logger.info("WebSocket服务器", "WebSocket服务器已禁用")
            print("🌐 WebSocket服务器已禁用（可在配置中启用）")
            return
        
        if self._running:
            logger.warning("WebSocket服务器", "服务器已在运行")
            return
        
        self._running = True
        
        def run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run_server())
            except Exception as e:
                logger.exception_occurred("WebSocket服务器", "服务器运行", e)
            finally:
                self._loop.close()
        
        self._thread = threading.Thread(target=run_loop, daemon=True, name="WebSocketServer")
        self._thread.start()
        logger.info("WebSocket服务器", "服务器线程已启动")
    
    def stop(self):
        """停止WebSocket服务器"""
        if not self._running:
            return
        
        self._running = False
        
        # 关闭所有客户端连接
        if self._loop and self.clients:
            async def close_all():
                with self._lock:
                    clients = list(self.clients.values())
                for client in clients:
                    try:
                        await client.websocket.close(1001, "服务器关闭")
                    except:
                        pass
            
            asyncio.run_coroutine_threadsafe(close_all(), self._loop)
        
        # 等待线程结束
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        
        logger.info("WebSocket服务器", "服务器已停止")
        print("🌐 WebSocket服务器已停止")


# 全局实例
_websocket_server: Optional[WebSocketServer] = None


def init_websocket_server(host: str = None, port: int = None) -> Optional[WebSocketServer]:
    """初始化WebSocket服务器"""
    global _websocket_server
    
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("WebSocket服务器", "websockets模块未安装，跳过初始化")
        return None
    
    _websocket_server = WebSocketServer(host, port)
    return _websocket_server


def get_websocket_server() -> Optional[WebSocketServer]:
    """获取WebSocket服务器实例"""
    return _websocket_server


def send_to_client(client_id: str, message: dict) -> bool:
    """向指定客户端发送消息（便捷函数）"""
    if _websocket_server:
        return _websocket_server.send_to_client_sync(client_id, message)
    return False


def broadcast_message(message: dict, exclude_clients: Set[str] = None):
    """广播消息给所有客户端（便捷函数）"""
    if _websocket_server:
        _websocket_server.broadcast_sync(message, exclude_clients)
