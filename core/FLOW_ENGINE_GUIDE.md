# Flow Engine 使用指南

`core/flow_engine.py` 是一个通用的、与具体项目/机器人无关的流程执行引擎。
它把"流程的形状"（先做什么、并行做什么、遇到条件怎么分支、循环几次）从
"每一步具体怎么做"（怎么导航、怎么呼叫某个 service）中分离出来，前者用 JSON
描述，后者由各项目自己写 Python 函数（handler）注册进来。

这样图形化编辑器（`flow_editor/`）只需要读写 JSON，操作人员就能在不改一行
Python 代码的情况下重新组装/调整全流程。

## 为什么要有这一层

以 `programs/WRC/WRC.py` 为例，原来的写法是：

```python
def _execute_trans_component_async(self, task_id, robot_id):
    while True:
        self._navigate(robot, NavigationPose.P1, "P1点位", robot_id)
        self._send_component_service(robot, WRCTask.PICK_UP_COMPONENT_A, ...)
        ...
```

流程完全写死在代码里：现场想调整一下顺序、换个点位、加一步操作，都得改代码、
重新打包镜像、重新部署。`flow_engine` 把这段"硬编码的控制流"换成一份 JSON：

```json
{"nodes": [...], "edges": [...]}
```

由引擎解释执行；具体每个节点做什么（比如 `navigate` 节点怎么导航）仍然是
Python 函数，只是从"写死的调用顺序"变成了"按需注册、按图调度"。

## 三层架构

```
┌───────────────────────────────────────────────────────────┐
│ 1. core/flow_engine.py — 通用引擎，不认识任何机器人/ROS 细节 │
│    只认识：节点/边/条件/并行/延时/等待信号/子流程/变量         │
├───────────────────────────────────────────────────────────┤
│ 2. programs/<PROJECT>/node_handlers.py — 项目专属           │
│    把 "navigate" "send_operation" 等业务节点类型             │
│    翻译成具体的 RobotController 调用                        │
├───────────────────────────────────────────────────────────┤
│ 3. flow_editor/ + network/flow_api_server.py — 图形化编辑器  │
│    读写 JSON 流程图，调用引擎做"演练"(dry-run)                │
└───────────────────────────────────────────────────────────┘
```

## JSON 流程图格式

```json
{
  "start": "n1",
  "context_vars": { "robot_id_a": "robot_a" },
  "nodes": [
    {"id": "n1", "type": "navigate", "label": "导航到P1",
     "params": {"robot_id": "robot_a", "pose": "P1"}, "x": 100, "y": 100}
  ],
  "edges": [
    {"source": "n1", "target": "n2", "when": "default"},
    {"source": "n2", "target": "n3", "when": "true"},
    {"source": "n2", "target": "n4", "when": "false"}
  ]
}
```

- `start` 指定起始节点 id；`context_vars` 是流程开始运行前预置到 `FlowContext`
  的初始变量（比如角色到具体 `robot_id` 的映射、槽位初始状态等）。
- 每条边用 `when` 字段区分走哪条出边：普通节点只有一条 `"default"` 出边；
  `condition` 节点靠 `"true"`/`"false"`；`navigate`/`send_operation` 等"动作类"
  节点靠 `"success"`/`"failure"`。`parallel` 节点的分支不靠边表达，而是靠
  `params.branches`（一串节点 id，各自独立开一条子链路并发执行，共享同一个
  `FlowContext`，因此可以互相读写变量做同步）。
- `x`/`y` 是图形化编辑器（`flow_editor/`）自己维护的画布坐标，引擎本身不使用、
  也不会因为缺失而报错；没有坐标的节点编辑器会自动按网格顺序摆开。
-   字符串参数里可以用 `{{变量名}}` 引用 `FlowContext` 中的变量（模板语法）。
  整串恰好是 `"{{var}}"` 时会保留变量原始类型（不强制转成字符串），比如
  `"timeout": "{{my_timeout}}"` 在 `my_timeout` 是数字时会解析成数字而不是字符串。
  变量名支持点号路径：`{{box_initial_area.shelf_type}}` 会先精确匹配整个键，
  没有再沿 dict 往下取，这样 HTTP 命令里的嵌套 params 可以直接用在模板和
  `condition` 上，新增字段不必改 Python。
  **`set_variable`/`condition` 节点的 `var` 字段本身也支持这种模板**，可以拼出
  动态变量名，比如先用 `find_slot` 找到槽位名存进 `p3_n`，再用
  `"var": "{{p3_n}}_state"` 去设置/判断"那个具体槽位"的状态，不需要为每个具体
  槽位单独画一份 `condition`/`set_variable` 节点。

## 内置节点类型（引擎自带，与项目无关）

| 类型 | 参数（`params`） | 出边 `when` | 说明 |
|---|---|---|---|
| `noop` | 无 | `default` | 空节点/占位（常用作多路分支的汇合点） |
| `set_variable` | `{var, value}` | `default` | 设置一个上下文变量；`var`/`value` 均支持 `{{tpl}}` |
| `delay` | `{seconds}` | `default` | 延时（可被暂停/停止打断） |
| `condition` | `{var, op, value}` | `true` / `false` | 条件分支，`op` 支持 `== != > < >= <= in not_in truthy falsy` |
| `parallel` | `{branches, join, timeout}` | `success` / `failure` | 并行执行多条子链路；`join`: `all`（默认，全部完成才算成功）/`any` |
| `wait_for_command` | `{event_name, var_prefix, timeout, dryrun_params}` | `success` / `failure` | 阻塞等待外部信号（配合 `SignalBus`），超时或收到信号才继续。命令 params **原样**写入上下文（含嵌套 dict，可用 `{{box_initial_area.shelf_type}}`），同时再写一份 `{前缀}_{键}`；整包另存 `{前缀}_payload` / `cmd_payload`。`dryrun_params` 是演练时自动打进来的示例入参，可在编辑器弹窗里改。 |
| `sub_flow` | `{flow}` | `success` / `failure` | 加载并运行另一份流程 JSON（子流程复用），`flow` 是目标 `flow_id` |

## 项目专属节点类型

引擎完全不认识 `navigate` / `send_operation` 这类节点，必须由项目通过
`FlowEngine(handlers={...})` 注册处理函数才能执行。参考实现见
`programs/WRC_FLOW/node_handlers.py`，目前登记了这些类型：

| 类型 | 说明 |
|---|---|
| `navigate` | 导航到点位，支持 `skip_if_at_pose` 跳过已到点的导航 |
| `send_operation` | 发送操作动作，`call_type`（`action`/`service`）+ `service`（`robot_task`/`robot_task_geely`）二选一，合并了原来"呼叫 Action"和"呼叫 Service"两种节点 |
| `plc_action` | PLC/传送带动作（正转/反转/停止），对接 `programs/WRC/plc_modbus.ConveyorController`；没有配置传送带（`conveyor=None`）时优雅降级为跳过并返回成功，不会阻塞流程图 |
| `find_slot` | 按状态在一组槽位变量（如 `P3_1_state`/`P3_2_state`）里查找第一个匹配的槽位并写入 `output_var`，用来表达"动态选一个可用槽位"这类分支逻辑（对应 `programs/WRC/WRC.py` 里 `SlotTracker.find_slot_by_state`） |
| `update_step` | 写入 `ParallelTaskStateMachine`，供 `GET_TASK_STATE` 查询回显 |

```python
def build_handler_registry(robots, task_state_machine, get_robot=None, conveyor=None):
    def handle_navigate(node, ctx):
        ...
        return True  # 或 False 表示失败，中止链路
    return {"navigate": handle_navigate, ...}
```

`handler(node, ctx) -> bool`：`node.params` 是原始参数字典（可能含
`{{var}}` 模板，需要 handler 自己调用 `ctx.render(...)` 解析），返回 `True`
表示成功继续（走 `success`/`default` 出边），`False` 表示失败（走 `failure`
出边；如果该节点没有 `failure` 出边，链路就在此安全终止，不会报错崩溃）。

新增一种业务节点类型只需要：
1. 在 `node_handlers.py` 里写一个 `handle_xxx(node, ctx) -> bool`
2. 在 `build_handler_registry()` 返回的 dict 里注册 `"xxx": handle_xxx`
3. 在 `NODE_TYPE_SCHEMAS` 里加一份表单 schema（供前端渲染参数编辑器，格式见下一节）

不需要改 `core/flow_engine.py` 或 `network/flow_api_server.py` 一行代码。

## 节点参数表单 schema（`*_NODE_TYPE_SCHEMAS`）

`core.flow_engine.BUILTIN_NODE_TYPE_SCHEMAS`（内置节点）和各项目
`node_handlers.py` 里的 `NODE_TYPE_SCHEMAS`（项目节点）结构完全一致，
`network/flow_api_server.py` 会把两边合并后通过
`GET /api/projects/<p>/node-types` 返回给前端，供 `flow_editor/app.js`
动态渲染节点面板和参数编辑表单：

```python
"send_operation": {
    "label": "发送操作动作",       # 面板/画布上显示的中文名
    "category": "机器人动作",      # 面板里的分组标题，纯展示用
    "fields": [                    # 有序数组，按顺序渲染表单
        {"name": "call_type", "type": "select", "label": "调用方式",
         "required": True, "options": ["action", "service"], "default": "action"},
        {"name": "task", "type": "text", "label": "任务名(task)", "required": True},
        {"name": "timeout", "type": "number", "label": "超时秒数", "default": 1200},
    ],
    "outputs": ["success", "failure"],  # 出边可用的 when 取值
}
```

`fields[].type` 支持：`text`（单行文本）/ `number`（数字）/
`select`（下拉框，配 `options`）/ `checkbox`（布尔）/ `json`（多行文本框，
按 JSON 解析，用于数组/对象类参数，比如 `parallel.branches`、
`send_operation.extra_params`）。

## 暂停 / 停止语义

与 `WRC.py` 里手写的 `_check_flow_control` 语义完全一致：

- `pause_event`（缺省常驻 set）：clear 时，引擎会在下一个节点执行前阻塞等待，
  直到重新 set 或 `stop_event` 被 set。
- `stop_event`：set 时，引擎会在下一个检查点（每个节点开始前、`delay`/
  `wait_for_command` 内部的轮询间隙）抛出 `FlowStopped` 并安全退出，不会遗留
  在半途。`parallel` 节点内部的每条分支也共享同一对 `pause_event`/`stop_event`，
  收到停止信号会各自退出后再汇合。

## 变量与上下文（FlowContext）

- `ctx.get(key, default)` / `ctx.set(key, value)` / `ctx.update(dict)`：
  线程安全的变量读写，跨节点、跨并行分支共享。
- `ctx.extra`：存放不需要 JSON 序列化的运行时对象（比如 `robots` 字典），
  引擎本身不碰它，只透传给 handler 使用。
- `ctx.snapshot()`：返回当前变量的浅拷贝，`FlowResult.context` 就是流程结束时
  的这份快照。

## SignalBus 与外部信号

`wait_for_command` 节点靠 `SignalBus` 和外部事件（比如 HTTP 命令）打通。

引擎会把"此刻有哪些节点在等信号"暴露出来，命令入口据此判断这条命令
是该唤醒流程、还是该当成一条新任务受理：

```python
engine.waiting_signals()              # -> {"PICK_UP_BOX"}
engine.is_waiting_for("PICK_UP_BOX")  # -> True
```

判断顺序很重要：**先判唤醒，再判新任务**。否则流程停在等待节点时，
外部发来的唤醒命令会被任务状态机的忙碌检查挡掉，流程永远醒不过来
（参见 `programs/KAIAO_FLOW/KAIAO_FLOW.py::dispatch`）。

信号携带的数据会写入上下文，两份并存：

1. **原键**（含嵌套对象、以及一层点号键如 `box_initial_area.shelf_type`），
   图上可以直接 `{{box_initial_area}}` / 条件判断 `var: box_initial_area.shelf_type`。
   新增 HTTP 字段不用改引擎。
2. `{var_prefix}_{键}` ——多个等待节点并存时避免互相覆盖。
   `var_prefix` 填 `cmd`、命令 params 是 `{"shelf_level": 3}` 时，图里还能用
   `{{cmd_shelf_level}}`。留空则前缀用信号名。

整包另存 `{{cmd_payload}}` 和 `{{<前缀>_payload}}`。带了 `robot_id` 时还会写一份
不带前缀的 `{{robot_id}}`，方便导航 / 调动作节点直接引用。


```python
bus = SignalBus()

# 外部线程（收到 MANUAL_RESET_COMPLETED HTTP 命令时）：
bus.fire("MANUAL_RESET_COMPLETED", {"operator": "张三"})

# 流程图里配置一个 wait_for_command 节点：
# {"type": "wait_for_command", "params": {"event_name": "MANUAL_RESET_COMPLETED"}}
```

`WRCFlowHandler.handle_manual_reset_completed` 就是这么把 HTTP 命令接入信号总线的，
参见 `programs/WRC_FLOW/WRC_FLOW.py`。

## 演练（dry-run）模式

引擎本身不区分"演练"和"生产"，区别完全在于调用方传入了什么样的 `handlers`
和 `robots`：

- 生产模式：`programs/<PROJECT>/WRC_FLOW.py` 传入真实机器人的
  `RobotController`，`FlowEngine.run()` 在业务线程里跑，出错走真实报警。
- 演练模式：`network/flow_api_server.py` 通过项目提供的 `dryrun_adapter.py`
  连接 mock rosbridge（`mock_rosbridge/mock_rosbridge_server.py`）的临时
  `RobotController`，跑一遍流程，收集每个节点的执行轨迹（`on_event` 回调）
  返回给前端在画布上高亮回放，不影响任何真实机器人。
  所需本机端口无人监听时会自动拉起 mock，演练结束再关掉；已有 mock 在跑则复用。

## 流程文件存放位置

`programs/<PROJECT>/flows/*.json`，每个文件是一份独立的流程图，通过
`network/flow_api_server.py` 的 API 增删改查。文件名（不含 `.json`）就是
`flow_id`。现场由流程 JSON 顶层的 `enabled` 开关决定哪一份会被
`PROCESS_BEGINS` / 业务命令拉起（编辑器工具栏「激活」）。
若 Docker 挂载了 `/config/flows/`，保存优先写到那里（宿主机上，换镜像不会丢）；
否则写回项目内置目录。

**软件更新前请用编辑器「⬇ 输出」把流程下载到本机**（当前一份，或本项目全部打成迁移包），
更新后再用同一按钮「导入」还原。单份输出就是磁盘上的流程图 JSON；迁移包带
`"format": "robot_connect.flow_pack"`。导入时同名文件会先备份再覆盖。

`PROCESS_BEGINS` 是流程总开关：`robot_config.flow_control.require_process_begins`
默认 true（展会：START_WORKING 只连机器人，不会自动开跑）。上位机项目另有开任务
指令时把该项设为 false。
