"""
KAIAO_FLOW 的节点处理器。

设计要点：**这里不重新实现任何 KAIAO 的业务逻辑**，所有节点都是薄薄一层，
把流程图上的节点参数翻译成对现有 ``programs.KAIAO.KAIAO.KAIAOHandler`` 方法的调用。

这么做的原因是 KAIAO 的导航实现（``KAIAOHandler._navigate``，KAIAO.py:769 起 330 余行）
包含走廊中间点生成、Planning Failed 抖动重试、feedback 取消协议等一整套现场调出来的
细节。照抄一份到这里，等于把现场踩过的坑重新踩一遍，而且今后两边还会各自漂移。
代价是本模块依赖 programs/KAIAO/（不像 WRC_FLOW 那样完全独立），这是刻意的取舍。

关于"中间点算法怎么编排"：
    它**不出现在流程图上**。图上只画一个"导航到 X"的节点，是否插中间点、插几个、
    分几次 Action 下发，全部由 KAIAOHandler._navigate 在运行时根据机器人此刻的
    实时位姿自行决定。原因是这些分支的判断依据（当前朝向属于走廊端点还是货架、
    手上有没有箱子）在画图时根本无从得知，画成显式分支既画不对也没人维护得了。
    需要精细控制时用节点上的两个参数兜底：
        mode='skip'      —— 完全不生成中间点，直达目标
        mid_send='batch' —— 中间点与目标一次 Action 发完，不分步
"""

from typing import Any, Callable, Dict, Optional, Tuple

from infrastructure.error_logger import get_error_logger
from core.flow_engine import FlowNode, FlowContext

from programs.KAIAO.KAIAO import (
    KAIAOHandler,
    _make_task_feedback_cb,
    _parse_box_location,
    _same_level_movement,
    _shelf_nav_area,
    _with_task_only_id,
)
from programs.KAIAO.constants import (
    KAIAOArea, KAIAONavTolerance, KAIAOService, KAIAOTask, KAIAOTimeout,
    KAIAO_TASK_ACTION_SPEC, get_component_weight_kg, get_nav_pose,
)
from hardware.navigation_utils import is_robot_at_pose
from hardware.task_utils import send_task_action

logger = get_error_logger()
_LOG = "KAIAO_FLOW"


# ──────────────────────────────────────────────────────────────────────────────
# 参数取值辅助
# ──────────────────────────────────────────────────────────────────────────────

def _param(node: FlowNode, ctx: FlowContext, name: str, default: Any = None) -> Any:
    """
    取节点参数并做模板渲染。

    FlowContext.render 对"整串恰好是一个 {{var}}"的情况会保留原始类型，
    所以 ``{{src_coords}}`` 能直接拿到列表本身，而不是它的字符串形式。
    """
    raw = node.params.get(name)
    if raw is None or raw == "":
        return default
    value = ctx.render(raw)
    return default if value is None or value == "" else value


def _robot_id(node: FlowNode, ctx: FlowContext) -> str:
    """机器人 id：节点上写了就用节点的，否则用流程变量（命令入口注入）。"""
    return _param(node, ctx, "robot_id") or ctx.get("robot_id") or "robot_a"


def _int_param(node: FlowNode, ctx: FlowContext, name: str, default: int = 0) -> int:
    try:
        return int(_param(node, ctx, name, default))
    except (TypeError, ValueError):
        return default


def build_handler_registry(kaiao: KAIAOHandler) -> Dict[str, Callable[[FlowNode, FlowContext], bool]]:
    """
    组装 KAIAO_FLOW 的节点处理器注册表。

    参数:
        kaiao: 一个现成的 KAIAOHandler 实例，所有底层能力（导航、抓放箱、
               货架重量追踪、持箱状态）都从它身上复用。
    """

    def _get_robot(robot_id: str):
        return kaiao.robots.get(robot_id)

    # ── 导航 ─────────────────────────────────────────────────────────────────

    def handle_kaiao_navigate(node: FlowNode, ctx: FlowContext) -> bool:
        """
        导航到点位。走廊中间点由 KAIAOHandler._navigate 内部处理，图上不可见。

        ``on_leave_shelf='adjust_pose'`` 时，会把姿态校正作为**回调挂进这次导航**，
        由 _navigate 在离开货架的第一个中间点到达后触发，而不是导航前单独走一段。
        这一点必须和真机保持一致，原因见下面 _make_after_leave_shelf 的说明。
        """
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None:
            logger.error(_LOG, f"kaiao_navigate 节点 {node.id}: 机器人 '{robot_id}' 不存在")
            return False
        if not robot.is_connected():
            logger.error(
                _LOG,
                f"kaiao_navigate 节点 {node.id}: 机器人 '{robot_id}' 未连接。"
                f"演练请先启动 mock_rosbridge（127.0.0.1:9090）",
            )
            return False

        area = _param(node, ctx, "area")
        if not area:
            logger.error(_LOG, f"kaiao_navigate 节点 {node.id}: 未指定目标点位")
            return False

        pose = get_nav_pose(area)
        if pose is None:
            logger.error(
                _LOG,
                f"kaiao_navigate 节点 {node.id}: 未找到点位 '{area}'，"
                f"请在 robot_config.json 的 navigation_poses.KAIAO 中配置",
            )
            return False

        after_leave_shelf = _make_after_leave_shelf(node, ctx, robot, robot_id)

        dryrun = bool(ctx.extra.get("dryrun"))

        # 已经在目标点位就不用再跑一趟（与 KAIAO.py 各任务里的前置判断一致）。
        # 但挂了离架校正时绝不能跳过：校正是挂在这次导航里发的，跳过导航等于
        # 跳过校正，会出现"没校正就直接放箱"。真机同样为此去掉了放箱位的前置判断。
        # 演练 mock 不发里程计，skip_if_at_pose 永远不命中，空等 5s 没意义。
        if after_leave_shelf is not None:
            if node.params.get("skip_if_at_pose"):
                logger.info(
                    _LOG,
                    f"[{robot_id}] 节点 {node.id} 同时勾了'已在点位则跳过'和'离架后校正'，"
                    f"以校正为准不跳过导航",
                )
        elif (
            node.params.get("skip_if_at_pose", True)
            and not dryrun
            and is_robot_at_pose(
                robot, pose, KAIAONavTolerance.DISTANCE, KAIAONavTolerance.HEADING,
            )
        ):
            logger.info(_LOG, f"[{robot_id}] 已在 {area}，跳过导航")
            ctx.set("last_nav_area", area)
            return True

        nav_kwargs = {}
        if dryrun:
            # mock 导航约 4s；未回结果时 12s 内失败，不要拖到演练总上限。
            nav_kwargs["timeout"] = 12.0
            nav_kwargs["retry_on_disconnect"] = False
            nav_kwargs["odom_timeout"] = 0.4

        ok = kaiao._navigate(
            robot, area, area, robot_id,
            task_id=ctx.get("task_id", ""),
            mode=_param(node, ctx, "mode", "auto"),
            mid_send=_param(node, ctx, "mid_send", "split"),
            after_leave_shelf=after_leave_shelf,
            **nav_kwargs,
        )
        ctx.set("last_nav_area", area)
        if not ok:
            logger.error(_LOG, f"[{robot_id}] 导航到 {area} 失败")
        return ok

    def _make_after_leave_shelf(node: FlowNode, ctx: FlowContext, robot, robot_id: str):
        """
        按节点参数构造"离架后校正"回调，不需要校正时返回 None。

        为什么校正必须挂进导航、而不是在导航前单独走一段
        ----------------------------------------------------
        走廊场景的判定（KAIAO.py::_prepend_intermediate_waypoint）完全依赖**调用那一刻**
        的实时里程计。如果先退离货架再进导航，导航读到的是"已经退到走廊里"的位姿：

          - 同侧货架且手上有箱时会落进 side_to_side_same 分支，该分支用
            ``retreat_x = src_x - cos(yaw) * retreat`` 再插一个后退中间点，
            于是机器人**一共后退两次**（0.3m + 0.3m）；
          - 其余场景的中间点坐标也会整体偏移一个后退距离。

        挂进导航后，中间点算法看到的始终是"还贴着货架"的原始位姿，校正则发生在
        第一个离架中间点到达之后，与真机 KAIAO.py::handle_pick_box_to_sp 完全一致。
        """
        if _param(node, ctx, "on_leave_shelf", "none") != "adjust_pose":
            return None

        adjust_area = _param(node, ctx, "adjust_area") or _param(node, ctx, "area")
        adjust_level = _int_param(node, ctx, "adjust_shelf_level", 0)

        def _after_leave() -> bool:
            return bool(kaiao._send_adjust_pose(
                robot, adjust_area, adjust_level, robot_id,
                ctx.get("task_id", ""), ctx.get("action_type", "KAIAO_FLOW"),
            ))

        return _after_leave

    # ── 发送操作动作（通用节点 send_operation 的 KAIAO 实现）──────────────────

    #: 抓箱类任务，成功后要把箱子从货架"取走"并置为持箱
    _PICK_TASKS = frozenset({KAIAOTask.PICK_UP_BOX, KAIAOTask.PICK_UP_HEAVY_BOX})
    #: 放箱类任务，成功后要把箱子放到货架格并清除持箱
    _PUT_TASKS = frozenset({KAIAOTask.PUT_DOWN_BOX, KAIAOTask.PUT_DOWN_HEAVY_BOX})

    def handle_send_operation(node: FlowNode, ctx: FlowContext) -> bool:
        """
        发送机器人任务（Action 或 Service）。

        参数表是跨项目通用的（见 core/common_nodes.py），这里是 KAIAO 的实现，
        除了下发指令还多做两件 KAIAO 特有的事：

        1. ``task='auto_pick'`` —— 按货架当前累计重量自动在 pick_up_box /
           pick_up_heavy_box 之间选择；``task='auto_put'`` 则按本次抓取配对
           put_down_box / put_down_heavy_box（抓了重箱就必须用 put_down_heavy_box）。
        2. 抓/放箱成功后维护"货架格累计重量"和"手上有没有箱子"两份状态，
           与 KAIAO.py::handle_pick_box_to_sp 的行为保持一致。

        第 2 点要用货架坐标定位格子，但它不是发给 ROS 的参数，通用参数表里没有
        它的位置，所以按任务类型从流程变量里取：抓箱用 ``src_coords``、
        放箱用 ``dst_coords``（都由命令入口 KAIAO_FLOW.py 解析入参时注入）。
        和走廊中间点一样，属于处理器内部自动维护的业务副作用，图上不体现。
        """
        robot_id = _robot_id(node, ctx)
        robot = _get_robot(robot_id)
        if robot is None:
            logger.error(_LOG, f"send_operation 节点 {node.id}: 机器人 '{robot_id}' 不存在")
            return False
        if not robot.is_connected():
            logger.error(
                _LOG,
                f"send_operation 节点 {node.id}: 机器人 '{robot_id}' 未连接。"
                f"演练请先启动 mock_rosbridge（127.0.0.1:9090）",
            )
            return False

        task = _param(node, ctx, "task")
        if not task:
            logger.error(_LOG, f"send_operation 节点 {node.id}: 未指定任务名(task)")
            return False
        area = _param(node, ctx, "area", "") or ""
        extra = dict(_param(node, ctx, "extra_params", {}) or {})
        timeout = _int_param(node, ctx, "timeout", KAIAOTimeout.ROBOT_ACTION)
        if ctx.extra.get("dryrun"):
            timeout = min(timeout, 15)

        is_auto_put = task == "auto_put"
        is_put = (
            is_auto_put
            or task in _PUT_TASKS
            or task == KAIAOTask.PUT_DOWN_COMPONENT
        )
        coords = ctx.get("dst_coords" if is_put else "src_coords") or [0, 0, 0]

        box_weight = 0.0
        if task == "auto_pick":
            task, box_weight = kaiao._pick_box_task(area, coords)
            logger.info(_LOG, f"[{robot_id}] auto_pick 按货架重量选定 {task}")

        if is_auto_put:
            pick_task = ctx.get("pick_task") or ""
            task = kaiao._put_box_task(pick_task, robot_id)
            logger.info(
                _LOG,
                f"[{robot_id}] auto_put 按抓取 {pick_task or '(未记录)'} 选定 {task}",
            )

        if task == KAIAOTask.PICK_UP_HEAVY_BOX and area != KAIAOArea.SHELF:
            logger.error(
                _LOG,
                f"[{robot_id}] pick_up_heavy_box 仅允许 area=shelf，当前 area={area}",
            )
            return False

        if node.params.get("call_type", "action") == "service":
            result = robot.send_service_request_task(
                KAIAOService.ROBOT_TASK, task=task, area=area,
                extra_params=_with_task_only_id(extra), maxtime=timeout,
            )
        else:
            result = send_task_action(
                robot, task=task, area=area,
                extra_params=_with_task_only_id(extra),
                spec=KAIAO_TASK_ACTION_SPEC, timeout=timeout,
                feedback_callback=_make_task_feedback_cb(task, robot_id),
            )

        ctx.set("last_op_task", task)
        ctx.set("last_op_success", bool(result))
        if not result:
            logger.error(_LOG, f"[{robot_id}] {task} 失败: {getattr(result, 'error_msg', '')}")
            return False

        # 业务状态维护：与 KAIAO.py 里各 handle_* 的处理保持一致
        if task in _PUT_TASKS:
            held = kaiao._holding_box_weight.get(robot_id, 0.0)
            if area == KAIAOArea.SHELF:
                kaiao._put_shelf_weight(coords, held)
            kaiao._set_holding_box(robot_id, False)
        elif task in _PICK_TASKS:
            ctx.set("pick_task", task)
            if area == KAIAOArea.SHELF:
                kaiao._take_shelf_weight(coords)
            kaiao._set_holding_box(robot_id, True, box_weight)
        elif task == KAIAOTask.PUT_DOWN_COMPONENT:
            dest = ctx.get("dst_coords") or coords
            unit_kg = ctx.get("unit_kg")
            if unit_kg is None:
                unit_kg = get_component_weight_kg(
                    ctx.get("comp_type") or extra.get("type") or ""
                )
            kaiao._add_shelf_weight(dest, float(unit_kg or 0))

        logger.info(_LOG, f"[{robot_id}] {task} 成功 area={area} coords={coords}")
        return True

    # ── 把命令里的取放位置解析成导航变量 ────────────────────────────────────

    def handle_kaiao_bind_locations(node: FlowNode, ctx: FlowContext) -> bool:
        """
        把 wait_for_command 注入的 box_initial_area / box_target_area
        解析成后续导航、抓放箱节点用的变量（pick_nav、put_area 等）。

        字段映射画在图上：改 HTTP 入参名或输出变量名只需改这个节点，不用改 Python。
        """
        src_field = _param(node, ctx, "source_pick")
        dst_field = _param(node, ctx, "source_put")
        # source_* 既可以是已经渲染好的对象，也可以是上下文里的变量名
        if isinstance(src_field, str):
            src_field = ctx.get(src_field, src_field)
        if isinstance(dst_field, str):
            dst_field = ctx.get(dst_field, dst_field)

        src, src_err = _parse_box_location(src_field, "source_pick")
        if src_err:
            logger.error(_LOG, f"kaiao_bind_locations 节点 {node.id}: {src_err}")
            ctx.set("bind_error", src_err)
            return False
        dst, dst_err = _parse_box_location(dst_field, "source_put")
        if dst_err:
            logger.error(_LOG, f"kaiao_bind_locations 节点 {node.id}: {dst_err}")
            ctx.set("bind_error", dst_err)
            return False

        pick_level = int(src["extra"].get("shelf_level", 0))
        put_level = int(dst["extra"].get("shelf_level", 0))
        mapping = {
            _param(node, ctx, "pick_nav_var", "pick_nav") or "pick_nav": src["nav_area"],
            _param(node, ctx, "put_nav_var", "put_nav") or "put_nav": dst["nav_area"],
            _param(node, ctx, "pick_area_var", "pick_area") or "pick_area": src["shelf_type"],
            _param(node, ctx, "put_area_var", "put_area") or "put_area": dst["shelf_type"],
            _param(node, ctx, "src_coords_var", "src_coords") or "src_coords": list(src["coords"]),
            _param(node, ctx, "dst_coords_var", "dst_coords") or "dst_coords": list(dst["coords"]),
            _param(node, ctx, "pick_level_var", "pick_shelf_level") or "pick_shelf_level": pick_level,
            _param(node, ctx, "put_level_var", "put_shelf_level") or "put_shelf_level": put_level,
            _param(node, ctx, "same_level_var", "same_level_movement") or "same_level_movement":
                _same_level_movement(src, dst),
        }
        for name, value in mapping.items():
            ctx.set(name, value)
        logger.info(
            _LOG,
            f"绑定取放位置: {src['shelf_type']}{src['coords']}@{src['nav_area']} → "
            f"{dst['shelf_type']}{dst['coords']}@{dst['nav_area']}",
        )
        return True

    # ── 分拣零件：命令 jobs → 逐件队列 ────────────────────────────────────

    def handle_kaiao_bind_component_jobs(node: FlowNode, ctx: FlowContext) -> bool:
        """
        把 PICK_COMPONENT_TO_SP 的 params（对象或对象列表）展开成「一件一格」队列。

        原生 KAIAO 是三重循环（job × target × component_number）。图上不画三层环，
        由本节点一次性摊平，后面用「取下一件 + 条件成环」逐件抓放。
        字段映射画在图上：改命令字段名或输出队列名改这个节点即可。
        """
        source = _param(node, ctx, "source_jobs")
        if isinstance(source, str):
            source = ctx.get(source, source)
        if source is None:
            source = ctx.get("jobs")
            if source is None:
                source = ctx.get("cmd_payload")

        jobs, err = kaiao._parse_component_jobs(source)
        if err:
            logger.error(_LOG, f"kaiao_bind_component_jobs 节点 {node.id}: {err}")
            ctx.set("bind_error", err)
            return False

        queue: list = []
        for job in jobs:
            unit_kg = get_component_weight_kg(job["comp_type"])
            for dest in job["targets"]:
                coords = list(dest["coords"])
                shelf_no, put_level, put_col = coords
                put_nav = _shelf_nav_area(shelf_no, put_col)
                for _ in range(int(dest["component_number"])):
                    queue.append({
                        "pick_nav": job["pick_nav"],
                        "box_num": job["box_num"],
                        "comp_type": job["comp_type"],
                        "put_nav": put_nav,
                        "put_shelf_level": put_level,
                        "dst_coords": coords,
                        "unit_kg": unit_kg,
                    })

        queue_var = _param(node, ctx, "queue_var", "component_queue") or "component_queue"
        index_var = _param(node, ctx, "index_var", "piece_index") or "piece_index"
        total_var = _param(node, ctx, "total_var", "piece_total") or "piece_total"
        ctx.set(queue_var, queue)
        ctx.set(index_var, 0)
        ctx.set(total_var, len(queue))
        ctx.set("has_piece", False)
        logger.info(
            _LOG,
            f"绑定分拣任务: {len(jobs)} 个箱子 → {len(queue)} 件 "
            + ", ".join(
                f"{j['comp_type']}x{sum(t['component_number'] for t in j['targets'])}"
                f"(box{j['box_num']})"
                for j in jobs
            ),
        )
        return True

    def handle_kaiao_next_component(node: FlowNode, ctx: FlowContext) -> bool:
        """
        从分拣队列取出下一件，写入 pick_nav / put_nav / box_num 等变量。

        队列取尽时 ``has_piece=false``，仍返回成功——图上用条件节点分流到完成出口，
        不要把「没有下一件」当成任务失败。
        """
        queue_var = _param(node, ctx, "queue_var", "component_queue") or "component_queue"
        index_var = _param(node, ctx, "index_var", "piece_index") or "piece_index"
        flag_var = _param(node, ctx, "has_piece_var", "has_piece") or "has_piece"
        queue = ctx.get(queue_var) or []
        try:
            idx = int(ctx.get(index_var) or 0)
        except (TypeError, ValueError):
            idx = 0
        if not isinstance(queue, list) or idx >= len(queue):
            ctx.set(flag_var, False)
            logger.info(_LOG, "分拣队列已空")
            return True

        item = queue[idx]
        ctx.set(index_var, idx + 1)
        ctx.set(flag_var, True)
        ctx.set("pick_nav", item.get("pick_nav"))
        ctx.set("box_num", item.get("box_num"))
        ctx.set("comp_type", item.get("comp_type"))
        ctx.set("put_nav", item.get("put_nav"))
        ctx.set("put_shelf_level", item.get("put_shelf_level"))
        ctx.set("dst_coords", item.get("dst_coords"))
        ctx.set("unit_kg", item.get("unit_kg"))
        ctx.set("piece_idx", idx + 1)
        ctx.set("piece_total", len(queue))
        logger.info(
            _LOG,
            f"下一件 [{idx + 1}/{len(queue)}] {item.get('comp_type')} "
            f"box={item.get('box_num')} {item.get('pick_nav')} → "
            f"{item.get('put_nav')} level={item.get('put_shelf_level')}",
        )
        return True

    # ── 状态步骤记录 ─────────────────────────────────────────────────────────

    def handle_update_step(node: FlowNode, ctx: FlowContext) -> bool:
        """把当前步骤写进任务状态机，供 GET_TASK_STATE 查询回显。"""
        step = _param(node, ctx, "step", "")
        message = _param(node, ctx, "message", "") or ""
        try:
            kaiao.task_state_machine.update_step_label(step, message)
        except Exception as e:  # noqa: BLE001 —— 状态记录失败不该拖垮业务流程
            logger.warning(_LOG, f"update_step 节点 {node.id} 写入失败: {e}")
        return True

    return {
        "kaiao_navigate": handle_kaiao_navigate,   # 项目专有：走廊中间点 + 离架校正
        "kaiao_bind_locations": handle_kaiao_bind_locations,  # 命令入参 → 导航变量
        "kaiao_bind_component_jobs": handle_kaiao_bind_component_jobs,  # 分拣 params → 逐件队列
        "kaiao_next_component": handle_kaiao_next_component,  # 取出下一件零件
        "send_operation": handle_send_operation,   # 通用节点的 KAIAO 实现
        "update_step": handle_update_step,         # 通用节点的 KAIAO 实现
    }


# ──────────────────────────────────────────────────────────────────────────────
# 前端节点面板用的 schema
# ──────────────────────────────────────────────────────────────────────────────
#
# options_source="poses"：选项由 flow_api_server 按 robot_config.json 里当前的
# navigation_poses 实时注入，在编辑器里新增点位后刷新即可选到。
# extra_options：额外附加的固定选项，用于让操作人员能直接选到流程变量模板
# （KAIAO 的点位是命令参数算出来的，画图时并不知道具体是 shelf0_1 还是 agv_car0_0）。

#: KAIAO 的任务步骤枚举（KAIAOStep），由 flow_api_server 注入到通用节点
#: update_step 的下拉框里，见 core/common_nodes.py。
STEP_OPTIONS = [
    "IDLE", "NAVIGATING", "PICKING_UP", "PICKING_COMPONENT",
    "ADJUSTING_POSE", "NAVIGATING_TARGET", "PUTTING_DOWN",
    "PUTTING_COMPONENT", "DONE",
]

#: KAIAO 只有一个 service 通道（/robot_task），不像 WRC 还要区分吉利专用通道
SERVICE_OPTIONS = ["robot_task"]

#: 本项目实现了哪些通用节点。没列进来的（比如通用 navigate）不会出现在面板上——
#: KAIAO 用的是专有的 kaiao_navigate。
COMMON_NODES = ["send_operation", "update_step"]

NODE_TYPE_SCHEMAS: Dict[str, Dict] = {
    "kaiao_navigate": {
        "label": "导航到点位",
        "category": "机器人动作",
        "fields": [
            {"name": "area", "type": "select", "label": "目标点位", "required": True,
             "options_source": "poses",
             "extra_options": ["{{pick_nav}}", "{{put_nav}}", "component_car", "home"],
             "hint": "可直接选点位名，也可选 {{pick_nav}}/{{put_nav}} 用命令参数算出的点位"},
            {"name": "robot_id", "type": "select", "label": "机器人",
             "options_source": "robots",
             "extra_options": ["{{robot_id}}"],
             "hint": "指定 robot_a / robot_b，或选 {{robot_id}} 跟随命令入参；留空也走命令里的 robot_id"},
            {"name": "skip_if_at_pose", "type": "checkbox",
             "label": "已在该点位时跳过导航", "default": True,
             "hint": "勾了'离架后校正'时本项自动失效，否则会跳过导航连带跳过校正"},
            {"name": "mode", "type": "select", "label": "走廊中间点",
             "options": ["auto", "skip"], "default": "auto",
             "hint": "auto=按当前位姿自动插入中间点；skip=不插中间点直达目标"},
            {"name": "mid_send", "type": "select", "label": "中间点下发方式",
             "options": ["split", "batch"], "default": "split",
             "hint": "split=中间点与目标分多次 Action；batch=一次发完"},
            {"name": "on_leave_shelf", "type": "select", "label": "离架后校正姿态",
             "options": ["none", "adjust_pose"], "default": "none",
             "hint": "抓箱后去放箱位这一段选 adjust_pose：离开货架的第一个中间点到达后"
                     "自动发 adjust_pose（同层也发；拿不到位姿时直达并延迟发）"},
            {"name": "adjust_area", "type": "select", "label": "校正区域",
             "options": ["shelf", "agv_car"],
             "extra_options": ["{{put_area}}"],
             "hint": "校正用的 area，通常填放箱位的 {{put_area}}；留空则沿用目标点位"},
            {"name": "adjust_shelf_level", "type": "text", "label": "校正垂直层",
             "default": "{{put_shelf_level}}",
             "hint": "校正到哪一层，通常填放箱位的 {{put_shelf_level}}"},
        ],
        "outputs": ["success", "failure"],
    },
    "kaiao_bind_locations": {
        "label": "解析取放位置",
        "category": "参数绑定",
        "fields": [
            {"name": "source_pick", "type": "text", "label": "取箱位置（命令字段）",
             "default": "{{box_initial_area}}", "required": True,
             "hint": "等待 PICK_BOX_TO_SP 后注入的对象；改命令字段名时改这里即可"},
            {"name": "source_put", "type": "text", "label": "放箱位置（命令字段）",
             "default": "{{box_target_area}}", "required": True,
             "hint": "同上，对应 box_target_area"},
            {"name": "pick_nav_var", "type": "text", "label": "写入：取箱导航点",
             "default": "pick_nav"},
            {"name": "put_nav_var", "type": "text", "label": "写入：放箱导航点",
             "default": "put_nav"},
            {"name": "pick_area_var", "type": "text", "label": "写入：取箱 area",
             "default": "pick_area"},
            {"name": "put_area_var", "type": "text", "label": "写入：放箱 area",
             "default": "put_area"},
            {"name": "src_coords_var", "type": "text", "label": "写入：取箱坐标",
             "default": "src_coords"},
            {"name": "dst_coords_var", "type": "text", "label": "写入：放箱坐标",
             "default": "dst_coords"},
            {"name": "pick_level_var", "type": "text", "label": "写入：取箱层",
             "default": "pick_shelf_level"},
            {"name": "put_level_var", "type": "text", "label": "写入：放箱层",
             "default": "put_shelf_level"},
            {"name": "same_level_var", "type": "text", "label": "写入：同层搬箱标记",
             "default": "same_level_movement"},
        ],
        "outputs": ["success", "failure"],
    },
    "kaiao_bind_component_jobs": {
        "label": "解析分拣任务",
        "category": "参数绑定",
        "fields": [
            {"name": "source_jobs", "type": "text", "label": "分拣任务（命令字段）",
             "default": "{{jobs}}", "required": True,
             "hint": "PICK_COMPONENT_TO_SP 的 params：对象或对象列表；入口会写成 jobs"},
            {"name": "queue_var", "type": "text", "label": "写入：逐件队列",
             "default": "component_queue"},
            {"name": "index_var", "type": "text", "label": "写入：当前下标",
             "default": "piece_index"},
            {"name": "total_var", "type": "text", "label": "写入：总件数",
             "default": "piece_total"},
        ],
        "outputs": ["success", "failure"],
    },
    "kaiao_next_component": {
        "label": "取下一件零件",
        "category": "参数绑定",
        "fields": [
            {"name": "queue_var", "type": "text", "label": "读取：逐件队列",
             "default": "component_queue"},
            {"name": "index_var", "type": "text", "label": "读取/写入：当前下标",
             "default": "piece_index"},
            {"name": "has_piece_var", "type": "text", "label": "写入：是否还有件",
             "default": "has_piece",
             "hint": "队列取尽为 false，仍返回成功。后面接条件节点 has_piece，不要把空队列接到失败出口"},
        ],
        "outputs": ["success", "failure"],
    },
}
# 本项目专有节点：kaiao_navigate（走廊中间点 + 离架校正）、
# kaiao_bind_locations（搬箱取放位置）、kaiao_bind_component_jobs /
# kaiao_next_component（分拣任务摊成逐件队列再循环）。
#
# send_operation 和 update_step 是跨项目通用节点，定义在 core/common_nodes.py，
# 本文件只提供它们的 KAIAO 实现（用 KAIAO 的 ActionSpec、并维护货架重量/持箱状态）。
# 下拉框选项由 flow_api_server 按 adapter 提供的数据注入：
#   步骤名 <- STEP_OPTIONS      Service 通道 <- SERVICE_OPTIONS      机器人 <- adapter
