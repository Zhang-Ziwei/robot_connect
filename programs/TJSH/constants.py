"""
TJSH 项目专属常量

导航点位加载优先级（高→低）：
    1. /config/robot_config.json                  — Docker 外部挂载配置
    2. programs/TJSH/robot_config.json            — 项目内置配置（本文件所在目录）
    3. infrastructure/robot_config.json           — 基础兜底配置
"""

import os
from infrastructure.pose_loader import load_nav_poses, get_active_project


# ──────────────────────────────────────────────────────────────────────────────
# 导航点位硬编码默认值（作为 schema 参考 + config 缺失时的兜底）
# ──────────────────────────────────────────────────────────────────────────────
_TJSH_POSE_DEFAULTS = {
    # 扫描台：先中间点再终点（多段路径）
    "SCAN_TABLE": [
        (2.88,  61.48, 0.0, 0.0, 0.0, 1.00,  0.03),   # 中间点
        (2.88, -61.48, 0.0, 0.0, 0.0, 1.00,  0.03),   # 终点
    ],

    # ── robot_a 转运任务点位 ──────────────────────────────────────────────────
    "HOME_ROBOT_A": [
        (0.0,  0.0,  0.0, 0.0, 0.0, 0.0, 1.0)],       # home_transfer
    "TASK_PREPARE_WAITING_SPLIT_AREA_TRANSFER": [
        (4.35, -3.80, 0.0, 0.0, 0.0, -0.83, -0.55)],  # 分液台待分液区任务准备点
    "WAITING_SPLIT_AREA_TRANSFER": [
        (0.72, -2.27, 0.0, 0.0, 0.0, -0.70, -0.72)],  # 分液台待分液区
    "SPLIT_DONE_250ML_AREA_TRANSFER": [
        (2.99, -1.13, 0.0, 0.0, 0.0, -1.00, -0.03)],  # 250ml分液完成暂存区（转运）
    "SPLIT_DONE_500ML_AREA_TRANSFER": [
        (0.0,  0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],     # 500ml分液完成暂存区（转运） TODO

    # ── robot_b 分液任务点位 ──────────────────────────────────────────────────
    "TASK_PREPARE_SPLIT": [
        (0.0,  0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],     # 分液任务准备点位 TODO
    "HOME_ROBOT_B": [
        (-2.49, -0.68, 0.0, 0.0, 0.0, -0.71, 0.71)],  # home_split
    "WAITING_SPLIT_AREA_SPLIT": [
        (-0.33,  0.06, 0.0, 0.0, 0.0,  0.01, 1.00)],  # 分液台待分液区（分液）
    "WAITING_SPLIT_AREA_SPLIT_FORWARD": [
        (0.0,   0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],    # 待分液区前进进入点 TODO
    "EMPTY_BOTTLE_AREA_SPLIT": [
        (0.31,  0.05, 0.0, 0.0, 0.0,  0.00, 1.00)],   # 空瓶区（分液）
    "EMPTY_BOTTLE_AREA_SPLIT_FORWARD": [
        (0.0,   0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],    # 空瓶区前进进入点 TODO
    "SPLIT_LIQUID_AREA_SPLIT": [
        (1.05,  0.04, 0.0, 0.0, 0.0,  0.00, 1.00)],   # 分液台（分液）
    "SPLIT_LIQUID_AREA_SPLIT_FORWARD": [
        (0.0,   0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],    # 分液台前进进入点 TODO
    "SPLIT_DONE_250ML_AREA_SPLIT": [
        (-1.41,  0.09, 0.0, 0.0, 0.0,  0.01, 1.00)],  # 250ml分液完成暂存区（分液）
    "SPLIT_DONE_250ML_AREA_SPLIT_BACK": [
        (0.0,   0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],    # 250ml分液完成暂存区后退进入点 TODO
    "SPLIT_DONE_500ML_AREA_SPLIT": [
        (-0.91,  0.05, 0.0, 0.0, 0.0,  0.01, 1.00)],  # 500ml分液完成暂存区（分液）
    "CHROMATOGRAPH": [
        (0.0,   0.0,  0.0, 0.0, 0.0,  0.0,  1.0)],    # 色谱仪 TODO
}


class NavigationPose:
    """
    TJSH 项目导航点位。
    各属性在模块加载时从 robot_config.json 动态覆盖；若 config 缺失则使用硬编码默认值。
    访问方式与原来完全一致：NavigationPose.SCAN_TABLE、NavigationPose.HOME_ROBOT_B 等。

    注意：所有点位均为 ``[(x, y, z, qx, qy, qz, qw), ...]`` 格式（list-of-tuples）。
    多段路径时 list 包含多个元素；单目标点时 list 只包含一个元素。
    """
    pass


# 运行时从 config 加载，覆盖 NavigationPose 的类属性
# ALL 模式下使用 infrastructure 配置，单项目模式使用本目录配置
_tjsh_poses = load_nav_poses(
    project="TJSH",
    defaults=_TJSH_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "TJSH" else None,
)
for _pose_key, _pose_val in _tjsh_poses.items():
    setattr(NavigationPose, _pose_key, _pose_val)
