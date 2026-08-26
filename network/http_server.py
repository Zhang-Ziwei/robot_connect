"""
HTTP服务器模块
接收外部发送的JSON命令消息
监听HTTP请求并调用命令处理器
支持任务队列模式
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs
from infrastructure.error_logger import get_error_logger
from infrastructure.constants import ErrorCode, make_error_response, make_success_response

logger = get_error_logger()


class CommandHTTPHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器"""
    
    # 类变量：命令处理回调函数
    command_callback: Callable = None
    # 任务队列（可选）
    task_queue = None
    # 是否使用队列模式
    use_queue: bool = False
    
    def do_POST(self):
        """处理POST请求"""
        try:
            # 读取请求体
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            # 解析JSON
            raw_data = json.loads(post_data.decode('utf-8'))

            # 兼容两种格式：
            #   标准格式: {"cmd_type": ..., "cmd_id": ..., ...}
            #   封装格式: {"params": {...}, "data": {"cmd_type": ..., "cmd_id": ...}}
            if "cmd_type" not in raw_data and "data" in raw_data and isinstance(raw_data["data"], dict):
                cmd_data = {**raw_data.get("params", {}), **raw_data["data"]}
            else:
                cmd_data = raw_data

            # 打印接收消息（添加醒目输出）
            if cmd_data.get('cmd_type') != "GET_TASK_STATE":
                print("\n" + "="*70)
                print(f">>> 收到HTTP POST请求")
                print("="*70)
                print(f"来源: {self.client_address[0]}:{self.client_address[1]}")
                print(f"命令类型: {cmd_data.get('cmd_type')}")
                print(f"命令ID: {cmd_data.get('cmd_id')}")
                print(f"完整数据: {json.dumps(cmd_data, ensure_ascii=False, indent=2)}")
                print("="*70 + "\n")
        
            logger.info("HTTP服务器", f"收到命令: {cmd_data.get('cmd_type')} (ID: {cmd_data.get('cmd_id')})")
            
            # 检查是否使用队列模式
            if CommandHTTPHandler.use_queue and CommandHTTPHandler.task_queue:
                # 提交到任务队列
                task_id = CommandHTTPHandler.task_queue.submit_task(
                    cmd_data, 
                    CommandHTTPHandler.command_callback
                )
                
                result = {
                    "success": True,
                    "message": "任务已加入队列",
                    "task_id": task_id,
                    "queue_size": CommandHTTPHandler.task_queue.task_queue.qsize(),
                    "note": "使用 GET /task/<task_id> 查询任务状态"
                }
                
                logger.info("HTTP服务器", f"任务已加入队列: {task_id}")
            else:
                # 直接执行（同步模式）
                if cmd_data.get('cmd_type') != "GET_TASK_STATE":
                    print(">>> 开始执行命令（同步模式）...\n")
                if CommandHTTPHandler.command_callback:
                    try:
                        result = CommandHTTPHandler.command_callback(cmd_data)
                        if cmd_data.get('cmd_type') != "GET_TASK_STATE":
                            print(f"\n>>> 命令执行完成，结果: {result.get('success')}")
                            print(f"    消息: {result.get('message')}\n")
                    except Exception as e:
                        print(f"\n>>> ✗ 命令执行出错: {e}\n")
                        result = make_error_response(
                            ErrorCode.CMD_EXECUTION_ERROR,
                            f"执行出错: {str(e)}"
                        )
                        logger.exception_occurred("HTTP服务器", "同步执行命令", e)
                else:
                    result = make_error_response(
                        ErrorCode.HANDLER_NOT_INIT,
                        "命令处理器未初始化"
                    )
                    print(">>> ✗ 命令处理器未初始化\n")
                if cmd_data.get('cmd_type') != "GET_TASK_STATE":
                    logger.info("HTTP服务器", f"命令执行完成: {result.get('success')}")
            
            # 获取HTTP状态码（从响应中提取，默认200）
            http_status = result.pop("_http_status", 200)
            
            # 返回响应
            self.send_response(http_status)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            response_data = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.wfile.write(response_data)
            
        except json.JSONDecodeError as e:
            logger.error("HTTP服务器", f"JSON解析错误: {e}")
            self.send_error(400, f"Invalid JSON: {str(e)}")
        
        except Exception as e:
            logger.exception_occurred("HTTP服务器", "处理POST请求", e)
            self.send_error(500, f"Internal Server Error: {str(e)}")
    
    def do_GET(self):
        """处理GET请求（健康检查、任务状态查询）"""
        try:
            # 解析URL路径
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            
            # 路由分发
            if path == '/':
                # 健康检查
                response = {
                    "status": "running",
                    "message": "Robot Control HTTP Server is running",
                    "queue_mode": CommandHTTPHandler.use_queue
                }
            
            elif path == '/queue/status':
                # 队列状态
                if CommandHTTPHandler.task_queue:
                    response = CommandHTTPHandler.task_queue.get_queue_status()
                    response["success"] = True
                    response["code"] = ErrorCode.SUCCESS
                else:
                    response = make_error_response(
                        ErrorCode.TASK_QUEUE_DISABLED,
                        "任务队列未启用"
                    )
            
            elif path.startswith('/task/'):
                # 查询任务状态
                task_id = path.split('/')[-1]
                
                if CommandHTTPHandler.task_queue:
                    task_status = CommandHTTPHandler.task_queue.get_task_status(task_id)
                    if task_status:
                        response = make_success_response(
                            "查询成功",
                            task=task_status
                        )
                    else:
                        response = make_error_response(
                            ErrorCode.TASK_NOT_FOUND,
                            f"任务不存在: {task_id}"
                        )
                else:
                    response = make_error_response(
                        ErrorCode.TASK_QUEUE_DISABLED,
                        "任务队列未启用"
                    )
            
            else:
                # 未知路径
                self.send_error(404, f"Not Found: {path}")
                return
            
            # 获取HTTP状态码（从响应中提取，默认200）
            http_status = response.pop("_http_status", 200)
            
            # 返回响应
            self.send_response(http_status)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
            
        except Exception as e:
            logger.exception_occurred("HTTP服务器", "处理GET请求", e)
            self.send_error(500, f"Internal Server Error: {str(e)}")
    
    def log_message(self, format, *args):
        """重写日志方法，使用自定义logger"""
        message = format % args
        logger.info("HTTP服务器", f"HTTP请求: {message}")


class HTTPCommandServer:
    """HTTP命令服务器"""
    
    def __init__(self, host: str = '172.16.10.17', port: int = 8081, use_queue: bool = False):
        self.host = host
        self.port = port
        self.server = None
        self.server_thread = None
        self.running = False
        self.use_queue = use_queue
        
        # 设置队列模式
        CommandHTTPHandler.use_queue = use_queue
    
    def set_command_callback(self, callback: Callable):
        """设置命令处理回调函数"""
        CommandHTTPHandler.command_callback = callback
        logger.info("HTTP服务器", "命令回调函数已设置")
    
    def set_task_queue(self, task_queue):
        """设置任务队列"""
        CommandHTTPHandler.task_queue = task_queue
        CommandHTTPHandler.use_queue = True
        self.use_queue = True
        logger.info("HTTP服务器", "任务队列已设置，启用队列模式")
        print("✓ HTTP服务器已启用任务队列模式")
    
    def start(self):
        """启动HTTP服务器"""
        if self.running:
            logger.warning("HTTP服务器", "服务器已在运行中")
            return
        
        try:
            self.server = HTTPServer((self.host, self.port), CommandHTTPHandler)
            self.running = True
            
            # 在单独线程中运行服务器
            self.server_thread = threading.Thread(
                target=self._run_server,
                daemon=True
            )
            self.server_thread.start()
            
            logger.info("HTTP服务器", f"HTTP服务器启动成功: {self.host}:{self.port}")
            print(f"HTTP命令服务器运行在: http://{self.host}:{self.port}")
            
        except Exception as e:
            logger.exception_occurred("HTTP服务器", "启动服务器", e)
            self.running = False
            raise
    
    def _run_server(self):
        """运行服务器（内部方法）"""
        try:
            logger.info("HTTP服务器", "开始监听HTTP请求...")
            self.server.serve_forever()
        except Exception as e:
            logger.exception_occurred("HTTP服务器", "服务器运行", e)
            self.running = False
    
    def stop(self):
        """停止HTTP服务器"""
        if not self.running:
            return
        
        logger.info("HTTP服务器", "正在停止HTTP服务器...")
        
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        
        if self.server_thread:
            self.server_thread.join(timeout=5)
        
        self.running = False
        logger.info("HTTP服务器", "HTTP服务器已停止")
        print("HTTP服务器已停止")
    
    def is_running(self):
        """检查服务器是否运行中"""
        return self.running


# 全局HTTP服务器实例
_http_server = None

def get_http_server(host: str = '0.0.0.0', port: int = 8080):
    """获取HTTP服务器实例（单例）"""
    global _http_server
    if _http_server is None:
        _http_server = HTTPCommandServer(host, port)
    return _http_server

