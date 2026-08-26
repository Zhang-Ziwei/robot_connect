"""
模拟机器人控制器
用于在没有实际机器人连接的情况下测试信息传递
"""

import json
import sys
import os
from typing import Dict, Any

# 兼容直接运行 mock 目录下的脚本时找不到顶层包的情况
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from infrastructure.error_logger import get_error_logger
except ImportError:
    from error_logger import get_error_logger

try:
    from hardware.robot_controller import ServiceTaskResult
except ImportError:
    # 作为备用，在找不到真实模块时本地定义一个等价结构
    import json as _json
    class ServiceTaskResult:  # type: ignore[no-redef]
        def __init__(self, success=False, error_msg="", return_params=""):
            self.success = success
            self.error_msg = error_msg
            self.return_params = return_params
        def __bool__(self):
            return self.success
        def parse_return_params(self):
            if not self.return_params:
                return {}
            try:
                return _json.loads(self.return_params)
            except Exception:
                return {}

logger = get_error_logger()


class MockRobotController:
    """模拟机器人控制器 - 用于测试"""
    
    def __init__(self, host: str, port: str, robot_type: str, 
                 max_retry_attempts=None, retry_interval=5):
        self.host = host
        self.port = port
        self.robot_type = robot_type
        self.robot_name = f"Mock Robot ({host}:{port})"
        self.connected = True  # 模拟模式始终显示已连接
        self.request_log = []  # 记录所有请求
        
        logger.info("模拟机器人", f"初始化模拟机器人: {self.robot_name}")
        print(f"\n{'='*70}")
        print(f"🤖 模拟机器人初始化")
        print(f"{'='*70}")
        print(f"机器人名称: {self.robot_name}")
        print(f"地址: {host}:{port}")
        print(f"类型: {robot_type}")
        print(f"模式: 模拟测试模式（不需要实际连接）")
        print(f"{'='*70}\n")
    
    def connect(self):
        """模拟连接"""
        print(f"\n{'='*70}")
        print(f"🔗 {self.robot_name} - 模拟连接")
        print(f"{'='*70}")
        print(f"✓ 跳过实际连接（模拟模式）")
        print(f"✓ 模拟连接成功")
        print(f"{'='*70}\n")
        
        logger.info("模拟机器人", f"{self.robot_name} 模拟连接成功")
        self.connected = True
        return True
    
    def is_connected(self):
        """检查连接状态"""
        return self.connected
    
    def send_service_request(self, service: str, *args, **kwargs) -> bool:
        """
        模拟发送服务请求。

        刻意不锁定参数名（task / action / extra_params / area / type / maxtime ...），
        让真实 ``RobotController.send_service_request`` 调整命名时本 mock 不需要跟着改。
        所有位置参数与关键字参数会原样保留到 ``request_log``。

        调用示例::

            robot.send_service_request(
                ROSService.CHEM_PROJECT_SERVICE,
                action="WAITING_SPLIT_AREA_TRANSFER",
                area="1",
            )
        """
        # dict / list 类的参数（典型如 extra_params）序列化成字符串，
        # 与真实 controller 下发到 rosbridge 的报文保持一致；其他字段原样保留。
        normalized = {}
        for key, value in kwargs.items():
            if isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value, ensure_ascii=False)
            else:
                normalized[key] = value

        request = {
            "op": "call_service",
            "service": service,
            "args": dict(normalized),
        }
        if args:
            request["args"]["positional"] = list(args)

        self.request_log.append(request)

        print(f"\n{'─'*70}")
        print(f"📤 {self.robot_name} - 发送请求")
        print(f"{'─'*70}")
        print(f"服务: {service}")
        if args:
            print(f"位置参数: {args}")
        if kwargs:
            print(f"关键字参数:")
            for key, value in kwargs.items():
                print(f"  {key}: {value}")

        print(f"\n完整JSON请求:")
        print(json.dumps(request, indent=2, ensure_ascii=False, default=str))
        print(f"{'─'*70}")

        print(f"✓ 模拟执行成功")
        print(f"✓ 返回结果: True")
        print(f"{'─'*70}\n")

        logger.info(
            "模拟机器人",
            f"{self.robot_name} 模拟请求 - service={service}, args={args}, kwargs={kwargs}",
        )

        return True

    def send_service_request_task(
        self,
        service: str,
        task: str = "",
        area: str = "",
        maxtime: int = 10,
        extra_params=None,
    ) -> ServiceTaskResult:
        """
        模拟新契约服务请求（task / area / extra_params）。

        返回 ServiceTaskResult，bool(result) == result.success。
        模拟模式下始终返回 success=True，error_msg="" ，return_params=""。

        调用示例::

            result = robot.send_service_request_task(
                ROSService.CHEM_PROJECT_SERVICE,
                task="WAITING_SPLIT_AREA_TRANSFER",
                area="scan_area",
            )
            if not result:
                ...
        """
        if extra_params is None:
            extra_params = {}
        extra_params_str = (
            extra_params
            if isinstance(extra_params, str)
            else json.dumps(extra_params, ensure_ascii=False)
        )

        request = {
            "op": "call_service",
            "service": service,
            "args": {
                "task": task,
                "area": area,
                "extra_params": extra_params_str,
            },
        }
        self.request_log.append(request)

        print(f"\n{'─'*70}")
        print(f"📤 {self.robot_name} - 发送任务请求（新契约）")
        print(f"{'─'*70}")
        print(f"服务:          {service}")
        print(f"task:          {task}")
        print(f"area:          {area}")
        print(f"extra_params:  {extra_params_str}")
        print(f"\n完整JSON请求:")
        print(json.dumps(request, indent=2, ensure_ascii=False, default=str))
        print(f"{'─'*70}")
        print(f"✓ 模拟执行成功")
        print(f"✓ 返回: ServiceTaskResult(success=True)")
        print(f"{'─'*70}\n")

        logger.info(
            "模拟机器人",
            f"{self.robot_name} 模拟任务请求 - service={service}, task={task}, area={area}",
        )

        return ServiceTaskResult(success=True, error_msg="", return_params="")

    def close(self):
        """关闭连接"""
        print(f"\n{'='*70}")
        print(f"🔌 {self.robot_name} - 断开连接")
        print(f"{'='*70}")
        print(f"✓ 模拟断开连接")
        print(f"总共发送了 {len(self.request_log)} 个请求")
        print(f"{'='*70}\n")
        
        logger.info("模拟机器人", f"{self.robot_name} 模拟断开连接")
        self.connected = False
    
    def get_request_log(self):
        """获取所有请求日志"""
        return self.request_log
    
    def print_request_summary(self):
        """打印请求摘要"""
        print(f"\n{'='*70}")
        print(f"📊 {self.robot_name} - 请求统计")
        print(f"{'='*70}")
        print(f"总请求数: {len(self.request_log)}")
        
        # 按服务统计
        service_count = {}
        action_count = {}
        
        for req in self.request_log:
            service = req.get("service", "unknown")
            action = req["args"].get("action", "unknown")
            
            service_count[service] = service_count.get(service, 0) + 1
            action_count[action] = action_count.get(action, 0) + 1
        
        print(f"\n按服务统计:")
        for service, count in service_count.items():
            print(f"  - {service}: {count}次")
        
        print(f"\n按动作统计:")
        for action, count in action_count.items():
            print(f"  - {action}: {count}次")
        
        print(f"{'='*70}\n")
    
    def save_requests_to_file(self, filename: str = "mock_requests_log.json"):
        """保存所有请求到文件"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(self.request_log, f, indent=2, ensure_ascii=False)
        
        print(f"✓ 请求日志已保存到: {filename}")
        logger.info("模拟机器人", f"请求日志已保存: {filename}")

