"""
AJL 安捷伦色谱仪放样项目 — 专属常量

所有 AJL 任务使用的导航点、步骤名、服务名、API 配置均定义在此文件。
修改此文件不影响其他项目；其他项目的变更也不影响 AJL 任务。

导航点位加载优先级（高→低）：
    1. /config/robot_config.json                 — Docker 外部挂载配置
    2. programs/AJL_anjielun/robot_config.json    — 项目内置配置（本文件所在目录）
    3. infrastructure/robot_config.json          — 基础兜底配置

色谱仪服务器地址加载优先级（高→低）：
    1. /config/robot_config.json                 — Docker 外部挂载配置（http_client 字段）
    2. programs/AJL_anjielun/robot_config.json    — 项目内置配置（http_client 字段）
    3. infrastructure/robot_config.json          — 基础兜底配置（http_client 字段）
    4. AJLApiConfig 类中的硬编码默认值
"""

import json
import os
from enum import Enum
from infrastructure.pose_loader import load_nav_poses, get_active_project


# ──────────────────────────────────────────────────────────────────────────────
# 导航点位坐标（x, y, z, qx, qy, qz, qw）
# ──────────────────────────────────────────────────────────────────────────────

# 硬编码默认值（作为 schema 参考 + config 缺失时的兜底）
_AJL_POSE_DEFAULTS = {
    "GO_HOME_0": [(5.41, 0.06, 0.0, 0.0, 0.0, 0.71, 0.70)],
    "GO_HOME_1": [(4.69, 0.06, 0.0, 0.0, 0.0, 0.71, 0.70), (3.71, 0.06, 0.0, 0.0, 0.0, 0.71, 0.70), (1.05, 0.22, 0.0, 0.0, 0.0, 0.71, 0.70), (0.60, 0.12, 0.0, 0.0, 0.0, 0.71, 0.70)],
    "GO_HOME_2": [(0.60, 0.12, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "HOME": [(-0.24, 0.22, 0.0, 0.0, 0.0, 0.0, 1.0)],   # 色谱盘抓取点
    "DISC_PICKUP_PRE_1": [(0.60, 0.12, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "DISC_PICKUP_PRE_2": [(0.60, 0.12, 0.0, 0.0, 0.0, 0.71, 0.70)],
    "DISC_PICKUP": [(1.05, 0.22, 0.0, 0.0, 0.0, 0.71, 0.70), (3.71, 0.06, 0.0, 0.0, 0.0, 0.71, 0.70), (4.69, 0.06, 0.0, 0.0, 0.0, 0.71, 0.70), (5.41, 0.25, 0.0, 0.0, 0.0, 0.71, 0.70)],   # 色谱盘抓取点
    "INSTRUMENT":  [(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],   # 色谱仪放样点
}


class AJLNavigationPose:
    """
    AJL 任务导航点位坐标。
    各属性在模块加载时从 robot_config.json 动态覆盖；若 config 缺失则使用硬编码默认值。
    访问方式与原来完全一致：AJLNavigationPose.DISC_PICKUP、AJLNavigationPose.INSTRUMENT 等。
    """
    pass


# 运行时从 config 加载，覆盖 AJLNavigationPose 的类属性
# ALL 模式下使用 infrastructure 配置，单项目模式使用本目录配置
_ajl_poses = load_nav_poses(
    project="AJL",
    defaults=_AJL_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "AJL" else None,
)
for _pose_key, _pose_val in _ajl_poses.items():
    setattr(AJLNavigationPose, _pose_key, _pose_val)


# ──────────────────────────────────────────────────────────────────────────────
# 导航精度参数
# ──────────────────────────────────────────────────────────────────────────────

class AJLNavTolerance:
    DISTANCE            = 0.12   # 到点距离容差（米）
    HEADING             = 0.16   # 朝向容差（弧度）
    TRANSLATION_HEADING = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# ROS Service / Task 名
# ──────────────────────────────────────────────────────────────────────────────

class AJLService:
    """AJL 使用的 ROS Service 名"""
    ROBOT_TASK = "/robot_task"


class AJLTask:
    """AJL 各步骤对应的 task 字段值"""
    PICK_DISC  = "pick_up_tray"   # 抓起色谱盘
    PLACE_DISC = "put_tray_to_chromatograph"  # 放下色谱盘（放入仪器）


class AJLArea:
    """AJL 各步骤对应的 area 字段值"""
    DISC_PICKUP = "disc_pickup"   # 色谱盘抓取区
    INSTRUMENT  = "instrument"    # 色谱仪进样区


# ──────────────────────────────────────────────────────────────────────────────
# 任务步骤枚举（用于 TaskStateMachine.update_step）
# ──────────────────────────────────────────────────────────────────────────────

class AJLStep(Enum):
    """AJL 任务流程步骤（对应新版 9 步协议）"""
    IDLE                    = "空闲"

    # ── 放样主流程 ────────────────────────────────────────────────────────────
    NAVIGATING_TO_HOME      = "导航回 home 点位"
    CHECKING_POSITION       = "检查机器人是否在抓取点位"
    NAVIGATING_TO_PICKUP    = "导航到色谱盘抓取点位"
    PICKING_DISC            = "抓起色谱盘"
    NAVIGATING_TO_INST      = "导航到色谱仪点位"
    # API 步骤（按协议顺序）
    API_LOGIN               = "登录AAC系统"
    API_SYNC_SAMPLES        = "同步样品数据到AAC"
    API_QUERY_INSTRUMENT    = "查询仪器CDS ID"
    API_READY_TO_PLACE      = "确认仪器可放样"
    API_APPLY_POSITION      = "申请进样器位置"
    PLACING_DISC            = "放下色谱盘到仪器"
    API_PLACE_COMPLETE      = "通知AAC放样完成"
    API_START_ANALYSIS      = "启动仪器分析"
    API_POLL_RUN_ID         = "轮询AnalysisRunId"
    API_WAIT_ANALYSIS       = "等待分析完成"
    NAVIGATING_BACK         = "导航回色谱盘抓取点位"

    # ── 通用 ─────────────────────────────────────────────────────────────────
    COMPLETED               = "任务完成"
    ERROR                   = "任务异常"


# ──────────────────────────────────────────────────────────────────────────────
# 超时配置（秒）
# ──────────────────────────────────────────────────────────────────────────────

class AJLTimeout:
    ROBOT_ACTION        = 120   # 单次机械臂动作最大等待时间
    NAVIGATION          = 180   # 单次导航最大等待时间
    API_REQUEST         = 50    # 色谱仪 API 单次请求超时
    AFTER_APPLY         = 10    # 申请进样器位置后等待时间（等机械动作完成）
    AFTER_PLACE         = 10    # 放样完成通知后等待时间（等机械归位）
    POLL_RUN_ID         = 60    # 轮询 AnalysisRunId 最大等待时间
    POLL_RUN_ID_INTERVAL = 5    # 轮询 AnalysisRunId 间隔
    ANALYSIS_MAX_WAIT   = 1800  # 等待分析完成最大时间（30 分钟）
    ANALYSIS_POLL_INTERVAL = 30 # 查询分析状态间隔


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 (ReadyToPlace) statusCode 处理策略
# ──────────────────────────────────────────────────────────────────────────────

class AJLStep4Policy:
    """
    step4_ready_to_place 返回的 API statusCode 决策策略。

    协议定义的非零码含义：
        -23  仪器暂时不可用（Instrument temporarily unavailable）
        -24  有暂停任务（Paused task exists）
        -25  已有任务运行中（Task already running）
        -26  错误锁定（Error locked）
        -27  被禁用（Disabled）
        -28  模块错误（Module error）

    策略分类：
        WAIT_AND_RETRY        — 等待短时间后重发（仪器忙碌，有望自动恢复）
        WAIT_LONGER_AND_RETRY — 等待较长时间后重发（运行中，需等当前任务结束）
        TERMINAL_ERROR        — 立即终止并上报错误（需要人工干预）
    """

    # 短等待后重试：仪器暂时不可用 / 有暂停任务
    WAIT_AND_RETRY: frozenset = frozenset({-23, -24})

    # 长等待后重试：已有任务运行中
    WAIT_LONGER_AND_RETRY: frozenset = frozenset({-25})

    # 立即终止：错误锁定 / 被禁用 / 模块错误（需人工干预，重试无意义）
    TERMINAL_ERROR: frozenset = frozenset({-26, -27, -28})

    # 等待时间（秒）
    RETRY_WAIT_SECONDS: int = 30      # WAIT_AND_RETRY 等待间隔
    WAIT_LONGER_SECONDS: int = 60     # WAIT_LONGER_AND_RETRY 等待间隔

    # 最大重试次数
    MAX_RETRIES: int = 10             # WAIT_AND_RETRY 最大次数
    MAX_RETRIES_LONGER: int = 20      # WAIT_LONGER_AND_RETRY 最大次数


# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# 色谱仪服务器地址动态加载
# ──────────────────────────────────────────────────────────────────────────────

def _load_chromatograph_base_url(default: str) -> str:
    """
    从 config 文件的 http_client 字段读取 host / port，拼接为 base_url。
    加载优先级：Docker 外部配置 → 项目内置配置 → infrastructure 配置 → 硬编码默认值。
    host 为空字符串时视为禁用，继续向下搜索。
    """
    _search_paths = [
        "/config/robot_config.json",
        os.path.join(os.path.dirname(__file__), "robot_config.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "infrastructure", "robot_config.json"),
    ]
    for path in _search_paths:
        try:
            with open(os.path.normpath(path), "r", encoding="utf-8") as f:
                data = json.load(f)
            srv = data.get("http_client", {})
            host = srv.get("host", "").strip()
            port = srv.get("port")
            if host and port:
                return f"http://{host}:{port}"
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return default


# ──────────────────────────────────────────────────────────────────────────────
# 色谱仪 AAC HTTP API 配置
# ──────────────────────────────────────────────────────────────────────────────

class AJLApiConfig:
    """AAC 色谱仪服务器连接配置"""
    # 硬编码兜底地址；优先由 robot_config.json 的 http_client 字段覆盖
    _BASE_URL_FALLBACK = "http://192.168.1.100"
    BASE_URL = _load_chromatograph_base_url(_BASE_URL_FALLBACK)

    # 登录凭据（步骤 1）
    USERNAME    = "admin"
    PASSWORD    = "agilent"

    # 第三方标识（步骤 3 查询仪器用，双方约定）
    THIRD_PARTY_IDENTITY = "72"

    # cdsId / injectorId：优先由步骤 3 动态获取，此处为降级默认值
    CDS_ID      = ""    # 步骤 3 查询后自动填入，也可预先填写固定值
    INJECTOR_ID = ""    # GCTray 的 id，步骤 3 查询后自动填入

    # 放样位置（5 个小样品对应 5 个塔孔位）
    VIAL_POSITIONS = ("51", "61", "71", "81", "91")
    VIAL_POSITION = VIAL_POSITIONS[0]  # step5 申请孔位等单点场景默认用第一个

    # 样品数据
    LIMS_SAMPLE_NAME      = "石脑油 1"
    LIMS_SAMPLE_TYPE      = "Sample"
    LIMS_SAMPLE_SITE      = ""          # 采样地点（可选）
    LIMS_EQUIPMENT        = ""          # 采样设备（可选）
    VIAL_TYPE             = "Vial"
    VIAL_LAB_NAME         = "LAB1"
    VIAL_ANALYSIS_PROJECT = "TRAY_DEMO"
    VIAL_ANALYSIS_METHOD  = ""          # 分析方法（可选）
    VIAL_TEST_CODE        = ""          # 检测项目代码（可选）
    VIAL_PRIORITY         = 0           # 优先级（0=正常）

    # 每个大样品（limsSample）对应的小样品（vial）数量
    VIAL_COUNT            = 5

    # 分析完成状态值
    ANALYSIS_STATUS_FINISHED = "Finished"
