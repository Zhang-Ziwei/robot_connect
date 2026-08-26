"""
机器人单独动作HTTP接口模块

该模块将机器人的每个动作开放为单独的HTTP接口，
外部HTTP客户端可以通过 action_type 参数触发特定动作。

复用功能：
- 复用 robot_controller.py 中的 send_service_request 方法（支持自动重连、超时处理、错误日志）
- 复用 error_logger.py 中的日志记录功能

使用方法：
1. 在 constants.py 中设置 ENABLE_ROBOT_B_ACTIONS = True
2. 发送HTTP请求：
   curl -X POST http://localhost:8090 -d @test_commands/ROBOT_ACTION_command.json
"""

from typing import Dict, Any, Optional
from infrastructure.constants import ENABLE_ROBOT_B_ACTIONS, ROSServiceRobotB
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()


# 机器人动作定义
# 每个动作包含: service(ROS服务名), action(动作名), type(类型参数), extra_params(额外参数), description(描述)
ROBOT_ACTIONS = {
    # ===== 机器人B动作 =====
    "B_STEP_1": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "pure_water",
        "type": 1,
        "description": "抓取瓶子后倒液并将试管1放到转盘上"
    },
    "B_STEP_2": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "pure_water",
        "type": 2,
        "description": "抓取瓶子后倒液并将试管2放到转盘上"
    },
    "B_STEP_3": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "place_reagent_bottle",
        "type": -1,
        "description": "把样品瓶放回原位并归位"
    },
    "B_STEP_4": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "pour_out_clean",
        "type": 1,
        "description": "将试管1放到清洗设备上"
    },
    "B_STEP_5": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "take_tube_rack",
        "type": 1,
        "description": "将试管1从清洗设备上拿到试管架上"
    },
    "B_STEP_6": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "pour_out_clean",
        "type": 2,
        "description": "将试管2放到清洗设备上"
    },
    "B_STEP_7": {
        "service": ROSServiceRobotB.HALFBODY_CHEMICAL_SERVICE,
        "action": "take_tube_rack",
        "type": 2,
        "description": "将试管2从清洗设备上拿到试管架上"
    }
}

# 保持向后兼容
ROBOT_B_ACTIONS = ROBOT_ACTIONS


def is_robot_actions_enabled() -> bool:
    """检查机器人动作接口是否启用"""
    return ENABLE_ROBOT_B_ACTIONS

# 保持向后兼容
is_robot_b_actions_enabled = is_robot_actions_enabled


def get_available_actions() -> Dict[str, str]:
    """获取所有可用的动作列表"""
    return {
        action_type: info["description"] 
        for action_type, info in ROBOT_ACTIONS.items()
    }


def execute_robot_action(robot, action_type: str, timeout: int = 600) -> Dict[str, Any]:
    """
    执行机器人指定动作
    
    复用 robot_controller.py 中的 send_service_request 方法，该方法支持：
    - 自动重连：连接断开时自动尝试重连
    - 超时处理：支持设置请求超时时间
    - 错误日志：自动记录请求成功/失败/异常
    
    Args:
        robot: 机器人控制器实例（RobotController）
        action_type: 动作类型，如 "B_STEP_1", "B_STEP_2" 等
        timeout: 请求超时时间（秒），默认600秒
    
    Returns:
        执行结果字典：
        {
            "success": bool,
            "action_type": str,
            "description": str,
            "message": str
        }
    """
    # 检查功能是否启用
    if not ENABLE_ROBOT_B_ACTIONS:
        return {
            "success": False,
            "action_type": action_type,
            "description": "",
            "message": "机器人动作接口未启用，请在constants.py中设置ENABLE_ROBOT_B_ACTIONS=True"
        }
    
    # 检查动作类型是否有效
    if action_type not in ROBOT_ACTIONS:
        available = ", ".join(ROBOT_ACTIONS.keys())
        return {
            "success": False,
            "action_type": action_type,
            "description": "",
            "message": f"无效的动作类型: {action_type}，可用动作: {available}"
        }
    
    # 检查机器人是否可用
    if robot is None:
        return {
            "success": False,
            "action_type": action_type,
            "description": ROBOT_ACTIONS[action_type]["description"],
            "message": "机器人未连接"
        }
    
    # 获取动作定义
    action_info = ROBOT_ACTIONS[action_type]
    service = action_info["service"]
    action = action_info["action"]
    action_type_param = action_info["type"]
    description = action_info["description"]
    
    logger.info("机器人动作", f"执行动作: {action_type} - {description}")
    print(f"\n===== 执行机器人动作: {action_type} =====")
    print(f"描述: {description}")
    print(f"服务: {service}, 动作: {action}, 类型参数: {action_type_param}")
    
    try:
        # 复用 robot_controller.py 的 send_service_request 方法
        # 该方法内置：自动重连、超时处理、错误日志记录
        result = robot.send_service_request(
            service=service,
            action=action,
            type=action_type_param,
            maxtime=timeout,
            extra_params=None,
        )

        if result:
            # send_service_request 内部已记录成功日志
            return {
                "success": True,
                "action_type": action_type,
                "description": description,
                "message": f"动作 {action_type} 执行成功"
            }
        else:
            # send_service_request 内部已记录失败日志
            return {
                "success": False,
                "action_type": action_type,
                "description": description,
                "message": f"动作 {action_type} 执行失败"
            }
            
    except Exception as e:
        # send_service_request 内部已记录异常日志
        logger.exception_occurred("机器人动作", f"动作 {action_type} 执行异常", e)
        return {
            "success": False,
            "action_type": action_type,
            "description": description,
            "message": f"动作 {action_type} 执行异常: {str(e)}"
        }

# 保持向后兼容
execute_robot_b_action = execute_robot_action


def handle_robot_action_command(data: Dict[str, Any], robots: Dict) -> Dict[str, Any]:
    """
    处理HTTP命令：ROBOT_ACTION
    
    Args:
        data: HTTP请求数据，包含：
            - action_type: 动作类型 (必需)
            - robot_id: 机器人ID (可选，默认为 "robot_b")
            - timeout: 超时时间秒 (可选，默认600)
        robots: 机器人字典 {robot_id: robot_controller}
    
    Returns:
        执行结果字典
    """
    action_type = data.get("action_type")
    robot_id = data.get("robot_id", "robot_b")
    timeout = data.get("timeout", 600)
    
    if not action_type:
        return {
            "success": False,
            "message": "缺少必需参数: action_type",
            "available_actions": get_available_actions()
        }
    
    # 获取机器人实例
    robot = robots.get(robot_id)
    
    # 执行动作（复用 execute_robot_action，内部调用 robot_controller.send_service_request）
    result = execute_robot_action(robot, action_type, timeout)
    
    return result

# 保持向后兼容
handle_robot_b_action_command = handle_robot_action_command

