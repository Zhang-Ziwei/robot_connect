"""
兼容性转发模块 — 请勿在新代码中使用此文件。

pose_lock 已迁移至 programs/TJSH/pose_lock.py，后续可安全删除此文件。
"""
from programs.TJSH.pose_lock import (  # noqa: F401
    PoseLock, PoseLockContext, get_pose_lock, reset_pose_lock,
    CONFLICTING_POSES, CONFLICTING_POSES_REVERSE, ALL_PROTECTED_POSES,
)
