"""
WAIC 项目专属常量

职责：
    - 定义导航点位（从 robot_config.json 动态加载，硬编码值作为兜底）
    - 定义 ROS 服务名、任务名、超时时长等项目常量

导航点位加载优先级（高→低）：
    1. /config/robot_config.json               — Docker 外部挂载配置
    2. programs/WAIC/robot_config.json         — 项目内置配置
    3. infrastructure/robot_config.json        — 基础兜底配置
"""

import os
from infrastructure.pose_loader import load_nav_poses, get_active_project


# ──────────────────────────────────────────────────────────────────────────────
# ROS 服务名称
# ──────────────────────────────────────────────────────────────────────────────

class WAICService:
    """WAIC 任务使用的 ROS 服务名"""
    ROBOT_TASK = "/robot_task_geely"


# ──────────────────────────────────────────────────────────────────────────────
# 任务名称（task 字段）
# ──────────────────────────────────────────────────────────────────────────────

class WAICTask:
    """WAIC 机器人任务名称（对应 send_service_request_task 的 task 参数）"""
    PICK_UP_BOX  = "pick_box"   # 把箱子搬起来
    PUT_DOWN_BOX = "put_box"  # 把箱子放下


# ──────────────────────────────────────────────────────────────────────────────
# 任务步骤名称（用于状态机 step 标记）
# ──────────────────────────────────────────────────────────────────────────────

class WAICStep:
    IDLE              = "IDLE"
    NAVIGATING        = "NAVIGATING"
    PICKING_UP        = "PICKING_UP"
    NAVIGATING_TARGET = "NAVIGATING_TARGET"
    PUTTING_DOWN      = "PUTTING_DOWN"
    DONE              = "DONE"


# ──────────────────────────────────────────────────────────────────────────────
# 超时配置（秒）
# ──────────────────────────────────────────────────────────────────────────────

class WAICTimeout:
    ROBOT_ACTION = 120   # 机器人单次抓放动作最长等待时间
    NAVIGATION   = 180   # 单次导航最长等待时间


# ──────────────────────────────────────────────────────────────────────────────
# 导航精度容差
# ──────────────────────────────────────────────────────────────────────────────

class WAICNavTolerance:
    DISTANCE            = 0.08
    HEADING             = 0.08
    TRANSLATION_HEADING = 0.08


# ──────────────────────────────────────────────────────────────────────────────
# 导航点位（运行时从 robot_config.json 加载）
# ──────────────────────────────────────────────────────────────────────────────

# 硬编码默认值——作为 config 缺失时的兜底，以及 schema 参考
# 生产环境请在 programs/WAIC/robot_config.json 中配置实际坐标
_WAIC_POSE_DEFAULTS: dict = {
    # 在此处列出所有合法的点位 key（防呆机制：config 中出现 defaults 没有的 key 会警告并忽略）
    # 坐标设为 (0,0,0,0,0,0,1) 作为占位，请在 robot_config.json 的 navigation_poses.WAIC 中配置实际坐标
    "home":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point1": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point2": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point3": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point4": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point5": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
}


class NavigationPose:
    """
    WAIC 导航点位。
    各属性在模块加载时从 robot_config.json 动态覆盖；若 config 缺失则使用硬编码默认值。
    也可通过 get_nav_pose(area_name) 按名称动态获取。
    """
    pass


_project_config_dir = (
    os.path.dirname(__file__)
    if get_active_project() not in ("ALL",)
    else None
)

_poses = load_nav_poses("WAIC", _WAIC_POSE_DEFAULTS, project_config_dir=_project_config_dir)
for _k, _v in _poses.items():
    setattr(NavigationPose, _k, _v)


def get_nav_pose(area_name: str):
    """
    按名称获取导航点位坐标列表。

    优先查找 NavigationPose 类属性（已从 config 加载）；
    未找到则返回 None。

    参数:
        area_name: 点位名称（如 "point1"、"home"）

    返回:
        list[tuple] | None
    """
    return getattr(NavigationPose, area_name, None)
