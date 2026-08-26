"""
配置文件加载模块
支持从外部JSON配置文件加载机器人和系统配置

配置文件搜索顺序（按 active_project 动态决定）：
  1. /config/robot_config.json          — Docker挂载目录（始终最高优先级）
  2. programs/{active_project}/robot_config.json — 单项目专属（仅 active_project != ALL）
  3. infrastructure/robot_config.json   — 基础共享配置（ALL模式或兜底）

ALL 模式（或 active_project 未配置）直接跳到第 3 步，
保证不同项目的机器人 IP / 端口等配置不会互相干扰。
"""

import os
import json
from typing import Dict, Any, List, Optional
from infrastructure.error_logger import get_error_logger

logger = get_error_logger()

# 各项目配置目录（相对于本文件所在的 infrastructure/ 目录）
_INFRA_DIR = os.path.dirname(__file__)
_PROJECT_CONFIG_DIRS: Dict[str, str] = {
    "ATC":  os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "ATC")),
    "WRC":  os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "WRC")),
    # WRC_FLOW：图形化流程编排引擎试点，独立配置目录，不影响 WRC
    "WRC_FLOW": os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "WRC_FLOW")),
    "AJL":  os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "AJL_anjielun")),
    "TJSH": os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "TJSH")),
    "WAIC": os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "WAIC")),
    "KAIAO": os.path.normpath(os.path.join(_INFRA_DIR, "..", "programs", "KAIAO")),
}

# 全局配置缓存
_config_cache: Optional[Dict[str, Any]] = None
_config_path: Optional[str] = None


def _get_search_paths() -> List[str]:
    """
    按当前 active_project 构造配置文件搜索路径列表（动态，每次 find_config_file 时调用）。

    返回顺序：
      1. /config/robot_config.json  — Docker外部（始终）
      2. programs/{project}/robot_config.json  — 单项目模式时
      3. infrastructure/robot_config.json  — ALL或兜底
    """
    paths = ["/config/robot_config.json"]

    try:
        from infrastructure.pose_loader import get_active_project
        active_project = get_active_project()
    except Exception:
        active_project = "ALL"

    if active_project != "ALL":
        project_dir = _PROJECT_CONFIG_DIRS.get(active_project)
        if project_dir:
            project_cfg = os.path.join(project_dir, "robot_config.json")
            paths.append(project_cfg)

    paths.append(os.path.join(_INFRA_DIR, "robot_config.json"))
    return paths


def find_config_file() -> Optional[str]:
    """
    查找配置文件（按当前 active_project 的搜索路径优先级）。

    返回:
        找到的配置文件路径，如果没找到返回 None
    """
    for path in _get_search_paths():
        if os.path.exists(path):
            return path
    return None


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    加载配置文件
    
    参数:
        force_reload: 是否强制重新加载
    
    返回:
        配置字典
    """
    global _config_cache, _config_path
    
    if _config_cache is not None and not force_reload:
        return _config_cache
    
    config_path = find_config_file()
    
    if config_path:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                _config_cache = json.load(f)
            _config_path = config_path
            logger.info("配置加载", f"✓ 已加载外部配置文件: {config_path}")
            print(f"✓ 已加载外部配置文件: {config_path}")
            return _config_cache
        except json.JSONDecodeError as e:
            logger.error("配置加载", f"配置文件格式错误: {config_path}, {e}")
            print(f"⚠️  配置文件格式错误: {config_path}")
        except Exception as e:
            logger.error("配置加载", f"读取配置文件失败: {config_path}, {e}")
            print(f"⚠️  读取配置文件失败: {config_path}")
    
    # 没找到或加载失败，返回空配置
    logger.info("配置加载", "未找到外部配置文件，使用默认配置")
    print("ℹ️  未找到外部配置文件，使用constants.py中的默认配置")
    _config_cache = {}
    _config_path = None
    return _config_cache


def get_config_path() -> Optional[str]:
    """获取当前使用的配置文件路径"""
    return _config_path


def reload_config() -> Dict[str, Any]:
    """
    重新加载配置文件
    
    返回:
        重新加载后的配置字典
    """
    global _config_cache, _config_path
    
    old_path = _config_path
    _config_cache = None
    _config_path = None
    
    new_config = load_config(force_reload=True)

    # 配置文件中 callback / 导航点位等由单例或 import 时缓存，须一并刷新
    try:
        from handlers.callback_sender import reset_callback_sender
        reset_callback_sender()
    except Exception as e:
        logger.warning("配置加载", f"重置 CallbackSender 失败: {e}")

    try:
        from infrastructure.pose_loader import reload_active_project_nav_poses
        reload_active_project_nav_poses()
    except Exception as e:
        logger.warning("配置加载", f"重载导航点位失败: {e}")

    if _config_path:
        logger.info("配置加载", f"✓ 配置文件已重新加载: {_config_path}")
        print(f"✓ 配置文件已重新加载: {_config_path}")
    else:
        logger.info("配置加载", "✓ 已重置为默认配置")
        print("✓ 已重置为默认配置")
    
    return new_config


def get_robot_configs() -> Dict[str, Dict[str, Any]]:
    """
    获取机器人配置
    
    返回:
        机器人配置字典，格式:
        {
            "robot_id": {
                "host": "ip_address",
                "port": "port",
                "robot_type": "type_string",
                "enabled": true/false
            }
        }
    """
    config = load_config()
    
    # 检查是否有配置文件（区分"没有配置文件"和"配置文件中没有启用的机器人"）
    if not config:
        # 没有找到配置文件，返回 None 让调用方使用默认配置
        return None
    
    robots_config = config.get("robots", {})
    
    # 如果配置文件中没有 robots 配置项，返回 None 使用默认配置
    if not robots_config:
        return None
    
    # 过滤掉禁用的机器人
    enabled_robots = {}
    for robot_id, robot_config in robots_config.items():
        if robot_config.get("enabled", True):
            enabled_robots[robot_id] = robot_config
    
    # 返回启用的机器人（可能是空字典，表示所有机器人都禁用了）
    return enabled_robots


def get_http_server_port() -> int:
    """获取HTTP服务器端口"""
    config = load_config()
    return config.get("http_server", {}).get("port", 8090)


def get_auto_charging_config() -> Dict[str, Any]:
    """
    获取自动充电配置
    
    返回:
        {
            "enabled": bool,
            "check_interval": int,
            "low_threshold": float,
            "charging_done_threshold": float,
            "charging_accept_task_threshold": float,
            "charging_done_home_poses": dict
        }
    """
    config = load_config()
    return config.get("auto_charging", {})


def get_flow_api_server_config() -> Dict[str, Any]:
    """
    获取图形化流程编辑器 API 服务器配置（`network/flow_api_server.py` 用）。

    这个服务器与业务命令端口（`http_server.port`）完全独立，专门服务
    拖拽式流程编辑器的静态页面 + 流程增删改查/校验/演练接口。

    返回:
        {
            "port": int,   # 监听端口，默认 8099
        }

    robot_config.json 配置示例：
        "flow_api_server": {
            "port": 8099
        }
    """
    config = load_config()
    return config.get("flow_api_server", {})


def get_websocket_server_config() -> Dict[str, Any]:
    """
    获取WebSocket服务器配置
    
    返回:
        {
            "enabled": bool,
            "host": str,
            "port": int,
            "ssl_enabled": bool,
            "ssl_cert_file": str,
            "ssl_key_file": str,
            "heartbeat_interval": int,
            "max_clients": int
        }
    """
    config = load_config()
    return config.get("websocket_server", {})


def get_navigation_map_config() -> Dict[str, Any]:
    """
    获取导航地图自动下发配置
    
    返回:
        {
            "auto_set_on_connect": bool,   # 连接后是否自动下发地图（默认 True）
            "wait_idle_timeout": int,      # 等待导航空闲的最长秒数（默认 60）
            "service_timeout": int,        # set_map 服务调用超时秒数（默认 30）
            "skip_if_same": bool,          # 当前地图相同则跳过（默认 True）
        }
    """
    config = load_config()
    return config.get("navigation_map", {})


def get_navigation_localization_config() -> Dict[str, Any]:
    """
    获取导航定位（reloc）配置

    返回:
        {
            "auto_set_after_map": bool,
            "method":    str,
            "map_path":  str,
            "x_pos": float, "y_pos": float, "z_pos": float,
            "x_ori": float, "y_ori": float, "z_ori": float, "w_ori": float,
            "check_timeout":  int,
            "retry_interval": int,
            "max_retries":    int,
        }
    """
    config = load_config()
    return config.get("navigation_localization", {})


def get_robot_connection_config() -> Dict[str, Any]:
    """
    获取机器人WebSocket连接配置（用于连接ROS Bridge）
    
    返回:
        {
            "reconnect_max_attempts": int or None,  # 最大重连次数，None为无限
            "reconnect_interval": int,              # 重连间隔（秒）
            "ping_interval": int,                   # WebSocket ping间隔（秒）
            "ping_timeout": int                     # WebSocket ping超时（秒）
        }
    """
    config = load_config()
    return config.get("robot_connection", {})


def get_callback_config() -> Dict[str, Any]:
    """
    获取异步任务完成回调配置。

    任务完成或失败后，系统会主动 POST 结果到配置的 URL。
    未配置或 enabled=false 时回调功能关闭。

    返回:
        {
            "enabled":         bool,   # 是否启用回调（默认 False）
            "url":             str,    # 回调目标完整 URL
            "timeout":         float,  # 单次请求等待响应超时（秒，默认 10）
            "retry":           int,    # 失败后重试次数（默认 0）
            "retry_interval":  float,  # 重试间隔（秒，默认 10）
        }

    对方须在 timeout 内返回 HTTP 200 且 JSON：{ "received": true }

    robot_config.json 配置示例：
        "callback": {
            "enabled": true,
            "url": "http://192.168.1.100:8099/callback",
            "timeout": 10,
            "retry": 3,
            "retry_interval": 10
        }
    """
    config = load_config()
    return config.get("callback", {})


def display_config_info():
    """显示当前配置信息"""
    config = load_config()
    config_path = get_config_path()
    
    print("\n" + "="*60)
    print("📋 配置信息")
    print("="*60)
    
    if config_path:
        print(f"配置文件: {config_path}")
    else:
        print("配置文件: 使用默认配置 (constants.py)")
    
    # 显示机器人配置
    robots = config.get("robots", {})
    if robots:
        print(f"\n机器人配置 ({len(robots)} 个):")
        for robot_id, robot_config in robots.items():
            enabled = "✓" if robot_config.get("enabled", True) else "✗"
            host = robot_config.get("host", "未配置")
            port = robot_config.get("port", "未配置")
            print(f"  [{enabled}] {robot_id}: {host}:{port}")
    else:
        print("\n机器人配置: 使用默认配置")
    
    # 显示HTTP服务器配置
    http_config = config.get("http_server", {})
    if http_config:
        print(f"\nHTTP服务器端口: {http_config.get('port', 8090)}")
    
    # 显示自动充电配置
    charging_config = config.get("auto_charging", {})
    if charging_config:
        enabled = "开启" if charging_config.get("enabled", True) else "关闭"
        print(f"\n自动充电功能: {enabled}")
        if charging_config.get("enabled", True):
            require_on_startup = charging_config.get("require_battery_on_startup", True)
            print(f"  启动时等待电量: {'是' if require_on_startup else '否'}")
            if require_on_startup:
                wait_timeout = charging_config.get("battery_wait_timeout", 60)
                print(f"  等待超时: {wait_timeout}秒" if wait_timeout > 0 else "  等待超时: 无限等待")
            print(f"  检测间隔: {charging_config.get('check_interval', 600)}秒")
            print(f"  低电量阈值: {charging_config.get('low_threshold', 0.30)*100:.0f}%")
            print(f"  可接收任务阈值: {charging_config.get('charging_accept_task_threshold', 0.50)*100:.0f}%")
            print(f"  充电完成阈值: {charging_config.get('charging_done_threshold', 0.99)*100:.0f}%")
            home_poses = charging_config.get('charging_done_home_poses', {})
            if home_poses:
                print(f"  充电完成后home点位:")
                for robot_id, pose in home_poses.items():
                    print(f"    {robot_id}: {pose}")
    
    # 显示WebSocket服务器配置
    ws_config = config.get("websocket_server", {})
    ws_enabled = ws_config.get("enabled", True)
    print(f"\nWebSocket服务器: {'开启' if ws_enabled else '关闭'}")
    if ws_enabled:
        ws_port = ws_config.get("port", 8091)
        ws_ssl = ws_config.get("ssl_enabled", False)
        protocol = "wss" if ws_ssl else "ws"
        print(f"  地址: {protocol}://0.0.0.0:{ws_port}")
        if ws_ssl:
            print(f"  SSL证书: {ws_config.get('ssl_cert_file', '未配置')}")
    
    # 显示导航地图自动下发配置
    nav_map_config = config.get("navigation_map", {})
    if nav_map_config:
        auto_on = nav_map_config.get("auto_set_on_connect", True)
        print(f"\n导航地图自动下发: {'开启' if auto_on else '关闭'}")
        if auto_on:
            print(f"  等待导航空闲超时: {nav_map_config.get('wait_idle_timeout', 60)}秒")
            print(f"  服务调用超时: {nav_map_config.get('service_timeout', 30)}秒")
            print(f"  当前地图相同时跳过: {'是' if nav_map_config.get('skip_if_same', True) else '否'}")
            # 逐机器人列出配置的 map_name
            robots = config.get("robots", {})
            for rid, rc in robots.items():
                if rc.get("enabled", True):
                    print(f"  {rid} 目标地图: {rc.get('navigation_map') or '(未配置)'}")
    
    # 显示机器人连接配置
    robot_conn_config = config.get("robot_connection", {})
    if robot_conn_config:
        max_attempts = robot_conn_config.get("reconnect_max_attempts")
        interval = robot_conn_config.get("reconnect_interval", 5)
        ping_interval = robot_conn_config.get("ping_interval", 20)
        ping_timeout = robot_conn_config.get("ping_timeout", 30)
        print(f"\n机器人连接配置:")
        print(f"  重连次数: {'无限' if max_attempts is None else max_attempts}")
        print(f"  重连间隔: {interval}秒")
        print(f"  Ping间隔: {ping_interval}秒")
        print(f"  Ping超时: {ping_timeout}秒")
    
    print("="*60 + "\n")


# 导出便捷函数
__all__ = [
    'load_config',
    'reload_config',
    'get_config_path',
    'get_robot_configs',
    'get_http_server_port',
    'get_auto_charging_config',
    'get_flow_api_server_config',
    'get_websocket_server_config',
    'get_navigation_map_config',
    'get_navigation_localization_config',
    'get_robot_connection_config',
    'get_callback_config',
    'display_config_info',
]

