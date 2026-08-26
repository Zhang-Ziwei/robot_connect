"""
导航点位配置加载器（带防呆机制）

加载优先级（高→低）：
    1. /config/robot_config.json       — Docker 挂载的外部配置（现场调试专用）
    2. programs/<project>/robot_config.json — 项目内置配置（代码仓库随项目发布）
    3. infrastructure/robot_config.json    — 基础配置（兜底）

防呆机制：
    • 若 config 中某个 key 不在 defaults 中（可能是拼写错误）→ WARNING + 忽略
    • 若 defaults 中某个 key 不在 config 中（config 缺失字段）  → WARNING + 使用硬编码默认值
    • 若 config 中某个 pose 格式非法（非 7 元坐标或合法嵌套列表） → WARNING + 使用硬编码默认值
    • 即使所有 config 都缺失，始终返回完整的 defaults，程序不崩溃

用法示例（在 constants.py 中）：
    from infrastructure.pose_loader import load_nav_poses
    import os

    _DEFAULTS = {
        "P1": [(-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],
        "P1_mid": [(0.07, 0.89, 0.0, ...), (-0.96, -0.92, 0.0, ...)],
    }

    class NavigationPose:
        pass

    _poses = load_nav_poses("ATC", _DEFAULTS, project_config_dir=os.path.dirname(__file__))
    for _k, _v in _poses.items():
        setattr(NavigationPose, _k, _v)
"""

import json
import os
from typing import Any, Dict, List, Optional

from infrastructure.error_logger import get_error_logger

logger = get_error_logger()

_LOG = "PoseLoader"

# ── 支持的项目名称 ───────────────────────────────────────────────────────────
# WRC_FLOW：图形化流程编排引擎的 WRC 试点，与 WRC 完全独立，不参与 "ALL" 模式
SUPPORTED_PROJECTS = frozenset({"TJSH", "ATC", "WRC", "WRC_FLOW", "AJL", "WAIC", "KAIAO", "ALL"})

# ── 项目配置优先级路径（共用于 active_project 与 navigation_poses）─────────
_DOCKER_EXTERNAL_CONFIG = "/config/robot_config.json"
_INFRA_CONFIG = os.path.join(os.path.dirname(__file__), "robot_config.json")


def get_active_project() -> str:
    """
    读取当前激活的项目配置。

    加载优先级（高→低）：
        1. /config/robot_config.json             — Docker 外部挂载配置
           （生产部署时将任意项目的 robot_config.json 挂载到此路径，其中的
           active_project 字段即自动选中对应项目）
        2. infrastructure/robot_config.json      — 基础兜底配置
           （active_project 默认为 "ALL"，可手动修改）
        3. infrastructure/constants.py           — 硬编码最终兜底
           （DEFAULT_ACTIVE_PROJECT，本地开发时修改此处切换项目）

    返回值：
        "TJSH" / "ATC" / "WRC" / "WRC_FLOW" / "AJL" / "WAIC" / "KAIAO" / "ALL"
    """
    # 优先级 1：Docker 外部挂载配置
    try:
        with open(_DOCKER_EXTERNAL_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        project = data.get("active_project")
        if project:
            if project not in SUPPORTED_PROJECTS:
                logger.warning(
                    _LOG,
                    f"active_project 值 '{project}' 不在支持列表 {sorted(SUPPORTED_PROJECTS)} 中，"
                    f"继续查找下一优先级",
                )
            else:
                return project
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(_LOG, f"读取 {_DOCKER_EXTERNAL_CONFIG} 中的 active_project 失败: {e}")

    # 优先级 2：infrastructure/robot_config.json
    try:
        with open(_INFRA_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        project = data.get("active_project")
        if project and project in SUPPORTED_PROJECTS:
            return project
    except Exception:
        pass

    # 优先级 3：infrastructure/constants.py 硬编码默认值
    try:
        from infrastructure.constants import DEFAULT_ACTIVE_PROJECT
        return DEFAULT_ACTIVE_PROJECT
    except Exception:
        return "ALL"

# Docker 外部挂载路径（优先级最高）
_DOCKER_CONFIG = "/config/robot_config.json"

# infrastructure 目录内的兜底配置
_INFRA_CONFIG = os.path.join(os.path.dirname(__file__), "robot_config.json")


# ──────────────────────────────────────────────────────────────────────────────
# 格式校验
# ──────────────────────────────────────────────────────────────────────────────

def _is_valid_pose(value: Any) -> bool:
    """
    校验单个点位坐标是否合法。
    合法格式：
        [x, y, z, qx, qy, qz, qw]          — 单段，7 个数字的 list/tuple
        [[x,y,z,qx,qy,qz,qw], ...]         — 多段路径，每段 7 个数字
    """
    if not isinstance(value, (list, tuple)) or len(value) == 0:
        return False

    # 多段路径：第一个元素是 list/tuple
    if isinstance(value[0], (list, tuple)):
        return all(
            isinstance(seg, (list, tuple)) and len(seg) == 7
            and all(isinstance(v, (int, float)) for v in seg)
            for seg in value
        )

    # 单段：7 个数字
    return len(value) == 7 and all(isinstance(v, (int, float)) for v in value)


def _normalize_pose(value: Any) -> Any:
    """将 JSON 加载的 list 转换为统一的 list-of-list 或 list 格式（不改变语义）。"""
    if isinstance(value[0], list):
        return [tuple(seg) for seg in value]
    return [tuple(value)]


# ──────────────────────────────────────────────────────────────────────────────
# 配置文件加载（带缓存）
# ──────────────────────────────────────────────────────────────────────────────

_file_cache: Dict[str, Optional[Dict]] = {}


def _load_json(path: str) -> Optional[Dict]:
    """加载 JSON 文件并缓存，失败返回 None。"""
    if path in _file_cache:
        return _file_cache[path]
    if not os.path.exists(path):
        _file_cache[path] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _file_cache[path] = data
        return data
    except Exception as e:
        logger.warning(_LOG, f"读取配置文件失败 [{path}]: {e}")
        _file_cache[path] = None
        return None


def _get_project_poses(config: Dict, project: str) -> Optional[Dict]:
    """从配置 dict 中提取指定项目的 navigation_poses，不存在返回 None。"""
    nav = config.get("navigation_poses")
    if not isinstance(nav, dict):
        return None
    poses = nav.get(project)
    return poses if isinstance(poses, dict) else None


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────

def load_nav_poses(
    project: str,
    defaults: Dict[str, Any],
    project_config_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    加载指定项目的导航点位，带防呆回退机制。

    参数:
        project:            项目名（对应 robot_config.json 中 navigation_poses 的一级 key）
        defaults:           硬编码默认值 dict，格式 {pose_name: coordinates}
                            同时作为"合法 key 集合"，防止 config 中的拼写错误污染
        project_config_dir: 项目配置文件所在目录（如 os.path.dirname(__file__)），
                            用于搜索项目内置 robot_config.json

    返回:
        合并后的点位 dict，所有 defaults 中的 key 均存在，类型与 defaults 一致。
    """
    expected_keys = set(defaults.keys())

    # 按优先级搜索配置来源
    search_paths: List[str] = [_DOCKER_CONFIG]
    if project_config_dir:
        search_paths.append(os.path.join(project_config_dir, "robot_config.json"))
    search_paths.append(_INFRA_CONFIG)

    raw_poses: Optional[Dict] = None
    used_path: Optional[str] = None

    for path in search_paths:
        cfg = _load_json(path)
        if cfg is None:
            continue
        poses = _get_project_poses(cfg, project)
        if poses is not None:
            raw_poses = poses
            used_path = path
            break

    if raw_poses is None:
        logger.warning(
            _LOG,
            f"[{project}] 所有配置文件均未找到 navigation_poses.{project}，"
            f"使用全部硬编码默认值",
        )
        return dict(defaults)

    logger.info(_LOG, f"[{project}] 从 {used_path} 加载导航点位")

    result: Dict[str, Any] = {}
    config_keys = set(raw_poses.keys())

    # 防呆：config 中有但 defaults 中没有的 key → 警告（可能是拼写错误）
    extra_keys = config_keys - expected_keys
    if extra_keys:
        logger.warning(
            _LOG,
            f"[{project}] config 中存在未知点位 key（已忽略，请检查拼写）: "
            f"{sorted(extra_keys)}",
        )
        print(
            f"⚠️  [PoseLoader/{project}] config 中存在未知点位 key，已忽略: "
            f"{sorted(extra_keys)}"
        )

    # 逐个 key 处理
    for key, default_val in defaults.items():
        if key not in raw_poses:
            # 防呆：config 缺少该 key → 警告 + 使用默认值
            logger.warning(
                _LOG,
                f"[{project}] config 缺少点位 key '{key}'，使用硬编码默认值",
            )
            print(f"⚠️  [PoseLoader/{project}] config 缺少 '{key}'，使用硬编码默认值")
            result[key] = default_val
            continue

        raw_val = raw_poses[key]
        if not _is_valid_pose(raw_val):
            # 防呆：格式非法 → 警告 + 使用默认值
            logger.warning(
                _LOG,
                f"[{project}] 点位 '{key}' 格式非法（期望 7 元坐标或其列表），"
                f"使用硬编码默认值。收到: {raw_val!r}",
            )
            print(f"⚠️  [PoseLoader/{project}] '{key}' 格式非法，使用硬编码默认值")
            result[key] = default_val
            continue

        result[key] = _normalize_pose(raw_val)

    return result


def _apply_poses_to_class(nav_class, poses: Dict[str, Any]) -> None:
    """将点位 dict 写回项目 NavigationPose 类属性。"""
    for key, val in poses.items():
        setattr(nav_class, key, val)


def reload_active_project_nav_poses() -> None:
    """
    从 robot_config 重新加载导航点位（RESET_SYSTEM 后调用）。

    模块 import 时只加载一次；修改 /config/robot_config.json 后须通过本函数刷新。
    """
    project = get_active_project()
    targets = []

    if project in ("ALL", "KAIAO"):
        from programs.KAIAO.constants import NavigationPose as KAIAONav, _KAIAO_POSE_DEFAULTS
        import programs.KAIAO.constants as kaiao_c
        targets.append((
            "KAIAO",
            KAIAONav,
            _KAIAO_POSE_DEFAULTS,
            os.path.dirname(kaiao_c.__file__) if project == "KAIAO" else None,
        ))

    if project in ("ALL", "WAIC"):
        from programs.WAIC.constants import NavigationPose as WAICNav, _WAIC_POSE_DEFAULTS
        import programs.WAIC.constants as waic_c
        targets.append((
            "WAIC",
            WAICNav,
            _WAIC_POSE_DEFAULTS,
            os.path.dirname(waic_c.__file__) if project == "WAIC" else None,
        ))

    if project in ("ALL", "ATC"):
        from programs.ATC.constants import NavigationPose as ATCNav, _ATC_POSE_DEFAULTS
        import programs.ATC.constants as atc_c
        targets.append((
            "ATC",
            ATCNav,
            _ATC_POSE_DEFAULTS,
            os.path.dirname(atc_c.__file__) if project == "ATC" else None,
        ))

    if project in ("ALL", "WRC"):
        from programs.WRC.constants import NavigationPose as WRCNav, _WRC_POSE_DEFAULTS
        import programs.WRC.constants as wrc_c
        targets.append((
            "WRC",
            WRCNav,
            _WRC_POSE_DEFAULTS,
            os.path.dirname(wrc_c.__file__) if project == "WRC" else None,
        ))

    if project in ("ALL", "TJSH"):
        from programs.TJSH.constants import NavigationPose as TJSHNav, _TJSH_POSE_DEFAULTS
        import programs.TJSH.constants as tjsh_c
        targets.append((
            "TJSH",
            TJSHNav,
            _TJSH_POSE_DEFAULTS,
            os.path.dirname(tjsh_c.__file__) if project == "TJSH" else None,
        ))

    if project in ("ALL", "AJL"):
        from programs.AJL_anjielun.constants import (
            AJLNavigationPose,
            _AJL_POSE_DEFAULTS,
        )
        import programs.AJL_anjielun.constants as ajl_c
        targets.append((
            "AJL",
            AJLNavigationPose,
            _AJL_POSE_DEFAULTS,
            os.path.dirname(ajl_c.__file__) if project == "AJL" else None,
        ))

    for proj, nav_class, defaults, cfg_dir in targets:
        poses = load_nav_poses(proj, defaults, project_config_dir=cfg_dir)
        _apply_poses_to_class(nav_class, poses)
        logger.info(_LOG, f"[{proj}] 导航点位已热重载（{len(poses)} 个）")
        print(f"✓ [{proj}] 导航点位已重新加载")
