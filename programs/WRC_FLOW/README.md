# WRC_FLOW —— 图形化流程编排引擎试点

`WRC_FLOW` 是 `programs/WRC/`（手写硬编码流程）的**独立试点副本**，用来验证
"用图形化编辑器拖拽组装/修改机器人全流程任务"这套方案。两者完全隔离：

| | `programs/WRC/` | `programs/WRC_FLOW/`（本目录） |
|---|---|---|
| 流程定义 | 写死在 `WRC.py` 的 Python 代码里 | JSON 图，存在 `flows/*.json` |
| 改流程 | 改代码 → 重新打包镜像 → 重新部署 | 在浏览器里拖拽/连线 → 点保存 |
| 执行引擎 | 无（直接跑 Python） | `core/flow_engine.py`（通用） |
| 节点"怎么做" | 写在 `WRC.py` 方法里 | `node_handlers.py` 里的处理函数 |

**改 `WRC_FLOW` 不会影响 `WRC` 一行代码，两个项目可以同时存在于代码仓库里**，
通过 `active_project` 配置项切换（生产环境同一时刻只会激活一个）。

## 目录结构

```
programs/WRC_FLOW/
├── __init__.py
├── constants.py         项目专属常量（导航点位、Service 名等），与 WRC/constants.py 互不影响
├── robot_config.json    项目内置配置（机器人地址、图形编辑器端口等）
├── node_handlers.py     业务节点类型的具体实现：navigate / send_operation / plc_action / find_slot / update_step
├── WRC_FLOW.py          命令入口（PROCESS_BEGINS / PAUSED / RESUMED / ENDED / 信号触发）
├── dryrun_adapter.py    "演练模式"适配器：连 mock 机器人、自动触发人工信号
├── flows/
│   └── wrc_flow_main.json   示例流程：复刻 WRC.py 里的并行分拣+装配循环
└── README.md            本文件
```

## 业务节点类型

`core/flow_engine.py` 完全不认识机器人/ROS，只提供通用节点
（`condition` / `set_variable` / `delay` / `parallel` / `wait_for_command` /
`sub_flow` / `noop`）。以下几种"业务节点"由本项目的 `node_handlers.py` 实现：

- **`navigate`** — 导航到某个点位。`params: {robot_id, pose, skip_if_at_pose?, timeout?}`
- **`send_operation`** — 发送一次操作动作。这是把原来"呼叫 action"
  （`send_task_action`）和"呼叫 service"（`send_service_request_task`）**合并**
  成的同一种节点，用 `call_type` 字段（`action` / `service`）区分，减少图形
  编辑器里需要认识的节点种类；`call_type=service` 时再用 `service` 字段
  （`robot_task` / `robot_task_geely`）选择走哪个 ROS service（零件抓放/装配
  走 `robot_task`，搬箱子走 `robot_task_geely`，对照
  `programs/WRC/WRC.py` 的 `_send_component_service` / `_send_box_service`）。
  `params: {robot_id, call_type, service?, task, area?, extra_params?, timeout?}`
- **`plc_action`** — PLC/传送带动作（`action`: `forward`/`reverse`/`stop`），
  对接 `programs/WRC/plc_modbus.ConveyorController`；当前 WRC_FLOW 试点没有
  接真实传送带，`build_handler_registry(..., conveyor=None)` 时该节点会跳过
  并直接返回成功，便于先跑通整张流程图、以后再接硬件。
- **`find_slot`** — 按状态在一组槽位变量里查找第一个匹配的槽位。对应
  `programs/WRC/WRC.py` 里 `SlotTracker.find_slot_by_state` 的"动态选槽位"
  逻辑：比如 `{"slots": ["P3_1","P3_2"], "want_state": "EMPTY", "output_var": "p3_n"}`
  会在 `P3_1_state`/`P3_2_state` 两个上下文变量里找第一个等于 `"EMPTY"` 的槽位，
  写入 `p3_n`；后续节点就能用 `"pose": "{{p3_n}}"`、
  `"var": "{{p3_n}}_state"` 这种模板动态操作"刚找到的那个槽位"，不需要为
  每个具体槽位单独画分支。找不到时走 `failure` 出边。
- **`update_step`** — 把当前进度写入任务状态机，供 `GET_TASK_STATE` 查询。
  `params: {robot_id, step, message?}`

新增一种业务节点类型时，只需要在 `node_handlers.py` 里加一个
`handle_xxx(node, ctx) -> bool` 函数、在 `build_handler_registry()` 里注册、
在 `NODE_TYPE_SCHEMAS` 里加一份表单 schema（`fields` 是有序数组，格式见
`core/FLOW_ENGINE_GUIDE.md`）——不需要改引擎或 API 服务器代码。

## 如何切换到 WRC_FLOW 项目

编辑 `/config/robot_config.json`（Docker 挂载）或本目录下的
`robot_config.json`，把：

```json
{ "active_project": "WRC_FLOW" }
```

`programs/TJSH/cmd_handler.py` 会在 `active_project == "WRC_FLOW"` 时自动注册
`WRCFlowHandler` 的命令；`ALL` 模式下 WRC_FLOW 不会被激活，避免和 `WRC` 的
`PROCESS_BEGINS` 等命令互相冲突（同一份命令名同时属于两个项目在生产环境没有意义）。

## 命令接口（与 WRC 项目的 HTTP 命令协议完全兼容）

| cmd_type | 说明 |
|---|---|
| `PROCESS_BEGINS` | 流程总开关：打开当前已激活的图。展会必须先发这条（`flow_control.require_process_begins` 默认 true）。图已在跑且正等待该命令时只唤醒，不开第二份 |
| `PROCESS_PAUSED` | 当前节点执行完后暂停 |
| `PROCESS_RESUMED` | 恢复暂停的流程 |
| `PROCESS_ENDED` | 结束流程 |
| `MANUAL_RESET_COMPLETED` | 触发流程图里等待该信号的 `wait_for_command` 节点 |
| `WRC_FLOW_SIGNAL`（`params.event_name`） | 通用信号入口，触发任意名字的信号 |

## 图形化编辑器 / 演练模式

流程图的可视化编辑、保存、校验、"演练"(dry-run) 由独立的
`network/flow_api_server.py` + `flow_editor/` 提供，运行在独立端口
（见 `robot_config.json` 的 `flow_api_server.port`，默认 `8099`），
与业务命令端口（`http_server.port`）互不干扰。启动方式：

```bash
python -m network.flow_api_server
```

演练模式使用 `dryrun_adapter.py` 里配置的 mock 机器人地址（对应
`mock_rosbridge/mock_rosbridge_server.py` 默认端口 9090/9091），会自动：
1. 检查所需端口是否可达，不可达就自动用子进程拉起
   `mock_rosbridge/mock_rosbridge_server.py`（最多等 6 秒就绪）；仍不可达的
   机器人直接跳过 `connect()`（不去调用它），避免触发
   `RobotController` 内部"最长 N 次重试"的长时间阻塞，让相关节点在演练时
   明确报"机器人未连接"而不是把整个 HTTP 请求拖住半分钟
2. 自动周期性触发 `manual_reset` 等信号（见 `AUTO_FIRE_SIGNALS`），模拟
   "现场有人在按人工复位按钮"
3. 演练结束后断开这些临时连接；若 mock 是这次演练拉起的，一并关掉
   （用户自己开着的 mock 不会被关）

## 已实现的示例流程（`flows/wrc_flow_main.json`）

用 `parallel` 节点完整对照复刻了 `programs/WRC/WRC.py` 里
`_execute_trans_component_async`（robot_a 分拣搬运）与
`_execute_assemble_async`（robot_b 装配）两条并行循环，并在人工复位后
由 robot_c 执行 `pick_box_to_sp` 拆垛（与 `WRC.py` 一致，不传 area），
包括真实代码里的动态选槽位、P4 三态分支等控制流细节：

```
p_start (parallel: join=all)
├── robot_a 分拣搬运循环：
│   a1 等待人工复位 → robot_c pick_box_to_sp 拆垛（失败则再等复位）→ P1→FULL
│   → 导航P1 → Service抓取零件A → find_slot 在P3找空箱槽位 n
│   → 导航{n} → 放下零件A → {n}→HALF → 按P1余量更新P1状态
│   → 导航P2 → 抓取零件B → 再导航{n} → 放下零件B → {n}→FULL
│   → P4满？是→等待腾空(轮询)；否→继续
│     → P4无箱？
│         是：直接把{n}满箱搬到P4 → {n}→NO_BOX, P4→FULL
│         否(P4空)：P4空箱→find_slot找无箱槽位k→搬到{k}→{k}→EMPTY，
│                   再把{n}满箱搬到P4 → {n}→NO_BOX, P4→FULL
│   → P1空？是→回home等补料；否→回P1继续 → 记录周期完成 → 回到a1（循环）
└── robot_b 装配循环：
    b1 等待P4满箱(轮询) → Service装配 → P4→EMPTY → 继续装配
    → 记录周期完成 → 回到b1（循环）
```

两条分支共享同一个 `FlowContext`（`p1_state`/`P3_1_state`/`P3_2_state`/
`p4_state` 等上下文变量），互相读写实现同步，等价于 `WRC.py` 里两个线程共享
同一个 `SlotTracker` 实例组的效果。这只是一个演示起点：实际部署时应在
图形化编辑器里按现场需求调整点位、task 名称、超时时间，或增删节点。
当前导航坐标在「📍 点位」弹窗顶部读取（`/zj_humanoid/navigation/odom_info`），
详见 `flow_editor/README.md`。
