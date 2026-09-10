"""
KAIAO 项目专属常量
"""

import copy
import os
from infrastructure.pose_loader import load_nav_poses, get_active_project
from infrastructure.constants import ROSTopic, ROSTopicMessageType
from hardware.action_utils import ActionSpec


class KAIAOService:
    ROBOT_TASK = "/robot_task"


# ── KAIAO 机器人任务 Action Spec ─────────────────────────────────────────────
# topic 与消息类型统一见 infrastructure.constants（ROSTopic / ROSTopicMessageType）
KAIAO_TASK_ACTION_SPEC = ActionSpec(
    goal_topic=ROSTopic.ROBOT_TASK_ACTION_GOAL,
    feedback_topic=ROSTopic.ROBOT_TASK_ACTION_FEEDBACK,
    result_topic=ROSTopic.ROBOT_TASK_ACTION_RESULT,
    cancel_topic=ROSTopic.ROBOT_TASK_ACTION_CANCEL,
    status_topic=ROSTopic.ROBOT_TASK_ACTION_STATUS,
    goal_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_GOAL,
    feedback_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_FEEDBACK,
    result_msg_type=ROSTopicMessageType.ROBOT_TASK_ACTION_RESULT,
)


class KAIAOArea:
    """ROS 任务 area 参数：区分 AGV 小车与播种墙/货架。"""
    AGV_CAR = "agv_car"
    AGV_CAR0_0 = "agv_car0_0"
    SHELF = "shelf"
    SHELF0_0 = "shelf0_0"
    SHELF0_1 = "shelf0_1"
    SHELF0_2 = "shelf0_2"
    SHELF1_0 = "shelf1_0"
    SHELF1_1 = "shelf1_1"
    SHELF1_2 = "shelf1_2"
    COMPONENT_CAR = "component_car"


class KAIAOTask:
    PICK_UP_BOX         = "pick_up_box"
    PICK_UP_HEAVY_BOX   = "pick_up_heavy_box"   # 货架满箱过重时抓取；area 只能是 shelf
    PUT_DOWN_BOX        = "put_down_box"
    PUT_DOWN_HEAVY_BOX  = "put_down_heavy_box"  # 与 pick_up_heavy_box 成对；放箱动作
    PICK_UP_COMPONENT  = "pick_up_component"
    PUT_DOWN_COMPONENT = "put_down_component"
    ADJUST_POSE        = "adjust_pose"        # 离架后中间点姿态修正
    MODIFY_Z           = "modify_z"     # 调整机械臂至目标层高度


class KAIAOStep:
    IDLE               = "IDLE"
    NAVIGATING         = "NAVIGATING"
    PICKING_UP         = "PICKING_UP"
    PICKING_COMPONENT  = "PICKING_COMPONENT"
    ADJUSTING_POSE     = "ADJUSTING_POSE"
    NAVIGATING_TARGET  = "NAVIGATING_TARGET"
    PUTTING_DOWN       = "PUTTING_DOWN"
    PUTTING_COMPONENT  = "PUTTING_COMPONENT"
    DONE               = "DONE"


class KAIAOTimeout:
    ROBOT_ACTION = 800
    NAVIGATION   = 180


class KAIAONavTolerance:
    DISTANCE            = 0.08
    HEADING             = 0.08
    TRANSLATION_HEADING = 0.08


# 取箱前默认导航点位（货架侧）
DEFAULT_SHELF_NAV_AREA = "shelf"

# 零件类型（ROS extra_params.type）
COMPONENT_TYPES = (
    "black_screw",
    "black_tube",
    "black_square",
    "black_joystick",
    "black_fan",
    "yellow_hinge",
    "white_connector",
    "wire_harness",
)

# 单件重量 / 重箱阈值硬编码兜底。现场以 programs/KAIAO/robot_config.json → kaiao_component 为准。
_DEFAULT_COMPONENT_WEIGHT_KG = 0.2
COMPONENT_WEIGHT_KG = {name: _DEFAULT_COMPONENT_WEIGHT_KG for name in COMPONENT_TYPES}
HEAVY_BOX_THRESHOLD_KG = 0.8


def _kaiao_component_cfg() -> dict:
    try:
        from infrastructure.config_loader import load_config
        cfg = load_config().get("kaiao_component") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def get_component_weight_kg(component_type: str) -> float:
    """读取某种零件的单件重量(kg)，优先 robot_config.json → kaiao_component.weight_kg。"""
    weights = _kaiao_component_cfg().get("weight_kg") or {}
    if isinstance(weights, dict) and component_type in weights:
        try:
            return float(weights[component_type])
        except (TypeError, ValueError):
            pass
    return float(COMPONENT_WEIGHT_KG.get(component_type, _DEFAULT_COMPONENT_WEIGHT_KG))


def get_heavy_box_threshold_kg() -> float:
    """货架取箱改用 pick_up_heavy_box / put_down_heavy_box 的重量阈值(kg)。"""
    raw = _kaiao_component_cfg().get("heavy_box_threshold_kg")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return float(HEAVY_BOX_THRESHOLD_KG)


# 狭长走廊中间点（robot_config.json → kaiao_waypoint）。字段说明同时给图形化配置表单用。
KAIAO_WAYPOINT_KEY = "kaiao_waypoint"
KAIAO_WAYPOINT_INTRO = (
    "狭长走廊自动插入中间过渡点。走廊沿 Y：agv_car 在 −Y 端，零件车 component_car 在 +Y 端；"
    "两侧货架朝向 ±X。改完后须「重置系统」或重启 main.py 才生效。"
    "KAIAO 与 KAIAO_FLOW 共用这一段，与「📍 点位」写在同一份 robot_config.json。"
)
KAIAO_WAYPOINT_FIELDS = [
    {
        "key": "angle_threshold_deg",
        "type": "number",
        "group": "走廊判定",
        "label": "插入中间点的朝向差阈值 (°)",
        "default": 45.0,
        "comment": "起点与终点偏航角差超过该值才插入中间点。货架（朝 ±X）与走廊端点（朝 ±Y）通常差约 90°，低于阈值会视为朝向已接近、直达目标。",
    },
    {
        "key": "end_yaw_half_width_deg",
        "type": "number",
        "group": "走廊判定",
        "label": "端点朝向判定半宽 (°)",
        "default": 45.0,
        "comment": "相对 ±Y 轴左右各这么多度，算走廊端点（agv_car / component_car）；其余算货架。默认 45°。过窄（旧值 10°）时真机姿态稍偏就会把端点误判成货架。",
    },
    {
        "key": "pose_match_radius",
        "type": "number",
        "group": "走廊判定",
        "label": "点位匹配半径 (m)",
        "default": 0.28,
        "comment": "判断当前位姿是否靠近某个配置点位（如 shelf0_3）的距离阈值。必须小于相邻货架格间距，否则会把旁边一格也当成「近 AGV 格」。默认 0.28。",
    },
    {
        "key": "angle_offset_deg",
        "type": "number",
        "group": "端点 ↔ 货架",
        "label": "货架与端点之间的原地旋转角 (°)",
        "default": 90.0,
        "comment": "从货架去 agv_car / 零件车（或反过来）时，先平移再原地旋转的角度，现场一般为 90°。旋转方向由起终点朝向差自动决定。",
    },
    {
        "key": "end_to_side_dx",
        "type": "number",
        "group": "端点 ↔ 货架",
        "label": "端点→货架 平移 X (m)",
        "default": 0.1,
        "comment": "从 agv_car 或零件车去货架：中间平移点相对起点的 X 偏移（米）。走廊沿 Y，这项通常较小。",
    },
    {
        "key": "end_to_side_dy",
        "type": "number",
        "group": "端点 ↔ 货架",
        "label": "端点→货架 平移 Y (m)",
        "default": 0.0,
        "comment": "从端点去货架时中间平移点的 Y 偏移。走廊本身沿 Y，一般填 0，平移点的 Y 会对齐目标货架。",
    },
    {
        "key": "side_to_end_dx",
        "type": "number",
        "group": "端点 ↔ 货架",
        "label": "货架→端点 平移 X (m)",
        "default": 0.4,
        "comment": "普通货架格去 agv_car / 零件车（空手，或非近 AGV 格持箱）：先沿 X 平移这么多再转 90°。近 AGV 格持箱去 agv_car 不走这条，而走下面的「避让」。",
    },
    {
        "key": "side_to_end_dy",
        "type": "number",
        "group": "端点 ↔ 货架",
        "label": "货架→端点 平移 Y (m)",
        "default": 0.0,
        "comment": "普通货架→端点中间平移的 Y 偏移，通常为 0。",
    },
    {
        "key": "side_to_side_same_retreat_dx",
        "type": "number",
        "group": "货架互搬 / 离架后退",
        "label": "离架后退距离 (m)",
        "default": 0.3,
        "comment": "沿当前朝向反方向退入走廊的距离。货架互搬（有箱/无箱）以及近 AGV 格持箱去 agv_car 的第一步都用它。货架朝 +X 时 x 减小，朝 −X 时 x 增大。默认 0.30。校正 adjust_pose 发生在这个后退点到达之后。",
    },
    {
        "key": "side_to_side_approach_dx",
        "type": "number",
        "group": "货架互搬 / 离架后退",
        "label": "有箱互搬：目标正前方距离 (m)",
        "default": 0.4,
        "comment": "手上有箱、从一个货架格到另一个货架格：后退 → 转向零件车（+Y）→ 走到「目标点沿其朝向后退这么多」的位置（站在目标格正前方走廊里）→ 再转向目标进入。默认 0.40。空手互搬不用这项，后退后直达目标。",
    },
    {
        "key": "side_to_end_agv_clear_dy",
        "type": "number",
        "group": "近 AGV 货架持箱避让",
        "label": "远离 AGV 的平移距离 (m)",
        "default": 0.4,
        "comment": "仅当手上有箱、起点靠近「需避让的起点货架格」、终点靠近 agv_car：后退并 adjust 后，再沿走廊远离 AGV 平移这么多，然后才转 90° 去 agv_car。agv_car 离货架太近时，原地转 90° 会让箱子撞到货架。默认 0.40。",
    },
    {
        "key": "side_to_end_agv_clear_from",
        "type": "string_list",
        "group": "近 AGV 货架持箱避让",
        "label": "需避让的起点货架格",
        "default": ["shelf0_3", "shelf1_3"],
        "comment": "导航点位名，逗号分隔。对应货架编号列 (0,*,3) 和 (1,*,3)，即最靠近 agv_car 的那一列。只有起点靠近这些格、且手上有箱、终点是 agv_car 时才启用避让。",
    },
    {
        "key": "side_to_end_agv_clear_to",
        "type": "string_list",
        "group": "近 AGV 货架持箱避让",
        "label": "避让场景的终点",
        "default": ["agv_car0_0"],
        "comment": "终点靠近这些点位才走避让。去零件车仍用普通「货架→端点」路径，不会先远离 AGV。",
    },
    {
        "key": "side_to_side_mid_1",
        "type": "json",
        "group": "旧版异侧撤退点（备用）",
        "label": "异侧货架撤退点 1",
        "default": [[-0.58, -0.8, 0.0, 0.0, 0.0, 0.71, 0.71]],
        "comment": "旧版「货架→货架异侧」使用的走廊撤退点（靠近 AGV 一侧）。当前货架互搬已改为后退+转向零件车，这两项不再参与主路径，保留以便对照或回退。格式与点位相同：[[x,y,z,qx,qy,qz,qw]]。",
    },
    {
        "key": "side_to_side_mid_2",
        "type": "json",
        "group": "旧版异侧撤退点（备用）",
        "label": "异侧货架撤退点 2",
        "default": [[-0.58, 2.1, 0.0, 0.0, 0.0, -0.71, 0.71]],
        "comment": "旧版异侧撤退点（靠近零件车一侧）。含义同上。",
    },
]


def kaiao_waypoint_defaults() -> dict:
    return {f["key"]: copy.deepcopy(f["default"]) for f in KAIAO_WAYPOINT_FIELDS}


def kaiao_waypoint_comments() -> dict:
    return {f["key"]: f["comment"] for f in KAIAO_WAYPOINT_FIELDS}


def merge_kaiao_waypoint_config(raw) -> dict:
    """把 robot_config 的 kaiao_waypoint 与默认值合并，丢掉 _comment 等说明字段。"""
    out = kaiao_waypoint_defaults()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).startswith("_"):
                continue
            out[key] = value
    return out


_KAIAO_POSE_DEFAULTS: dict = {
    "home":    [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "agv_car0_0": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_0":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_1":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_2":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf0_3":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_0":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_1":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_2":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "shelf1_3":   [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "component_car": [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point1":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point2":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point3":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point4":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
    "point5":  [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)],
}


class NavigationPose:
    pass


_project_config_dir = (
    os.path.dirname(__file__)
    if get_active_project() not in ("ALL",)
    else None
)

_poses = load_nav_poses(
    "KAIAO", _KAIAO_POSE_DEFAULTS,
    project_config_dir=_project_config_dir,
    allow_extra_keys=True,
)
for _k, _v in _poses.items():
    setattr(NavigationPose, _k, _v)


def get_nav_pose(area_name: str):
    return getattr(NavigationPose, area_name, None)
