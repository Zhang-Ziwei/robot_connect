"""
KAIAO 任务处理器

职责：
    - PICK_BOX_TO_SP        ：异步，box_initial_area / box_target_area 为
                              { shelf_num: [编号,row,column], shelf_type: "agv_car"|"shelf" }
    - PICK_COMPONENT_TO_SP  ：异步，从 box_initial_area 取件，按 target 分拣至播种墙格位
    - PICK_UP_BOX / PUT_DOWN_BOX / NAVIGATION ：单步异步（HTTP 参数与 WAIC 相同，ROS extra_params 扩展）

每个异步任务结束后通过 CallbackSender 回调外部系统。
"""

import math
import random
import re
import threading
import uuid
from typing import Dict, Any, Optional, Tuple, List, Callable

from infrastructure.constants import (
    ErrorCode,
    NavigationState,
    make_error_response,
)
from infrastructure.error_logger import get_error_logger
from hardware.navigation_utils import (
    send_navigation_action,
    cancel_navigation_action,
    build_navigation_goal,
    is_robot_at_pose,
    get_robot_odom,
)
from hardware.robot_controller import RobotController
from hardware.task_utils import send_task_action, TaskFeedback, TaskResult
from core.task_state_machine import TaskStateMachine
from handlers.callback_sender import CallbackSender, get_callback_sender

from programs.KAIAO.constants import (
    KAIAOService,
    KAIAOTask,
    KAIAOStep,
    KAIAOTimeout,
    KAIAONavTolerance,
    KAIAOArea,
    COMPONENT_TYPES,
    KAIAO_TASK_ACTION_SPEC,
    get_nav_pose,
)

logger = get_error_logger()
_LOG = "KAIAO"


def _make_action_response(
    success: bool,
    action_type: str,
    description: str = "",
    message: str = "",
    code: int = 0,
) -> Dict:
    if not message:
        message = (
            f"动作 {action_type} 执行成功"
            if success
            else f"动作 {action_type} 执行失败"
        )
    return {
        "success":     success,
        "code":        code if success else (code or ErrorCode.INTERNAL_ERROR),
        "action_type": action_type,
        "description": description,
        "message":     message,
    }


def _parse_shelf_coords(raw, field_name: str) -> Tuple[Optional[Tuple[int, int, int]], Optional[str]]:
    """解析 [货架编号, row, column]。"""
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None, f"{field_name} 须为 [货架编号, row, column] 三元素列表"
    try:
        return (int(raw[0]), int(raw[1]), int(raw[2])), None
    except (TypeError, ValueError):
        return None, f"{field_name} 元素须为整数"


def _shelf_nav_area(shelf_number: int, column: int) -> str:
    """
    零件放件导航点（默认 shelf 类型）：{货架编号}_{column}。
    例: [0, *, 0] → shelf0_0
    """
    return f"shelf{int(shelf_number)}_{int(column)}"


def _typed_nav_area(shelf_type: str, shelf_number: int, column: int) -> str:
    """
    由 shelf_type + shelf_num[0]/[2] 推导导航点位名（第二位是列）。
    例: shelf_type=agv_car, [0, *, 0] → agv_car0_0
         shelf_type=shelf,   [0, *, 1] → shelf0_1
    """
    return f"{shelf_type}{int(shelf_number)}_{int(column)}"


def _box_extra_params(
    shelf_number: int,
    row: int,
    column: int,
) -> Dict[str, Any]:
    """
    箱子抓放基础 extra_params（仅 shelf_level）。
    same_level_movement 只在 put_down_box 时由调用方按起终点关系注入。
    """
    return {"shelf_level": int(row)}


def _shelf_side_from_nav(nav_area: str) -> Optional[int]:
    """
    从导航点名提取货架侧编号：shelf0_1 → 0，shelf1_3 → 1。
    非 shelf 点位（agv_car0_0 等）返回 None。
    """
    m = re.fullmatch(rf"{KAIAOArea.SHELF}(\d+)_(\d+)", str(nav_area or ""))
    return int(m.group(1)) if m else None


def _same_level_movement(src: Dict[str, Any], dst: Dict[str, Any]) -> int:
    """
    同一边货架→货架搬箱时返回 1，否则 0。

    判定依据是实际导航点名（nav_area）中的货架侧编号，而非直接取 shelf_num[0]，
    避免与导航点命名（{shelf_type}{shelf_num[0]}_{shelf_num[2]}）出现不一致。
    例：shelf0_* → shelf0_* 为 1；shelf1_* → shelf1_* 为 1；
        shelf0_* → shelf1_* / agv_car* 为 0。
    """
    src_side = _shelf_side_from_nav(src.get("nav_area"))
    dst_side = _shelf_side_from_nav(dst.get("nav_area"))
    if src_side is None or dst_side is None:
        return 0
    return 1 if src_side == dst_side else 0


#: 箱子抓放允许的 shelf_type
_BOX_SHELF_TYPES = frozenset({KAIAOArea.AGV_CAR, KAIAOArea.SHELF})


def _parse_box_location(
    raw, field_name: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    解析箱子位置对象::

        {
          "shelf_num":  [货架编号, row, column],
          "shelf_type": "agv_car" | "shelf"
        }

    返回 dict:
        shelf_type  — ROS area（如 agv_car / shelf）
        coords      — (shelf_number, row, column)
        nav_area    — 导航点位名 {type}{编号}_{column}（如 agv_car0_0 / shelf0_1）
        extra       — pick/put 的 extra_params（shelf_level = row）
    """
    if not isinstance(raw, dict):
        return None, (
            f"{field_name} 须为对象 "
            f'{{"shelf_num": [编号, row, column], "shelf_type": "agv_car"|"shelf"}}'
        )

    shelf_type = raw.get("shelf_type")
    if not isinstance(shelf_type, str) or not shelf_type:
        return None, f'{field_name}.shelf_type 须为非空字符串（如 "agv_car" / "shelf"）'
    if shelf_type not in _BOX_SHELF_TYPES:
        return None, (
            f"{field_name}.shelf_type 无效: {shelf_type!r}，"
            f"可选: {', '.join(sorted(_BOX_SHELF_TYPES))}"
        )

    coords, err = _parse_shelf_coords(raw.get("shelf_num"), f"{field_name}.shelf_num")
    if err:
        return None, err

    shelf_number, row, column = coords
    return {
        "shelf_type": shelf_type,
        "coords":     coords,
        "nav_area":   _typed_nav_area(shelf_type, shelf_number, column),
        "extra":      _box_extra_params(shelf_number, row, column),
    }, None


def _is_end_facing(yaw_rad: float, half_width_deg: float = 45.0) -> bool:
    """
    判断朝向是否为走廊端点（近似沿 ±y / yaw≈±90°）。

    half_width_deg：相对 y 轴左右允许偏差。默认 45°（|sin(yaw)| > sin(45°)）；
    旧值 10° 对应 |sin(yaw)| > sin(80°)，真机姿态偏差时易把端点误判为货架。
    """
    half = max(0.0, min(89.9, float(half_width_deg)))
    thr = math.sin(math.radians(90.0 - half))
    return abs(math.sin(yaw_rad)) > thr


def _compute_intermediate_waypoint(
    src_x: float, src_y: float, src_qz: float, src_qw: float,
    tgt_x: float, tgt_y: float, tgt_qz: float, tgt_qw: float,
    angle_threshold_deg: float,
    angle_offset_deg: float,
    end_to_side_dx: float,
    end_to_side_dy: float,
    side_to_end_dx: float,
    side_to_end_dy: float,
    side_to_side_mid_1: Optional[Tuple] = None,
    side_to_side_mid_2: Optional[Tuple] = None,
    end_yaw_half_width_deg: float = 45.0,
) -> Optional[Tuple[float, ...]]:
    """
    为狭长走廊场景生成中间过渡路径点。

    走廊端点（agv_car / component_car）朝向 y 轴（yaw ≈ ±90°）；
    货架（shelf）朝向 x 轴（yaw ≈ 0° 或 180°）。

    端点/货架判定：|sin(yaw)| > sin(90° - half_width)。
    half_width 默认 45°（相对 y 轴左右各 45° 内算端点；相对 x 轴左右各 45° 内算货架）。
    原先 half_width=10°（sin(80°)）过窄，真机姿态偏差时易把端点误判为货架。

    触发条件：起终点偏航角差超过 angle_threshold_deg。

    分三种情况处理：

        走廊端点 → 货架：
            仅生成「平移中间点」（朝向保持 src 不变），由 _prepend 再插入原地旋转点：
            mid_x = src_x - sign(Δx) * end_to_side_dx
            mid_y = tgt_y
            yaw = src_yaw

        货架 → 走廊端点：
            仅生成「平移中间点」（朝向保持 src 不变），由 _prepend 再插入原地旋转点：
            mid_x = src_x + sign(Δx) * side_to_end_dx
            mid_y = src_y + sign(Δy) * side_to_end_dy
            yaw = src_yaw

        货架 → 货架：
            使用 side_to_side_mid_1 / side_to_side_mid_2 中距起点较近的那个。

        z, qx, qy = 0

    返回 (x, y, 0.0, 0.0, 0.0, qz, qw) 或 None（不满足触发条件或无可用配置）。
    """
    src_yaw = 2.0 * math.atan2(src_qz, src_qw)
    tgt_yaw = 2.0 * math.atan2(tgt_qz, tgt_qw)

    # 归一化角差到 [-π, π]
    angle_diff = tgt_yaw - src_yaw
    while angle_diff >  math.pi:  angle_diff -= 2.0 * math.pi
    while angle_diff < -math.pi:  angle_diff += 2.0 * math.pi

    if abs(angle_diff) < math.radians(angle_threshold_deg):
        return None

    is_src_end = _is_end_facing(src_yaw, end_yaw_half_width_deg)
    is_tgt_end = _is_end_facing(tgt_yaw, end_yaw_half_width_deg)

    delta_x = tgt_x - src_x
    delta_y = tgt_y - src_y

    # ── 端点 → 端点：不在此生成中间点，由调用方发送原地旋转导航指令 ──────────
    if is_src_end and is_tgt_end:
        return None

    # ── 货架 → 货架：选距起点较近的自定义撤退点 ──────────────────────────────
    if not is_src_end and not is_tgt_end:
        candidates = [p for p in (side_to_side_mid_1, side_to_side_mid_2) if p is not None]
        if not candidates:
            return None
        def _dist(p):
            return math.hypot(float(p[0]) - src_x, float(p[1]) - src_y)
        chosen = min(candidates, key=_dist)
        return tuple(float(v) for v in chosen)

    if is_src_end and not is_tgt_end:
        # 走廊端点 → 货架：
        #   mid_x = 起点 x 向「目标 x 的反方向」平移 dx
        #   mid_y = 目标点 y
        #   朝向保持起点
        mid_x = src_x + ((-math.copysign(1.0, delta_x) * end_to_side_dx) if abs(delta_x) > 1e-6 else 0.0)
        mid_y = tgt_y
        return (mid_x, mid_y, 0.0, 0.0, 0.0, src_qz, src_qw)

    if not is_src_end and is_tgt_end:
        # 货架 → 走廊端点：从起点朝目标方向平移 dx/dy，朝向保持起点
        mid_x = src_x + ((math.copysign(1.0, delta_x) * side_to_end_dx) if abs(delta_x) > 1e-6 else 0.0)
        mid_y = src_y + ((math.copysign(1.0, delta_y) * side_to_end_dy) if abs(delta_y) > 1e-6 else 0.0)
        return (mid_x, mid_y, 0.0, 0.0, 0.0, src_qz, src_qw)

    return None


def _make_task_feedback_cb(label: str, robot_id: str) -> "Callable[[TaskFeedback], None]":
    """
    构造 send_task_action 的 feedback_callback。

    打印 feedback.status 和 feedback.current_params；
    current_params 非空时尝试 JSON 解析并格式化显示。
    """
    def _cb(fb: TaskFeedback) -> None:
        params_str = ""
        if fb.current_params:
            try:
                import json as _json
                params_str = f"  params={_json.loads(fb.current_params)}"
            except Exception:
                params_str = f"  params={fb.current_params!r}"
        print(f"  [KAIAO feedback][{robot_id}] {label}: status={fb.status!r}{params_str}")
        logger.info(_LOG, f"[{robot_id}] {label} feedback: {fb.status}{params_str}")
    return _cb


def _with_task_only_id(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """为每次下发指令的 extra_params 注入唯一 task_only_id（随机数）。"""
    out = dict(extra or {})
    out["task_only_id"] = str(uuid.uuid4().int)
    return out


#: 中间点 Planning Failed 时，在原 xy 附近随机重试的半径（米）与次数
_MID_JITTER_RADIUS_M = 0.02
_MID_PLANNING_MAX_RETRIES = 3


def _nav_causes_blob(res, err_msg: str = "") -> str:
    """把 NavigationResult 的 causes / raw_result / err_msg 拼成小写文本，便于匹配失败原因。"""
    parts: List[str] = []
    if err_msg:
        parts.append(str(err_msg))
    if res is None:
        return " ".join(parts).lower()
    for c in (getattr(res, "causes", None) or []):
        if isinstance(c, dict):
            for k in ("message", "msg", "description", "reason", "name"):
                if c.get(k) is not None:
                    parts.append(str(c.get(k)))
            parts.append(str(c))
        else:
            parts.append(str(c))
    raw = getattr(res, "raw_result", None)
    if raw:
        parts.append(str(raw))
    return " ".join(parts).lower()


def _nav_error_codes(res, err_msg: str = "") -> set:
    """从导航结果与 err_msg 中递归提取 int 错误码。"""
    codes = set()

    def _walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("code", "error_code", "errorCode"):
                    try:
                        codes.add(int(item))
                    except (TypeError, ValueError):
                        pass
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)
        elif isinstance(value, str):
            import re
            for m in re.finditer(r"['\"]?code['\"]?\s*[:=]\s*(\d+)", value):
                try:
                    codes.add(int(m.group(1)))
                except ValueError:
                    pass

    if res is not None:
        _walk(getattr(res, "causes", None) or [])
        _walk(getattr(res, "raw_result", None) or {})
    if err_msg:
        _walk(err_msg)
    return codes


def _is_planning_failed(res, err_msg: str = "") -> bool:
    """
    路径规划失败（真机常见 code=10009, msg='Planning Failed'）。
    注意 10009 不能单独作为 out of tolerate 的判定依据。
    """
    blob = _nav_causes_blob(res, err_msg)
    if "planning failed" in blob or "planning_failed" in blob:
        return True
    if "planning" in blob and "fail" in blob:
        return True
    codes = _nav_error_codes(res, err_msg)
    if 10009 in codes and not _is_out_of_tolerate(res, err_msg):
        return True
    return False


def _is_out_of_tolerate(res, err_msg: str = "") -> bool:
    """到达容差不满足（真机常见 code=10008, msg 含 tolerance/tolerate）。"""
    blob = _nav_causes_blob(res, err_msg)
    if (
        "out of tolerate" in blob
        or "out of tolerance" in blob
        or "out_of_tolerate" in blob
        or "out_of_tolerance" in blob
        or "arrival accuracy out of tolerance" in blob
    ):
        return True
    if "tolerat" in blob and "planning" not in blob:
        return True
    return 10008 in _nav_error_codes(res, err_msg)


def _jitter_waypoint_xy(wp, radius_m: float = _MID_JITTER_RADIUS_M):
    """在原路点 xy 的 radius_m 圆盘内随机取一点，其余分量不变。"""
    base = list(wp)
    angle = random.uniform(0.0, 2.0 * math.pi)
    r = random.uniform(0.0, radius_m)
    base[0] = float(wp[0]) + r * math.cos(angle)
    base[1] = float(wp[1]) + r * math.sin(angle)
    return type(wp)(base) if isinstance(wp, tuple) else base


def _kaiao_feedback_stamp_secs(feedback) -> Optional[int]:
    """读取 KAIAO 定制导航 feedback.header.stamp.secs。"""
    raw = getattr(feedback, "raw_feedback", None) or {}
    try:
        return int(((raw.get("header") or {}).get("stamp") or {}).get("secs"))
    except (TypeError, ValueError):
        return None


def _component_pick_extra(component_type: str) -> Dict[str, Any]:
    return {
        "shelf_level": 1,
        "type":        component_type,
        "number":      1,
    }


def _component_put_extra(shelf_level: int, component_type: str) -> Dict[str, Any]:
    return {
        "shelf_level": shelf_level,
        "type":        component_type,
        "number":      1,
    }


class KAIAOHandler:
    """KAIAO 任务处理器。"""

    def __init__(
        self,
        robots: Dict[str, RobotController] = None,
        task_state_machine: TaskStateMachine = None,
        callback_sender: CallbackSender = None,
    ):
        self.robots = robots or {}
        self.task_state_machine = (
            task_state_machine if task_state_machine is not None else TaskStateMachine()
        )
        self.callback_sender = (
            callback_sender if callback_sender is not None else get_callback_sender()
        )
        # 各机器人是否手上有箱子：pick_up_box 成功后为 True，put_down_box 成功后为 False
        self._holding_box: Dict[str, bool] = {}

    def _is_holding_box(self, robot_id: str) -> bool:
        return bool(self._holding_box.get(robot_id, False))

    def _set_holding_box(self, robot_id: str, holding: bool) -> None:
        prev = self._is_holding_box(robot_id)
        self._holding_box[robot_id] = bool(holding)
        if prev != bool(holding):
            logger.info(
                _LOG,
                f"[{robot_id}] holding_box: {prev} → {bool(holding)}",
            )

    def _get_robot(self, cmd_data: Dict) -> tuple:
        params   = cmd_data.get("params", {}) or {}
        robot_id = params.get("robot_id") or "robot_a"
        robot    = self.robots.get(robot_id)
        if robot is None:
            resp = make_error_response(
                ErrorCode.ROBOT_NOT_FOUND,
                f"机器人 {robot_id} 不存在",
            )
            return robot_id, None, resp
        return robot_id, robot, None

    def _busy_response(self, action_type: str, robot_id: str) -> Dict:
        state = self.task_state_machine.get_state()
        busy_task = state.get("cmd_id") or state.get("task_id") or "未知"
        return _make_action_response(
            False,
            action_type,
            message=f"机器人 {robot_id} 正忙（当前任务: {busy_task}）",
            code=ErrorCode.ROBOT_BUSY,
        )

    def _fail_async_task(
        self,
        task_id: str,
        action_type: str,
        err: str,
        *,
        set_error: bool = True,
    ) -> None:
        """异步任务失败：更新状态机，并向上层回调 success=False。"""
        if set_error:
            self.task_state_machine.set_error(err)
        self.callback_sender.send(
            cmd_id=task_id, cmd_type=action_type, success=False, message=err,
        )

    def _fail_if_nav(
        self,
        ok: bool,
        task_id: str,
        action_type: str,
        fallback_err: str,
    ) -> bool:
        """
        导航失败时回调 False 并返回 True（表示应中止）；
        成功时返回 False（继续执行）。
        _navigate 失败时通常已 set_error，此处不再重复写入。
        """
        if ok:
            return False
        err = getattr(self.task_state_machine, "error_message", None) or fallback_err
        self._fail_async_task(task_id, action_type, err, set_error=False)
        return True

    def _get_waypoint_config(self) -> Dict[str, Any]:
        """
        读取走廊中间点生成参数（来自 robot_config.json 的 kaiao_waypoint 节）。
        """
        try:
            from infrastructure.config_loader import load_config
            return load_config().get("kaiao_waypoint", {})
        except Exception:
            return {}

    def _prepend_intermediate_waypoint(
        self,
        robot: RobotController,
        waypoints: List,
        robot_id: str,
        mode: str = 'auto',
    ) -> "Tuple[List, str]":
        """
        根据当前机器人位置和目标终点，判断走廊导航场景并处理中间过渡点。

        参数:
            mode:
                'auto'  — 自动检测场景并插入中间点（默认）
                'skip'  — 跳过中间点生成，直接返回原始路点

        返回:
            (waypoints, nav_case) 二元组：
                'end_to_end'  — waypoints[0] 为原地旋转路点
                'end_to_side' / 'side_to_end' — waypoints[0]=平移, [1]=原地旋转, 其后为目标
                'side_to_side' — 异侧货架，已插入单个撤退点
                'side_to_side_same' — 同侧货架且手上有箱：waypoints[0]=后退, [1]=对齐目标y, 其后为目标
                'no_change'   — 无需中间点（含同侧无箱时直达目标）
        """
        if mode == 'skip':
            return waypoints, 'no_change'
        wp_cfg = self._get_waypoint_config()
        if not wp_cfg:
            return waypoints, 'no_change'

        odom = get_robot_odom(robot, timeout=3.0)
        if not odom:
            logger.warning(_LOG, f"[{robot_id}] 无法获取当前位置，跳过中间过渡点生成")
            return waypoints, 'no_change'

        src_x  = float(odom["Position_Point_X_In"]) if odom.get("Position_Point_X_In") is not None else 0.0
        src_y  = float(odom["Position_Point_Y_In"]) if odom.get("Position_Point_Y_In") is not None else 0.0
        src_qz = float(odom["Orientation_Z_In"]) if odom.get("Orientation_Z_In") is not None else 0.0
        # 注意：yaw=180° 时 qw=0.0，不能用 `or 1`（会把 0 当成缺省）
        src_qw = float(odom["Orientation_W_In"]) if odom.get("Orientation_W_In") is not None else 1.0

        final  = waypoints[-1]
        tgt_x  = float(final[0])
        tgt_y  = float(final[1])
        tgt_qz = float(final[5])
        tgt_qw = float(final[6])

        # 端点→端点：判断是否需要先原地旋转180°
        threshold = wp_cfg.get("angle_threshold_deg", 45.0)
        end_half  = float(wp_cfg.get("end_yaw_half_width_deg", 45.0))
        src_yaw   = 2.0 * math.atan2(src_qz, src_qw)
        tgt_yaw   = 2.0 * math.atan2(tgt_qz, tgt_qw)
        angle_diff = tgt_yaw - src_yaw
        while angle_diff >  math.pi:  angle_diff -= 2.0 * math.pi
        while angle_diff < -math.pi:  angle_diff += 2.0 * math.pi
        is_src_end = _is_end_facing(src_yaw, end_half)
        is_tgt_end = _is_end_facing(tgt_yaw, end_half)

        if abs(angle_diff) >= math.radians(threshold) and is_src_end and is_tgt_end:
            # 端点→端点：在 waypoints 头部插入原地旋转路点
            rot_yaw = src_yaw + math.pi
            rot_wp  = (src_x, src_y, 0.0, 0.0, 0.0,
                       math.sin(rot_yaw / 2.0), math.cos(rot_yaw / 2.0))
            return [rot_wp] + waypoints, 'end_to_end'

        # 同侧货架→货架（角差小、两端均非走廊端点）：
        #   手上有箱 → ①后退 ②平移到目标 y ③再去目标
        #   手上无箱 → 直接前往目标（不插中间点）
        if (
            not is_src_end and not is_tgt_end
            and abs(angle_diff) < math.radians(threshold)
        ):
            if not self._is_holding_box(robot_id):
                logger.info(
                    _LOG,
                    f"[{robot_id}] 同侧货架移动但手上无箱，直达目标（跳过后退/平移）",
                )
                return waypoints, 'no_change'
            retreat = float(wp_cfg.get("side_to_side_same_retreat_dx", 0.3))
            # 朝向反方向后退进入走廊：yaw=0 → x减小；yaw=180° → x增大
            retreat_x = src_x - math.cos(src_yaw) * retreat
            retreat_wp = (retreat_x, src_y, 0.0, 0.0, 0.0, src_qz, src_qw)
            align_wp = (retreat_x, tgt_y, 0.0, 0.0, 0.0, src_qz, src_qw)
            logger.info(
                _LOG,
                f"[{robot_id}] 同侧货架移动且手上有箱，插入后退/对齐中间点",
            )
            return [retreat_wp, align_wp] + waypoints, 'side_to_side_same'

        def _to_tuple(raw):
            if raw is None:
                return None
            if isinstance(raw[0], (list, tuple)):
                raw = raw[0]
            return tuple(float(v) for v in raw)

        s2s_mid_1 = _to_tuple(wp_cfg.get("side_to_side_mid_1"))
        s2s_mid_2 = _to_tuple(wp_cfg.get("side_to_side_mid_2"))

        mid = _compute_intermediate_waypoint(
            src_x, src_y, src_qz, src_qw,
            tgt_x, tgt_y, tgt_qz, tgt_qw,
            threshold,
            wp_cfg.get("angle_offset_deg",     90.0),
            wp_cfg.get("end_to_side_dx",        0.3),
            wp_cfg.get("end_to_side_dy",        0.3),
            wp_cfg.get("side_to_end_dx",        0.5),
            wp_cfg.get("side_to_end_dy",        0.3),
            s2s_mid_1,
            s2s_mid_2,
            end_half,
        )

        if mid is None:
            return waypoints, 'no_change'

        if not is_src_end and not is_tgt_end:
            return [mid] + waypoints, 'side_to_side'

        # 端点↔货架：拆成「平移(朝向不变)」+「原地旋转 angle_offset_deg」
        nav_case = 'end_to_side' if is_src_end else 'side_to_end'
        angle_offset = float(wp_cfg.get("angle_offset_deg", 90.0))
        rot_yaw = src_yaw + math.copysign(math.radians(angle_offset), angle_diff)
        rot_wp = (
            float(mid[0]), float(mid[1]), 0.0, 0.0, 0.0,
            math.sin(rot_yaw / 2.0), math.cos(rot_yaw / 2.0),
        )
        return [mid, rot_wp] + waypoints, nav_case

    def _resolve_pose(self, area_name: str, label: str, robot_id: str) -> tuple:
        pose = get_nav_pose(area_name)
        if pose is None:
            msg = (
                f"[{robot_id}] {label}: 未找到点位 '{area_name}'，"
                f"请在 robot_config.json 的 navigation_poses.KAIAO 中配置"
            )
            logger.error(_LOG, msg)
            return None, msg
        if isinstance(pose, list) and pose and isinstance(pose[0], (list, tuple)):
            return pose, None
        if isinstance(pose, (list, tuple)) and len(pose) == 7 and isinstance(pose[0], (int, float)):
            return [pose], None
        return [pose], None

    def _navigate(
        self,
        robot: RobotController,
        area_name: str,
        label: str,
        robot_id: str,
        task_id: str = "",
        mode: str = 'auto',
        waypoints: Optional[List] = None,
        mid_send: str = 'split',
    ) -> bool:
        """
        统一导航入口。

        参数:
            mode:
                'auto' — 按走廊场景自动插入中间点（首次）；目标失败重试只发原始目标点
                'skip' — 不生成中间点，直接导航给定/解析出的路点
            waypoints:
                若传入则跳过点位名解析，直接使用该路点列表
            mid_send:
                'split' — 分多次 Action（默认）：
                    · end_to_side / side_to_end：①平移 → ②原地旋转 → ③目标
                    · side_to_side_same：①后退 → ②对齐目标y → ③目标
                    · side_to_side：①撤退点 → ②目标
                'batch' — 中间点与目标在同一次 Action 一口气发送
                （端点→端点原地旋转始终分两次，不受本参数影响）

            中间点失败策略（split）:
                · causes 含 Planning Failed → 在原中点 0.02m 内随机抖动后重发中间点
                · causes 含 out of tolerate（或 code=10009）→ 跳过该中间点，改发目标点
        """
        if mid_send not in ('batch', 'split'):
            raise ValueError(f"mid_send 须为 'batch' 或 'split'，收到: {mid_send!r}")

        if waypoints is None:
            waypoints, err = self._resolve_pose(area_name, label, robot_id)
            if err:
                if task_id:
                    self.task_state_machine.set_error(err)
                return False

        # 保存原始目标路点：失败重试时只用目标，不再带中间点
        original_waypoints = list(waypoints)

        waypoints, nav_case = self._prepend_intermediate_waypoint(
            robot, waypoints, robot_id, mode,
        )

        _CASE_DESC = {
            'end_to_end':  '端点→端点（先原地旋转180°）',
            'end_to_side': '端点→货架（平移→原地旋转→目标）',
            'side_to_end': '货架→端点（平移→原地旋转→目标）',
            'side_to_side':'货架→货架异侧（插入最近撤退点）',
            'side_to_side_same': '货架→货架同侧（后退→对齐y→目标）',
            'no_change':   '直接导航（无中间点）',
        }
        case_desc = _CASE_DESC.get(nav_case, nav_case)
        logger.info(
            _LOG,
            f"[{robot_id}] 导航到 {label}：{case_desc}  mid_send={mid_send}  "
            f"waypoints={len(waypoints)}",
        )
        print(
            f"  [KAIAO 导航][{robot_id}] {label}  场景={case_desc}  "
            f"mid_send={mid_send}  waypoints={waypoints}"
        )

        _RETRYABLE = {NavigationState.FAILED, NavigationState.ABORTED}
        mid_feedback_skip = {"value": False}

        def _send_nav(
            wps,
            attempt_label: str = "",
            skip_mid_on_feedback_secs_one: bool = False,
        ):
            """发送一次导航 Action。max_attempts=1：禁止底层用同一 goal（含中间点）重发。"""
            mid_feedback_skip["value"] = False
            g = build_navigation_goal(
                wps,
                distance_tolerance=KAIAONavTolerance.DISTANCE,
                heading_tolerance=KAIAONavTolerance.HEADING,
                translation_enable=True,
                translation_heading=KAIAONavTolerance.TRANSLATION_HEADING,
            )
            pfx = f"[{robot_id}] {label}{attempt_label}"

            def _feedback_cb(fb):
                secs = _kaiao_feedback_stamp_secs(fb)
                print(
                    f"  [KAIAO 导航]{pfx}: {fb.state.name}"
                    f"{f' stamp.secs={secs}' if secs is not None else ''}"
                )
                if (
                    skip_mid_on_feedback_secs_one
                    and secs == 1
                    and not mid_feedback_skip["value"]
                ):
                    # KAIAO 定制协议：中间点 feedback stamp.secs=1 表示
                    # 无需继续执行当前点。立即取消当前 goal，随后由业务流程
                    # 继续下一个中间点或最终目标。最终目标不启用此规则。
                    mid_feedback_skip["value"] = True
                    logger.warning(
                        _LOG,
                        f"{pfx} 中间点 feedback stamp.secs=1，"
                        f"取消当前导航并继续后续步骤",
                    )
                    print(
                        f"  [KAIAO 导航]{pfx}: stamp.secs=1，"
                        f"取消当前中间点并继续"
                    )
                    if not cancel_navigation_action(robot):
                        logger.error(
                            _LOG,
                            f"{pfx} 收到 stamp.secs=1，但取消当前导航失败",
                        )

            res = send_navigation_action(
                robot, g,
                feedback_callback=_feedback_cb,
                timeout=KAIAOTimeout.NAVIGATION,
                max_attempts=1,
            )
            if res is None:
                return None, f"{pfx} 超时或无响应"
            if res.succeeded:
                return res, None
            err_msg = f"{pfx} 失败: {res.state.name}"
            if getattr(res, 'causes', None):
                err_msg += f" causes={res.causes}"
            return res, err_msg

        def _send_mid_waypoint(mid_wp, step_label: str):
            """
            发送中间点导航。

            - 成功 → ('success', res, None)
            - Planning Failed → 在原中点 0.02m 内随机抖动后重发，最多 _MID_PLANNING_MAX_RETRIES 次；
              仍失败则跳过中间点 ('skip', ...)
            - out of tolerate → 跳过该中间点，改发目标 ('skip', ...)
            - 其它失败 → ('fail', ...) 中止整条导航（不跳过中间点）
            """
            original = list(mid_wp)
            send_wp = mid_wp
            last_res, last_err = None, None
            for attempt in range(_MID_PLANNING_MAX_RETRIES + 1):
                if attempt == 0:
                    attempt_label = f" {step_label}"
                else:
                    send_wp = _jitter_waypoint_xy(original, _MID_JITTER_RADIUS_M)
                    attempt_label = (
                        f" {step_label} Planning重试{attempt} "
                        f"jitter=({send_wp[0]:.4f},{send_wp[1]:.4f})"
                    )
                    logger.warning(
                        _LOG,
                        f"[{robot_id}] {step_label} Planning Failed，"
                        f"在原中点±{_MID_JITTER_RADIUS_M}m 内重试"
                        f" #{attempt}: ({send_wp[0]:.4f}, {send_wp[1]:.4f})",
                    )
                    print(
                        f"  [KAIAO 导航][{robot_id}] {step_label} Planning Failed → "
                        f"抖动重试#{attempt}: ({send_wp[0]:.4f}, {send_wp[1]:.4f})"
                    )

                last_res, last_err = _send_nav(
                    [send_wp],
                    attempt_label,
                    skip_mid_on_feedback_secs_one=True,
                )
                if mid_feedback_skip["value"]:
                    logger.info(
                        _LOG,
                        f"[{robot_id}] {step_label} 已按 feedback stamp.secs=1 "
                        f"取消，继续后续步骤",
                    )
                    return "advance", last_res, None
                if last_err is None:
                    return "success", last_res, None

                if _is_planning_failed(last_res, last_err):
                    if attempt < _MID_PLANNING_MAX_RETRIES:
                        continue
                    logger.warning(
                        _LOG,
                        f"[{robot_id}] {step_label} Planning Failed 重试耗尽，跳过中间点改试仅目标: {last_err}",
                    )
                    print(
                        f"  [KAIAO 导航][{robot_id}] {step_label} Planning Failed "
                        f"重试耗尽，跳过中间点"
                    )
                    return "skip", last_res, last_err

                if _is_out_of_tolerate(last_res, last_err):
                    logger.warning(
                        _LOG,
                        f"[{robot_id}] {step_label} out of tolerate，跳过中间点改试仅目标: {last_err}",
                    )
                    print(
                        f"  [KAIAO 导航][{robot_id}] {step_label} "
                        f"out of tolerate，跳过中间点"
                    )
                    return "skip", last_res, last_err

                logger.error(
                    _LOG,
                    f"[{robot_id}] {step_label}失败（非 Planning/out of tolerate）: {last_err}",
                )
                print(f"  [KAIAO 导航][{robot_id}] {step_label}失败，中止导航")
                return "fail", last_res, last_err

            return "fail", last_res, last_err

        def _navigate_with_retry(wps_first, wps_retry):
            """首次用 wps_first，失败后重试用 wps_retry（仅目标）。"""
            res, err = _send_nav(wps_first, "")
            if err is None:
                return res, None
            if res is not None and res.state in _RETRYABLE:
                for attempt in range(2, 4):
                    logger.warning(_LOG,
                        f"[{robot_id}] 导航到 {label} 失败，第{attempt}次重试（仅目标点，无中间点）")
                    print(f"  [KAIAO 导航][{robot_id}] {label} 重试{attempt-1}：仅目标点")
                    res2, err2 = _send_nav(wps_retry, f" 重试{attempt-1}")
                    if err2 is None:
                        return res2, None
                    if res2 is None or res2.state not in _RETRYABLE:
                        return res2, err2
                return res, err
            return res, err

        if nav_case == 'end_to_end':
            rot_wp, target_wps = waypoints[0], waypoints[1:]
            logger.info(_LOG, f"[{robot_id}] 端点→端点 步骤1：原地旋转180°")
            print(f"  [KAIAO 导航][{robot_id}] 步骤1 原地旋转180°: {rot_wp}")
            status, res, err = _send_mid_waypoint(rot_wp, "旋转180°")
            if status == "fail":
                if err:
                    logger.error(_LOG, err)
                    if task_id:
                        self.task_state_machine.set_error(err)
                return False
            logger.info(_LOG, f"[{robot_id}] 端点→端点 步骤2：导航到目标 {label}")
            print(f"  [KAIAO 导航][{robot_id}] 步骤2 导航到目标 {label}")
            if status == "skip":
                res, err = _navigate_with_retry(original_waypoints, original_waypoints)
            else:
                res, err = _navigate_with_retry(target_wps, original_waypoints)
        elif (
            mid_send == 'split'
            and nav_case in ('end_to_side', 'side_to_end', 'side_to_side_same')
            and len(waypoints) >= 3
        ):
            # 三步 Action：①中间点A ②中间点B ③目标
            # end/side: 平移→旋转；同侧货架: 后退→对齐目标y
            step1_wp, step2_wp, target_wps = waypoints[0], waypoints[1], waypoints[2:]
            step1_name = "后退" if nav_case == 'side_to_side_same' else "平移中间点"
            step2_name = "对齐目标y" if nav_case == 'side_to_side_same' else "原地旋转"
            logger.info(_LOG, f"[{robot_id}] {label} 分步导航①：{step1_name} {step1_wp[:2]}")
            print(f"  [KAIAO 导航][{robot_id}] 步骤1 {step1_name}: {step1_wp[:2]}")
            status, res, err = _send_mid_waypoint(step1_wp, step1_name)
            if status == "fail":
                if err:
                    logger.error(_LOG, err)
                    if task_id:
                        self.task_state_machine.set_error(err)
                return False
            if status == "skip":
                res, err = _navigate_with_retry(original_waypoints, original_waypoints)
            else:
                logger.info(_LOG, f"[{robot_id}] {label} 分步导航②：{step2_name}")
                print(
                    f"  [KAIAO 导航][{robot_id}] 步骤2 {step2_name}: "
                    f"{step2_wp[:2]} qz/qw={step2_wp[5]:.3f}/{step2_wp[6]:.3f}"
                )
                status, res, err = _send_mid_waypoint(step2_wp, step2_name)
                if status == "fail":
                    if err:
                        logger.error(_LOG, err)
                        if task_id:
                            self.task_state_machine.set_error(err)
                    return False
                if status == "skip":
                    res, err = _navigate_with_retry(original_waypoints, original_waypoints)
                else:
                    logger.info(_LOG, f"[{robot_id}] {label} 分步导航③：目标点")
                    print(f"  [KAIAO 导航][{robot_id}] 步骤3 目标点 {label}")
                    res, err = _navigate_with_retry(target_wps, original_waypoints)
        elif (
            mid_send == 'split'
            and nav_case == 'side_to_side'
            and len(waypoints) >= 2
        ):
            mid_wp, target_wps = waypoints[0], waypoints[1:]
            logger.info(_LOG, f"[{robot_id}] {label} 分步导航 步骤1：中间点 {mid_wp[:2]}")
            print(f"  [KAIAO 导航][{robot_id}] 步骤1 中间点: {mid_wp[:2]}")
            status, res, err = _send_mid_waypoint(mid_wp, "中间点")
            if status == "fail":
                if err:
                    logger.error(_LOG, err)
                    if task_id:
                        self.task_state_machine.set_error(err)
                return False
            if status == "skip":
                res, err = _navigate_with_retry(original_waypoints, original_waypoints)
            else:
                logger.info(_LOG, f"[{robot_id}] {label} 分步导航 步骤2：目标点")
                print(f"  [KAIAO 导航][{robot_id}] 步骤2 目标点 {label}")
                res, err = _navigate_with_retry(target_wps, original_waypoints)
        else:
            res, err = _navigate_with_retry(waypoints, original_waypoints)

        if err:
            logger.error(_LOG, err)
            if task_id: self.task_state_machine.set_error(err)
            return False

        logger.info(_LOG, f"[{robot_id}] 导航到 {label} 成功（{case_desc}, mid_send={mid_send}）")
        return True


    def _single_step_box_extras(self, params: Dict, for_pick: bool) -> Tuple[str, Dict[str, Any]]:
        """单步抓放：robot_area + shelf_level（可选）。"""
        robot_area = params.get(
            "robot_area",
            KAIAOArea.AGV_CAR if for_pick else KAIAOArea.SHELF,
        )
        shelf_level = int(params.get("shelf_level", 0))
        extra = {"shelf_level": shelf_level}
        # 单步 PUT_DOWN_BOX 才带 same_level_movement（默认 0，可 HTTP 显式传入）
        if not for_pick:
            same_lv = int(params.get("same_level_movement", 0) or 0)
            extra["same_level_movement"] = 1 if same_lv else 0
        return robot_area, extra

    # ── 公开命令处理器 ────────────────────────────────────────────────────────

    def handle_pick_box_to_sp(self, cmd_data: Dict) -> Dict:
        """
        PICK_BOX_TO_SP —— 从 box_initial_area 取箱，搬至 box_target_area（目前一次只搬一只）。

        params:
            box_initial_area / box_target_area:
                {
                  "shelf_num":  [货架编号, row, column],
                  "shelf_type": "agv_car" | "shelf"
                }
                - shelf_type → ROS area，并参与导航点命名
                - shelf_num[0]/[2]（编号 + column）→ 导航点（如 agv_car0_0 / shelf0_1）
                - shelf_num[1]（row）→ shelf_level（垂直层 0–3）
            robot_id: 可选
        """
        action_type = "PICK_BOX_TO_SP"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")

        src, src_err = _parse_box_location(
            params.get("box_initial_area"), "box_initial_area",
        )
        if src_err:
            return _make_action_response(
                False, action_type, message=src_err, code=ErrorCode.INVALID_PARAMS,
            )

        dst, dst_err = _parse_box_location(
            params.get("box_target_area"), "box_target_area",
        )
        if dst_err:
            return _make_action_response(
                False, action_type, message=dst_err, code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        task_id = cmd_id or str(uuid.uuid4())
        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        pick_nav  = src["nav_area"]
        put_nav   = dst["nav_area"]
        pick_area = src["shelf_type"]
        put_area  = dst["shelf_type"]
        same_lv   = _same_level_movement(src, dst)
        pick_extra = dict(src["extra"])
        put_extra  = dict(dst["extra"])
        # 仅 put_down_box 需要 same_level_movement；pick 不带该字段
        put_extra["same_level_movement"] = same_lv
        pick_extra.pop("same_level_movement", None)
        logger.info(
            _LOG,
            f"[{robot_id}] same_level_movement={same_lv} "
            f"(pick_nav={pick_nav} side={_shelf_side_from_nav(pick_nav)}, "
            f"put_nav={put_nav} side={_shelf_side_from_nav(put_nav)}, "
            f"src_shelf_num={list(src['coords'])}, dst_shelf_num={list(dst['coords'])})",
        )

        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            summary = (
                f"{pick_area}{src['coords']}@{pick_nav} → "
                f"{put_area}{dst['coords']}@{put_nav}"
            )
            try:
                '''result = send_task_action(
                    robot, task=KAIAOTask.PICK_UP_BOX, area="agv_car",
                    extra_params={"shelf_level": 2},
                    spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                    feedback_callback=_make_task_feedback_cb("pick_up_box", robot_id),
                )
                input("Press Enter to continue...")'''
                '''if not self._navigate(robot, "component_car", "component_car", robot_id, task_id):
                    return
                if not self._navigate(robot, "shelf0_2", "shelf0_2", robot_id, task_id):
                    return
                input("Press Enter to continue...")'''
                '''self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"搬运完成: {summary}",
                )
                input("Press Enter to continue...")'''


                if not is_robot_at_pose(
                    robot, get_nav_pose(pick_nav),
                    KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        KAIAOStep.NAVIGATING, f"导航到取箱位 {pick_nav}"
                    )
                    if not self._navigate(robot, pick_nav, pick_nav, robot_id, task_id):
                        if self._fail_if_nav(
                            False, task_id, action_type,
                            f"[{robot_id}] 导航到取箱位 {pick_nav} 失败",
                        ):
                            return

                self.task_state_machine.update_step_label(
                    KAIAOStep.PICKING_UP,
                    f"抓取箱子 area={pick_area} {src['coords']}",
                )
                result = send_task_action(
                    robot, task=KAIAOTask.PICK_UP_BOX, area=pick_area,
                    extra_params=_with_task_only_id(pick_extra),
                    spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                    feedback_callback=_make_task_feedback_cb("pick_up_box", robot_id),
                )
                # result = robot.send_service_request_task(
                #     KAIAOService.ROBOT_TASK, task=KAIAOTask.PICK_UP_BOX,
                #     area=pick_area, extra_params=pick_extra, maxtime=KAIAOTimeout.ROBOT_ACTION,
                # )
                if not result:
                    err = f"[{robot_id}] pick_up_box 失败: {result.error_msg}"
                    logger.error(_LOG, err)
                    self._fail_async_task(task_id, action_type, err)
                    return
                logger.info(_LOG, f"[{robot_id}] pick_up_box 成功")
                self._set_holding_box(robot_id, True)

                if not is_robot_at_pose(
                    robot, get_nav_pose(put_nav),
                    KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        KAIAOStep.NAVIGATING_TARGET, f"导航到放箱位 {put_nav}"
                    )
                    if not self._navigate(robot, put_nav, put_nav, robot_id, task_id):
                        if self._fail_if_nav(
                            False, task_id, action_type,
                            f"[{robot_id}] 导航到放箱位 {put_nav} 失败",
                        ):
                            return

                self.task_state_machine.update_step_label(
                    KAIAOStep.PUTTING_DOWN,
                    f"放置箱子 area={put_area} {dst['coords']}",
                )
                result = send_task_action(
                    robot, task=KAIAOTask.PUT_DOWN_BOX, area=put_area,
                    extra_params=_with_task_only_id(put_extra),
                    spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                    feedback_callback=_make_task_feedback_cb("put_down_box", robot_id),
                )
                # result = robot.send_service_request_task(
                #     KAIAOService.ROBOT_TASK, task=KAIAOTask.PUT_DOWN_BOX,
                #     area=put_area, extra_params=put_extra, maxtime=KAIAOTimeout.ROBOT_ACTION,
                # )
                if not result:
                    err = f"[{robot_id}] put_down_box 失败: {result.error_msg}"
                    logger.error(_LOG, err)
                    self._fail_async_task(task_id, action_type, err)
                    return
                logger.info(_LOG, f"[{robot_id}] put_down_box 成功")
                self._set_holding_box(robot_id, False)

                self.task_state_machine.complete_task(True, summary)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"搬运完成: {summary}",
                )
                logger.info(_LOG, f"[{robot_id}] PICK_BOX_TO_SP 完成: {summary}")

            except Exception as exc:
                err = f"[{robot_id}] PICK_BOX_TO_SP 异常: {exc}"
                logger.exception_occurred(_LOG, "PICK_BOX_TO_SP", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=False, message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"kaiao-pick-box-{robot_id}"
        ).start()

        return _make_action_response(True, action_type, message=f"动作 {action_type} 已受理，后台执行中")

    def _parse_component_targets(self, params: Dict) -> Tuple[
        Optional[List[Dict[str, Any]]], Optional[str]
    ]:
        """
        解析播种墙目的地，支持三种写法：
          - target:  { ... }           单个对象
          - target:  [ {...}, {...} ]  列表（与当前测试命令一致）
          - targets: [ {...}, {...} ]  列表（兼容旧字段名）
        """
        raw = params.get("targets")
        if raw is None:
            raw = params.get("target")

        if raw is None:
            return None, "缺少必要参数: target（或 targets 列表）"

        if isinstance(raw, dict):
            items = [raw]
        elif isinstance(raw, list):
            if not raw:
                return None, "target/targets 须为非空列表"
            items = raw
        else:
            return None, "target 须为对象或非空列表（targets 同）"

        parsed = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                return None, f"target[{idx}] 须为对象"
            coords, err = _parse_shelf_coords(
                item.get("box_target_area"), f"target[{idx}].box_target_area",
            )
            if err:
                return None, err
            try:
                comp_num = int(item.get("component_number", 0))
            except (TypeError, ValueError):
                return None, f"target[{idx}].component_number 须为整数"
            if comp_num <= 0:
                return None, f"target[{idx}].component_number 须大于 0"
            parsed.append({"coords": coords, "component_number": comp_num})
        return parsed, None

    def handle_pick_component_to_sp(self, cmd_data: Dict) -> Dict:
        """
        PICK_COMPONENT_TO_SP —— 从 box_initial_area 抓取零件，分拣至 target 指定播种墙格位。

        params:
            box_initial_area: 取件导航点位名（如 point1）
            component_type:   零件类型
            target:           单个对象，或对象列表（多目的地）
                              每项: { box_target_area: [shelf, row, column], component_number }
                              [0]/[2]（编号 + column）→ 放件导航点 shelf{0}_{2}
                              [1]（row）→ 放置层 shelf_level
            targets:          可选，同 target 列表写法（兼容旧字段名）
        """
        action_type = "PICK_COMPONENT_TO_SP"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        pick_nav    = params.get("box_initial_area", "")
        comp_type   = params.get("component_type", "")

        if not pick_nav or not isinstance(pick_nav, str):
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: box_initial_area（导航点位名称）",
                code=ErrorCode.INVALID_PARAMS,
            )
        if not comp_type:
            return _make_action_response(
                False, action_type,
                message="缺少必要参数: component_type",
                code=ErrorCode.INVALID_PARAMS,
            )
        if comp_type not in COMPONENT_TYPES:
            return _make_action_response(
                False, action_type,
                message=f"未知零件类型: {comp_type}，可选: {', '.join(COMPONENT_TYPES)}",
                code=ErrorCode.INVALID_PARAMS,
            )

        targets, target_err = self._parse_component_targets(params)
        if target_err:
            return _make_action_response(
                False, action_type, message=target_err, code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )

        task_id = cmd_id or str(uuid.uuid4())
        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        total_num = sum(t["component_number"] for t in targets)

        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            summary = f"{comp_type} x{total_num} from {pick_nav}"
            piece_idx = 0
            try:
                # 导航到取件位
                if not is_robot_at_pose(
                    robot, get_nav_pose(pick_nav),
                    KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        KAIAOStep.NAVIGATING, f"导航到取件位 {pick_nav}"
                    )
                    if not self._navigate(robot, pick_nav, pick_nav, robot_id, task_id):
                        if self._fail_if_nav(
                            False, task_id, action_type,
                            f"[{robot_id}] 导航到取件位 {pick_nav} 失败",
                        ):
                            return

                for dest in targets:
                    dest_coords = dest["coords"]
                    dest_num    = dest["component_number"]
                    shelf_no, put_level, put_col = dest_coords
                    put_nav = _shelf_nav_area(shelf_no, put_col)

                    for i in range(dest_num):
                        piece_idx += 1
                        step = f"[{piece_idx}/{total_num}]"

                        # 抓取零件
                        self.task_state_machine.update_step_label(
                            KAIAOStep.PICKING_COMPONENT,
                            f"抓取零件 {comp_type} {step}",
                        )
                        '''result = send_task_action(
                            robot, task=KAIAOTask.PICK_UP_COMPONENT, area=KAIAOArea.COMPONENT_CAR,
                            extra_params=_component_pick_extra(comp_type),
                            spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                            feedback_callback=_make_task_feedback_cb(
                                f"pick_up_component{step}", robot_id),
                        )'''
                        result = robot.send_service_request_task(
                            KAIAOService.ROBOT_TASK, task=KAIAOTask.PICK_UP_COMPONENT,
                            area=KAIAOArea.COMPONENT_CAR,
                            extra_params=_with_task_only_id(
                                _component_pick_extra(comp_type)
                            ),
                            maxtime=KAIAOTimeout.ROBOT_ACTION,
                        )
                        if not result:
                            err = f"[{robot_id}] pick_up_component{step} 失败: {result.error_msg}"
                            logger.error(_LOG, err)
                            self._fail_async_task(task_id, action_type, err)
                            return
                        logger.info(_LOG, f"[{robot_id}] pick_up_component{step} 成功")

                        # 导航到放件位（由 box_target_area[0]/[2] 推导，如 shelf0_0）
                        if not is_robot_at_pose(
                            robot, get_nav_pose(put_nav),
                            KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                        ):
                            self.task_state_machine.update_step_label(
                                KAIAOStep.NAVIGATING_TARGET,
                                f"导航到放件位 {put_nav} {step}",
                            )
                            if not self._navigate(robot, put_nav, put_nav, robot_id, task_id):
                                if self._fail_if_nav(
                                    False, task_id, action_type,
                                    f"[{robot_id}] 导航到放件位 {put_nav} 失败",
                                ):
                                    return

                        # 放置零件：area=放件导航点；shelf_level=box_target_area[1]（row）
                        self.task_state_machine.update_step_label(
                            KAIAOStep.PUTTING_COMPONENT,
                            f"放置零件至 {put_nav} level={put_level} {step}",
                        )
                        '''result = send_task_action(
                            robot, task=KAIAOTask.PUT_DOWN_COMPONENT, area=put_nav,
                            extra_params=_component_put_extra(put_level, comp_type),
                            spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                            feedback_callback=_make_task_feedback_cb(
                                f"put_down_component{step}", robot_id),
                        )'''
                        result = robot.send_service_request_task(
                            KAIAOService.ROBOT_TASK, task=KAIAOTask.PUT_DOWN_COMPONENT,
                            area=put_nav,
                            extra_params=_with_task_only_id(
                                _component_put_extra(put_level, comp_type)
                            ),
                            maxtime=KAIAOTimeout.ROBOT_ACTION,
                        )
                        if not result:
                            err = f"[{robot_id}] put_down_component{step} 失败: {result.error_msg}"
                            logger.error(_LOG, err)
                            self._fail_async_task(task_id, action_type, err)
                            return
                        logger.info(_LOG, f"[{robot_id}] put_down_component{step} 成功")

                        if piece_idx < total_num:
                            if not is_robot_at_pose(
                                robot, get_nav_pose(pick_nav),
                                KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                            ):
                                self.task_state_machine.update_step_label(
                                    KAIAOStep.NAVIGATING,
                                    f"返回取件位 {pick_nav} {step}",
                                )
                                if not self._navigate(robot, pick_nav, pick_nav, robot_id, task_id):
                                    if self._fail_if_nav(
                                        False, task_id, action_type,
                                        f"[{robot_id}] 返回取件位 {pick_nav} 失败",
                                    ):
                                        return
                        else:
                            # 导航到回家位
                            if not is_robot_at_pose(
                                robot, get_nav_pose("home"),
                                KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                            ):
                                self.task_state_machine.update_step_label(
                                    KAIAOStep.NAVIGATING, f"导航到回家位 home"
                                )
                                '''if not self._navigate(robot, "home", "home", robot_id, task_id):
                                    return'''

                self.task_state_machine.complete_task(True, summary)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"零件搬运完成: {summary}",
                )
                logger.info(_LOG, f"[{robot_id}] PICK_COMPONENT_TO_SP 完成: {summary}")

            except Exception as exc:
                err = f"[{robot_id}] PICK_COMPONENT_TO_SP 异常: {exc}"
                logger.exception_occurred(_LOG, "PICK_COMPONENT_TO_SP", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=False, message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"kaiao-pick-comp-{robot_id}"
        ).start()

        return _make_action_response(True, action_type, message=f"动作 {action_type} 已受理，后台执行中")

    def handle_pick_up_box(self, cmd_data: Dict) -> Dict:
        """PICK_UP_BOX —— 导航到 area 后抓箱（HTTP 参数同 WAIC，ROS 带 shelf_level）。"""
        action_type = "PICK_UP_BOX"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type, message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )
        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        robot_area, extra = self._single_step_box_extras(params, for_pick=True)
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                self.task_state_machine.update_step_label(
                    KAIAOStep.PICKING_UP, f"抓取箱子（{area}）"
                )
                result = send_task_action(
                    robot, task=KAIAOTask.PICK_UP_BOX, area=robot_area,
                    extra_params=_with_task_only_id(extra),
                    spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                    feedback_callback=_make_task_feedback_cb("pick_up_box", robot_id),
                )
                # result = robot.send_service_request_task(
                #     KAIAOService.ROBOT_TASK, task=KAIAOTask.PICK_UP_BOX,
                #     area=robot_area, extra_params=extra, maxtime=KAIAOTimeout.ROBOT_ACTION,
                # )
                if not result:
                    err = f"[{robot_id}] pick_up_box 失败: {result.error_msg}"
                    logger.error(_LOG, err)
                    self._fail_async_task(task_id, action_type, err)
                    return
                logger.info(_LOG, f"[{robot_id}] pick_up_box 成功")
                self._set_holding_box(robot_id, True)
                self.task_state_machine.complete_task(True, f"pick_up_box @ {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"抓取完成: {area}",
                )
            except Exception as exc:
                err = f"[{robot_id}] PICK_UP_BOX 异常: {exc}"
                logger.exception_occurred(_LOG, "PICK_UP_BOX", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=False, message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"kaiao-pick-up-{robot_id}"
        ).start()
        return _make_action_response(True, action_type, message=f"动作 {action_type} 已受理，后台执行中")

    def handle_put_down_box(self, cmd_data: Dict) -> Dict:
        """PUT_DOWN_BOX —— 导航到 area 后放箱。"""
        action_type = "PUT_DOWN_BOX"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type, message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )
        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        robot_area, extra = self._single_step_box_extras(params, for_pick=False)
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                self.task_state_machine.update_step_label(
                    KAIAOStep.PUTTING_DOWN, f"放置箱子（{area}）"
                )
                result = send_task_action(
                    robot, task=KAIAOTask.PUT_DOWN_BOX, area=robot_area,
                    extra_params=_with_task_only_id(extra),
                    spec=KAIAO_TASK_ACTION_SPEC, timeout=KAIAOTimeout.ROBOT_ACTION,
                    feedback_callback=_make_task_feedback_cb("put_down_box", robot_id),
                )
                # result = robot.send_service_request_task(
                #     KAIAOService.ROBOT_TASK, task=KAIAOTask.PUT_DOWN_BOX,
                #     area=robot_area, extra_params=extra, maxtime=KAIAOTimeout.ROBOT_ACTION,
                # )
                if not result:
                    err = f"[{robot_id}] put_down_box 失败: {result.error_msg}"
                    logger.error(_LOG, err)
                    self._fail_async_task(task_id, action_type, err)
                    return
                logger.info(_LOG, f"[{robot_id}] put_down_box 成功")
                self._set_holding_box(robot_id, False)
                self.task_state_machine.complete_task(True, f"put_down_box @ {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"放置完成: {area}",
                )
            except Exception as exc:
                err = f"[{robot_id}] PUT_DOWN_BOX 异常: {exc}"
                logger.exception_occurred(_LOG, "PUT_DOWN_BOX", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=False, message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"kaiao-put-down-{robot_id}"
        ).start()
        return _make_action_response(True, action_type, message=f"动作 {action_type} 已受理，后台执行中")

    def handle_navigation(self, cmd_data: Dict) -> Dict:
        """NAVIGATION —— 导航到 area。"""
        action_type = "NAVIGATION"
        params      = cmd_data.get("params", {}) or {}
        cmd_id      = cmd_data.get("cmd_id", "")
        area        = params.get("area", "")

        if not area:
            return _make_action_response(
                False, action_type, message="缺少必要参数: area",
                code=ErrorCode.INVALID_PARAMS,
            )

        robot_id, robot, err_resp = self._get_robot(cmd_data)
        if err_resp:
            return _make_action_response(
                False, action_type,
                message=err_resp.get("message", "机器人不存在"),
                code=ErrorCode.ROBOT_NOT_FOUND,
            )
        if self.task_state_machine.is_busy():
            return self._busy_response(action_type, robot_id)

        task_id = cmd_id or str(uuid.uuid4())
        self.task_state_machine.start_task(task_id, robot_id)

        def _run():
            try:
                if not is_robot_at_pose(
                    robot, get_nav_pose(area),
                    KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
                ):
                    self.task_state_machine.update_step_label(
                        KAIAOStep.NAVIGATING, f"导航到 {area}"
                    )
                    if not self._navigate(robot, area, area, robot_id, task_id):
                        if self._fail_if_nav(
                            False, task_id, action_type,
                            f"[{robot_id}] 导航到 {area} 失败",
                        ):
                            return
                self.task_state_machine.complete_task(True, f"导航到 {area}")
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=True,
                    message=f"导航完成: {area}",
                )
            except Exception as exc:
                err = f"[{robot_id}] NAVIGATION 异常: {exc}"
                logger.exception_occurred(_LOG, "NAVIGATION", exc)
                self.task_state_machine.set_error(err)
                self.callback_sender.send(
                    cmd_id=task_id, cmd_type=action_type, success=False, message=err,
                )

        threading.Thread(
            target=_run, daemon=True, name=f"kaiao-nav-{robot_id}"
        ).start()
        return _make_action_response(True, action_type, message=f"动作 {action_type} 已受理，后台执行中")
