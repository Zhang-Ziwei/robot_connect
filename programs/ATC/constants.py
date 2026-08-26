"""
ATC（零件转移）任务专属常量

所有 ATC 任务使用的导航点、步骤名、服务名、任务名均定义在此文件。
修改此文件不会影响旧项目；旧项目的同名常量变更也不会影响 ATC 任务。

导航点位加载优先级（高→低）：
    1. /config/robot_config.json            — Docker 外部挂载配置
    2. programs/ATC/robot_config.json       — 项目内置配置（本文件所在目录）
    3. infrastructure/robot_config.json    — 基础兜底配置
"""

import os
from enum import Enum
from infrastructure.pose_loader import load_nav_poses, get_active_project


# ──────────────────────────────────────────────────────────────────────────────
# 槽位状态枚举
# ──────────────────────────────────────────────────────────────────────────────

class BoxSlotState(str, Enum):
    """
    箱子槽位的物理状态。

    状态流转示例（P3 放零件B）：
        NO_BOX → (人工放入空箱子) → EMPTY → (机器人放了一部分) → HALF
                                                               → (机器人放满) → FULL
                                                                             → (人工取走) → NO_BOX
    """
    EMPTY  = "空箱子"    # 槽位有空箱，可以接收零件
    HALF   = "放了一半"  # 箱子里已有部分零件，仍可继续放
    FULL   = "放满了"    # 箱子已满，等待人工取走
    NO_BOX = "没箱子"    # 槽位无箱子（空槽）


# ──────────────────────────────────────────────────────────────────────────────
# 导航目标点位（对应机器人地图中的实际坐标名）
# ──────────────────────────────────────────────────────────────────────────────

class ATCPose:
    """ATC 任务各导航点位名称"""
    P0   = "point_0"    # 双手抓取点
    P1   = "point_1"    # 零件A取件点
    P2   = "point_2"    # 零件B取件点，初始位置
    P3_1 = "point_3_1"  # P3 槽位 1（放零件B用的空箱子区域）
    P3_2 = "point_3_2"  # P3 槽位 2
    P4   = "point_4"    # 最终放置点

# ── 导航点位硬编码默认值（作为 schema 参考 + config 缺失时的兜底）────────────────
_ATC_POSE_DEFAULTS = {
    "home":     [(0.95, -0.25, 0.0, 0.0, 0.0, 0.05, 1.00)],
    "P1":       [(-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],          # 箱子A取件点
    "P1_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (-0.96, -0.92, 0.0, 0.0, 0.0, 1.00, -0.05)],           # 箱子A取件点（含中间点）
    "P2":       [(-1.02, -0.18, 0.0, 0.0, 0.0, 1.00, -0.05)],          # 箱子B取件点
    "P2_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (-1.02, -0.18, 0.0, 0.0, 0.0, 1.00, -0.05)],           # 箱子B取件点（含中间点）
    "P3_1":     [(0.88, 0.30, 0.0, 0.0, 0.0, 0.05, 1.00)],             # 箱子C中转点1
    "P3_1_mid": [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (0.88, 0.30, 0.0, 0.0, 0.0, 0.05, 1.00)],              # 箱子C中转点1（含中间点）
    "P3_2":     [(0.97, -0.64, 0.0, 0.0, 0.0, 0.05, 1.00)],            # 箱子C中转点2
    "P3_2_mid": [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73),
                 (0.97, -0.64, 0.0, 0.0, 0.0, 0.05, 1.00)],             # 箱子C中转点2（含中间点）
    "P4":       [(0.59, 2.01, 0.0, 0.0, 0.0, 0.73, 0.68)],             # 箱子C放置点
    "P4_mid":   [(0.07, 0.89, 0.0, 0.0, 0.0, -0.68, 0.73)],            # P4 中间点
}


class NavigationPose:
    """
    ATC 导航点位。
    各属性在模块加载时从 robot_config.json 动态覆盖；若 config 缺失则使用上方硬编码默认值。
    访问方式与原来完全一致：NavigationPose.P1、NavigationPose.P1_mid 等。
    """
    pass


# 运行时从 config 加载，覆盖 NavigationPose 的类属性
# ALL 模式下跳过项目内置 config，统一使用 infrastructure 配置；单项目模式使用本目录配置
_atc_poses = load_nav_poses(
    project="ATC",
    defaults=_ATC_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "ATC" else None,
)
for _pose_key, _pose_val in _atc_poses.items():
    setattr(NavigationPose, _pose_key, _pose_val)

# ──────────────────────────────────────────────────────────────────────────────
# 导航精度参数（弧度 / 米）
# ──────────────────────────────────────────────────────────────────────────────

class ATCNavTolerance:
    DISTANCE = 0.12   # 米
    HEADING  = 0.16   # 弧度
    TRANSLATION_HEADING = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# ROS Service / Task 名
# ──────────────────────────────────────────────────────────────────────────────

class ATCService:
    """ATC 使用的 ROS Service 名"""
    ROBOT_TASK = "/robot_task"      # 通用任务 service
    ROBOT_TASK_GEELY = "/robot_task_geely"      # 吉利任务 service


class ATCTask:
    """ATC 各步骤对应的 task 字段值"""
    # 搬零件操作
    PICk_UP_COMPONENT_A_AND_B = "pick_up_component_A_and_B"    # 箱子A和B双手抓取
    PUT_DOWN_COMPONENT_A_AND_B = "put_down_component_A_and_B"    # 箱子A和B双手放置
    PICK_UP_COMPONENT_A   = "pick_up_component_A"    # 箱子A取件
    PICK_UP_COMPONENT_B   = "pick_up_component_B"    # 箱子B取件
    PICK_UP_SCREW   = "pick_up_screw"    # 箱子C取件
    PUT_DOWN_COMPONENT_A   = "put_down_component_A"    # 箱子A放置
    PUT_DOWN_COMPONENT_B   = "put_down_component_B"    # 箱子B放置
    PUT_DOWN_SCREW   = "put_down_screw"    # 箱子C放置

    # 搬箱子操作
    PICK_BOX_TO_SP    = "pick_box_to_sp"       # 把箱子从一点搬到另一点
    PICK_BOX = "pick_box"       # 搬起箱子
    PUT_BOX = "put_box"       # 放下箱子

    # 组装零件操作
    ASSEMBLY = "assembly"
    CONTINUE_ASSEMBLY = "continue_assembly"

    # 最终完成
    COMPLETE = "complete"

# ──────────────────────────────────────────────────────────────────────────────
# 任务步骤枚举（用于 TaskStateMachine.update_step）
# ──────────────────────────────────────────────────────────────────────────────

class ATCStep(Enum):
    """ATC 任务流程步骤，与旧项目 TaskStep 完全独立"""
    IDLE                           = "空闲"

    # ── robot_a：分拣搬运流程 ────────────────────────────────────────────────
    # P2 取件
    NAVIGATING_TO_P2               = "导航到P2取件点"
    PICK_UP_COMPONENT_B_AT_P2      = "抓取零件B在P2点位"
    PICK_UP_SCREW_AT_P2            = "抓取螺丝在P2点位"
    WAITING_MANUAL_RESET           = "等待人工复位完成信号"
    NAVIGATING_TO_P1               = "导航到P1取件点"
    PICK_UP_COMPONENT_A_AT_P1      = "抓取零件A在P1点位"
    NAVIGATING_TO_HOME             = "导航到home点位"

    # P3 放件（两个可选槽位）
    NAVIGATING_TO_P3               = "导航到P3槽位"
    PUT_DOWN_COMPONENT_B_AT_P3     = "放下零件B到P3槽位"
    PUT_DOWN_SCREW_AT_P3           = "放下螺丝到P3槽位"
    PUT_DOWN_COMPONENT_A_AT_P3     = "放下零件A到P3槽位"
    PUT_DOWN_COMPONENT_A_AND_B_AT_P3 = "放下零件A和B到P3槽位"

    # P4 搬箱子流程
    NAVIGATING_TO_P4               = "导航到P4放置点"
    ACTION_PUT_BOX_AT_P3           = "把空箱子从P4搬到P3空槽"
    ACTION_PUT_BOX_AT_P4           = "把满箱子从P3搬到P4"

    # 等待 & 交接
    WAITING_NEXT_STEP              = "等待NEXT_STEP信号"
    NAVIGATING_TO_T6               = "导航到T6交接点"
    ACTION_AT_T6                   = "T6交接动作"
    NAVIGATING_TO_T7               = "导航到T7完成点"
    ACTION_COMPLETE                = "完成动作"

    # ── robot_b：装配流程 ────────────────────────────────────────────────────
    ASSEMBLY                       = "装配中"

    # ── 通用 ─────────────────────────────────────────────────────────────────
    COMPLETED                      = "任务完成"
    ERROR                          = "任务异常"


# ──────────────────────────────────────────────────────────────────────────────
# 超时配置（秒）
# ──────────────────────────────────────────────────────────────────────────────

class ATCTimeout:
    ROBOT_ACTION  = 1200   # 单次机器人动作最大等待时间
    NAVIGATION    = 180   # 单次导航最大等待时间
    HUMAN_WAIT    = 600   # 等待人工介入（10 分钟）
    NEXT_STEP     = 600   # 等待 NEXT_STEP 命令（10 分钟）
    CONVEYOR      = 60    # 传送带运行超时时间（秒）
    P1_WAIT       = 1800  # 等待 P2 有物料的最大时间（30 分钟）


# ──────────────────────────────────────────────────────────────────────────────
# 传送带 PLC 配置（Modbus TCP 客户端）
# ──────────────────────────────────────────────────────────────────────────────

class ATCConveyorConfig:
    """传送带 PLC 连接配置"""
    HOST = "192.168.0.1"   # PLC IP 地址
    PORT = 2000               # Modbus TCP 默认端口


class ATCConveyorRegisters:
    """
    ATC 传送带 PLC 保持寄存器地址与值（Modbus TCP 客户端视角）

    控制寄存器（地址 0）— 写入：
        CTRL_STOP    = 0  停止（每次运动结束后必须写停止，状态才会归空闲）
        CTRL_FORWARD = 1  正转
        CTRL_REVERSE = 2  反转

    状态寄存器（地址 3）— 只读轮询：
        STATE_IDLE = 0  空闲 / 就绪（初始状态；写停止后恢复）
        STATE_RUN  = 1  运行中
        STATE_DONE = 2  运行完成（需写停止才能归空闲）

    注意：与 plc_controller/plc_modbus.py 的服务器端协议独立，勿混用。
    """
    # 寄存器地址
    CTRL  = 0   # 控制寄存器地址
    STATE = 3   # 状态寄存器地址

    # 控制指令值
    CTRL_STOP    = 0
    CTRL_FORWARD = 1
    CTRL_REVERSE = 2

    # 状态值
    STATE_IDLE = 0
    STATE_RUN  = 1
    STATE_DONE = 2
