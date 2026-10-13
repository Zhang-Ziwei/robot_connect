"""展厅导航点位及机械臂任务配置名称。"""

from __future__ import annotations

import os

from infrastructure.pose_loader import get_active_project, load_nav_poses


class NavigationPose:
    """由配置提供实际导航坐标；没有配置时不提供虚构的默认点位。"""


_ZHANTING_POSE_DEFAULTS = {}
_poses = load_nav_poses(
    project="ZHANTING",
    defaults=_ZHANTING_POSE_DEFAULTS,
    project_config_dir=os.path.dirname(__file__) if get_active_project() == "ZHANTING" else None,
    allow_extra_keys=True,
)
for _name, _pose in _poses.items():
    setattr(NavigationPose, _name, _pose)


ARM_TASK_KEYS = (
    "load_to_opener",
    "opener_to_robot",
    "bottle_to_closer",
    "closer_to_loading",
)

LOADED_EVENT = "ZHANTING_LOADED"
