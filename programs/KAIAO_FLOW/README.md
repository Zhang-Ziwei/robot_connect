# KAIAO_FLOW —— KAIAO 业务流程的图形化编排版

把 KAIAO 的业务步骤从 Python 代码搬到可视化流程图上，现场人员在浏览器里拖拽即可调整
搬箱顺序、增删状态记录、改导航点位，不用改代码重新打包 Docker。

**`programs/KAIAO/` 里的任何文件都没有被改动**，两套实现靠 `active_project` 切换，随时可以切回。

---

## 1. 与 programs/KAIAO/ 的关系

本项目**不重写任何业务逻辑**，只是把"业务步骤怎么串"从代码搬到流程图上。
所有实际动作仍然调用 `programs/KAIAO/KAIAO.py` 里现成的 `KAIAOHandler` 方法：

| 流程图上的节点 | 实际执行的代码 |
|---|---|
| 等待外部命令 | `SignalBus`（HTTP 整包 params 注入上下文） |
| 解析取放位置 `kaiao_bind_locations` | `_parse_box_location` / `_same_level_movement` |
| 解析分拣任务 `kaiao_bind_component_jobs` | `_parse_component_jobs`，摊成逐件队列 |
| 取下一件零件 `kaiao_next_component` | 从队列弹出一件，写入 `pick_nav` / `put_nav` 等 |
| 导航到点位 | `KAIAOHandler._navigate()`（含走廊中间点、离架后 `adjust_pose`、失败重试） |
| 发送操作动作 | `send_task_action()` + `_pick_box_task()` + 货架重量/持箱状态维护 |

这么设计是因为 KAIAO 的导航实现（`KAIAO.py:769` 起 330 余行）包含走廊中间点生成、
Planning Failed 抖动重试、feedback 取消协议等一整套现场调出来的细节。照抄一份过来
等于把现场踩过的坑重新踩一遍，而且今后两边还会各自漂移。

**代价**：本项目依赖 `programs/KAIAO/`，不像 `WRC_FLOW` 那样完全独立。这是刻意的取舍。

---

### 边界：编排独立，底层能力复用

- **编排层完全属于本项目**：命令注册、参数解析、流程调度、成功/失败判定都在
  `KAIAO_FLOW.py` 里，`cmd_handler` 只认本项目一个入口，不会出现两套编排抢同一条命令。
- **底层能力仍复用 `KAIAOHandler`**：导航（含走廊中间点）、抓放箱、货架重量追踪、
  持箱状态、回调发送等，当作"库"来调用。

之所以不把这些能力也复制一份过来：它们和 KAIAO 现场强绑定，别的项目根本用不上，
提炼成共享模块是过度抽象；而复制一份则会分叉——`_navigate` 一个方法就有 379 行，
走廊中间点算法另有 223 行，真机那边改了算法，这边不会有任何提示。
复用同一个 `KAIAOHandler` 实例还有个实际好处：货架重量和持箱状态只有一份，
不会因为两套实现各记一本账而对不上。

## 2. 走廊中间点算法怎么编排？——不画在图上

KAIAO 最特殊的地方是导航会**在运行时动态生成中间点**：根据机器人此刻的实时位姿、
目标点位、手上有没有箱子，判断属于 `end_to_end` / `end_to_side` / `side_to_side` 等
五种走廊场景中的哪一种，再决定插 0~2 个中间点、分几次 Action 下发。

**结论：这套算法完全封装在导航节点内部，流程图上看不到。**
图上只画一个"导航到 X"的节点，插不插中间点由节点自己在运行时决定。

之所以不拆成"算中间点 + 导航"两个节点、也不做成子流程，是因为那样图上会出现五个
场景分支，而分支条件（当前朝向属于走廊端点还是货架、手上有没有箱子）**在画图的时候
根本无从判断**——它取决于机器人执行到那一步时恰好在哪。画成显式分支既画不对，
也没人维护得了。

需要精细控制时，用导航节点上的两个参数兜底：

| 参数 | 取值 | 含义 |
|---|---|---|
| `mode` | `auto`（默认） | 按实时位姿自动插入中间点 |
| | `skip` | 完全不生成中间点，直达目标 |
| `mid_send` | `split`（默认） | 中间点与目标分多次 Action 下发 |
| | `batch` | 一次性发完 |

### 姿态校正也必须挂在导航里，不能拆成前一步

同样的道理还适用于 `adjust_pose`。导航节点有个 `on_leave_shelf` 参数，选
`adjust_pose` 就会在**离开货架的第一个中间点到达后**自动发校正指令，
与真机 `KAIAO.py::handle_pick_box_to_sp` 完全一致（同层也发，不再是"错层才做"）。

**不能改成"先校正、再导航"两个独立步骤**，原因是走廊场景的判定
（`_prepend_intermediate_waypoint`）完全依赖调用那一刻的实时里程计：

- 先退离货架再进导航，导航读到的是"已经退到走廊里"的位姿；
- 同侧货架且手上有箱时会落进 `side_to_side_same` 分支，该分支用
  `retreat_x = src_x - cos(yaw) * retreat` **再插一个后退中间点**，
  于是机器人一共后退两次；
- 其余场景的中间点坐标也会整体偏移一个后退距离。

配套地，挂了校正的导航节点会**强制忽略 `skip_if_at_pose`**：校正是挂在这次导航里
发的，跳过导航等于跳过校正，会出现"没校正就直接放箱"。真机同样为此去掉了放箱位的
前置位置判断。

---

## 3. 启用方式

KAIAO_FLOW 不参与 `ALL` 模式（否则会和 KAIAO 抢同一批 `cmd_type`），必须单独激活。
按优先级三选一：

1. **生产部署（推荐）**：把 `programs/KAIAO_FLOW/robot_config.json` 挂载到容器的
   `/config/robot_config.json`，其中 `active_project` 已经是 `KAIAO_FLOW`
2. 修改 `infrastructure/robot_config.json` 的 `active_project` 字段
3. 本地开发：修改 `infrastructure/constants.py` 的 `DEFAULT_ACTIVE_PROJECT`

激活后的命令分工：

| cmd_type | 由谁处理 |
|---|---|
| `PICK_BOX_TO_SP` | **流程图编排**（本项目） |
| `PICK_COMPONENT_TO_SP` | **流程图编排**（本项目） |
| `PICK_UP_BOX` / `PUT_DOWN_BOX` / `NAVIGATION` | 原 KAIAO 实现 |
| `GET_TASK_STATE` / `RESET_SYSTEM` | 通用实现 |

尚未图形化的命令继续走原实现，且**两者共用同一个 `KAIAOHandler` 实例**，
货架累计重量、持箱状态不会分裂成两份。对外 HTTP 接口与 KAIAO 完全一致，
现有的 `test_commands/KAIAO_*.json` 可以直接复用。编辑器「🤖 运行控制」的
「启动」只发 `START_WORKING`；业务命令用面板里的「模拟 HTTP 发送」或由 WCS 下发。

---

## 4. 命令模型：和 WRC_FLOW 不一样

这是理解本项目结构的关键。

| | WRC_FLOW | KAIAO_FLOW |
|---|---|---|
| 触发方式 | 一条 `PROCESS_BEGINS` 启动**常驻循环** | 每条命令触发一段**一次性任务** |
| 流程图 | 一张常驻大图 | **每个命令一张参数化的小图** |
| 参数 | 图里写死 | HTTP 入参原样进上下文；图上「等待外部命令」+「解析取放位置」体现字段映射 |
| 结束 | 不结束，等下一条控制命令 | 跑完回调外部系统 |

所以 KAIAO_FLOW 的执行链路是：

```
HTTP 命令 ──► KAIAO_FLOW.dispatch() ──┬─ 有流程正等这条命令？ ─► 唤醒它，注入命令参数
                                      │
                                      ├─ 已画成流程图？ ─► 起线程跑图（先把本条命令打进 SignalBus）
                                      │                   图开头 wait_for_command 立刻拿到 params
                                      │                   kaiao_bind_locations 解析成 pick_nav 等
                                      │                              │
                                      │                 task_state_machine + 回调外部系统
                                      │
                                      └─ 还没画成图 ─► 转交 programs/KAIAO 的现成实现
```

取放位置**不再在 Python 入口里解析成 `pick_nav` 再注入**。否则每加一个 HTTP 字段、
每加一个条件判断都要改代码。图上的等待节点把命令 params 原样写进上下文
（`{{box_initial_area}}`、`{{box_initial_area.shelf_type}}` 都能用），
绑定节点把字段映射画出来，改命令字段名或输出变量名改节点即可。

### 命令注册：编排入口只有一个

本项目对外注册的命令**全部**从 `dispatch()` 进，`cmd_handler` 不再直接引用
原生 KAIAO 的处理器。哪些命令走流程图、哪些暂时转交原生实现，是本项目内部的事：

| 配置 | 含义 |
|---|---|
| `COMMAND_FLOWS` | 已画成流程图的命令 → 流程图 id |
| `_FLOW_STARTERS` | 上述命令各自的启动入口（忙碌检查、起线程；入参校验在图上） |
| `PENDING_FLOW_COMMANDS` | 还没画成图的命令 → 暂时转交 `KAIAOHandler` 的哪个方法 |

把一条命令图形化，就是把它从 `PENDING_FLOW_COMMANDS` 挪到 `COMMAND_FLOWS`
并补上解析入口，**不用动命令分发那一层**。这样也不会出现
"一半命令走图、一半走原生"却从注册表上看不出来的局面。

### 等待外部命令：让流程中途停下来等放行

流程图里的「等待外部命令」节点现在能被真实 HTTP 命令唤醒。
`dispatch()` 收到命令时**先问引擎"有没有节点在等这条命令"**，有就唤醒、没有才当新任务受理。

顺序不能反：流程停在等待节点时任务状态机是「忙碌」的，
若先走新任务分支，唤醒命令会被忙碌检查挡掉，流程就永远醒不过来了。

命令的 `params` **原样**写入流程变量（嵌套对象保留，点号路径可用），
同时再写一份 `{前缀}_{键}`。节点上把「参数变量前缀」填成 `cmd`、
外部发来 `{"shelf_level": 3}`，图里后续节点既能用 `{{shelf_level}}` 也能用
`{{cmd_shelf_level}}`，整包另存一份在 `{{cmd_payload}}`。前缀留空则用命令名当前缀。
节点上的命令下拉框列的就是上面注册的那几条命令。

`PICK_BOX_TO_SP` 图从等待该命令开始：HTTP 一进来会先 `fire` 再跑引擎，
第一次等待立刻拿到这包参数，不用再等第二次请求。流程若已停在同一个等待节点上，
`dispatch` 走唤醒，不会再起一趟新任务。

### PICK_BOX_TO_SP 在图上的参数

HTTP 字段进上下文后可直接引用，例如：

| 写法 | 含义 |
|---|---|
| `{{box_initial_area}}` / `{{box_target_area}}` | 命令里的位置对象 |
| `{{box_initial_area.shelf_type}}` | 嵌套字段，条件判断也可以用这个变量名 |
| 其它新增 params 键 | 自动出现，不用改 Python |

「解析取放位置」节点再把位置对象写成后续导航/抓放箱用的变量（映射在节点参数上改）：

| 变量 | 说明 | 示例 |
|---|---|---|
| `pick_nav` / `put_nav` | 取箱位 / 放箱位导航点名 | `shelf0_2` / `agv_car0_0` |
| `pick_area` / `put_area` | ROS 的 area 参数 | `shelf` / `agv_car` |
| `src_coords` / `dst_coords` | 货架坐标 `[编号,层,列]`，用于重量追踪 | `[0,1,2]` |
| `pick_shelf_level` / `put_shelf_level` | 垂直层 | `1` |
| `same_level_movement` | 同侧货架搬运标记，仅放箱用 | `0` |
| `robot_id` / `task_id` | 机器人与任务标识 | `robot_a` |

---

## 5. 流程图：成功与失败两个出口

`flows/kaiao_pick_box_to_sp.json`：

```
等待 PICK_BOX_TO_SP ──► 解析取放位置 ──► 导航到取箱位 ──► 抓箱 ──► 导航到放箱位 ──► 放箱 ──► mark_done
                                                                  （离架后校正）                  (成功出口)

   等待超时 / 解析失败 / 任何一步动作失败 ──────────────────────────────► mark_failed
                                                                          (失败出口)
```

图上没有"是否错层"的分支：校正已经并入"导航到放箱位"这个节点，同层错层一视同仁，
和真机一致。

**图上必须有失败出口，这不是可选装饰。** 流程引擎的 `success` 只表示"图正常走完
没有异常中断"：某个节点失败后如果没有后继连线，引擎会认为流程优雅结束并返回
`success=True`。对 WRC_FLOW 那种常驻循环无所谓，但 KAIAO 的一次性任务如果照搬，
半路抓箱失败也会给外部系统回调一条"搬运完成"。

所以判定以图上两个显式出口为准（`KAIAO_FLOW.py::_flow_succeeded`）：

- 走到 `mark_done` 才算成功
- 碰过 `mark_failed` 就算失败，失败原因从执行轨迹里取最后一个失败节点的信息

**自己改图时请保证新增的关键节点也连上失败出口**，否则会退化成误报成功。

---

## 6. 节点类型

编辑器左侧面板把节点分成两大栏，避免"同名不同参"用错块。

### KAIAO 专有

| 节点 | 说明 | 关键参数 |
|---|---|---|
| **导航到点位** `kaiao_navigate` | 走廊中间点与离架后校正都内部自动处理 | `area`、`skip_if_at_pose`、`mode`、`mid_send`、`on_leave_shelf`、`adjust_area`、`adjust_shelf_level` |
| **解析取放位置** `kaiao_bind_locations` | 把命令里的位置对象解析成 `pick_nav` 等 | `source_pick` / `source_put`（默认 `{{box_initial_area}}` / `{{box_target_area}}`），以及各输出变量名 |
| **解析分拣任务** `kaiao_bind_component_jobs` | 把 params 摊成「一件一格」队列 | `source_jobs`（默认 `{{jobs}}`） |
| **取下一件零件** `kaiao_next_component` | 弹出一件，写入 `pick_nav` / `put_nav` / `has_piece` | 队列取尽时 `has_piece=false`，仍返回成功，后面接条件节点 |

导航之所以没并进通用节点，是因为走廊中间点策略和离架校正这些参数在别的项目
没有对应概念，硬凑成同一个 `navigate` 只会让两边都难用。反过来，KAIAO 面板上
也**不会出现**通用的 `navigate`——通用节点是项目可选实现的，本项目没实现就不显示，
免得拖上去要等跑起来才报"未知节点类型"。

### 通用节点（定义在 `core/common_nodes.py`，本项目提供 KAIAO 实现）

| 节点 | 说明 | 关键参数 |
|---|---|---|
| **发送操作动作** `send_operation` | 发 Action 或 Service | `call_type`、`task`、`area`、`extra_params`、`timeout` |
| **记录状态步骤** `update_step` | 写任务状态机，供 `GET_TASK_STATE` 回显 | `step`、`message` |

加上引擎内置的 `condition` / `set_variable` / `delay` / `parallel` /
`wait_for_command` / `sub_flow` / `noop`。三层节点的划分标准见 `core/common_nodes.py` 开头。

`send_operation` 的 KAIAO 实现比字面意思多做两件事：

1. `task` 填 `auto_pick` 时，按货架当前累计重量自动在 `pick_up_box` /
   `pick_up_heavy_box` 之间选择；`task` 填 `auto_put` 时按本次抓取配对
   `put_down_box` / `put_down_heavy_box`（抓了重箱就必须用 `put_down_heavy_box`）。
   操作人员不用关心重量阈值。
2. 抓/放箱成功后维护"货架格累计重量"和"手上有没有箱子"；`put_down_component`
   成功后按该零件重量累加到对应货架格。定位货架格用的坐标
   **不是节点参数**（它不发给 ROS，通用参数表里没有位置），而是按任务类型从流程变量
   取：抓箱用 `src_coords`、放箱/放零件用 `dst_coords`。和走廊中间点一样属于处理器内部
   自动维护的业务副作用，图上不体现。

KAIAO 用不到 WRC_FLOW 的 `plc_action`（没有传送带 PLC）和 `find_slot`（货架状态是
累计重量而非空/半满/满的枚举）。

---

## 7. 配置与导航点位

`programs/KAIAO_FLOW/robot_config.json` 只写本现场相关的 `robots` / 端口 / `active_project`。
`load_config()` **浅合并**（低 → 高）：

1. `infrastructure/robot_config.json`
2. `programs/KAIAO/robot_config.json`（自动充电、回调、`navigation_map` 运行时段等共用段）
3. `programs/KAIAO_FLOW/robot_config.json`（覆盖 `robots`，含 IP 与地图名）
4. `/config/robot_config.json`（Docker 挂载，若存在）

这样 FLOW 文件不必再抄一份自动充电。机器人 `navigation_map` 必须与现场
`get_map_list` 里的名字一致（当前现场是 `kion_com_3`，不是 `kaiao_com_3`）。

**点位与 KAIAO 共用一份**：`programs/KAIAO/robot_config.json` 的
`navigation_poses.KAIAO`。编辑器「📍 点位」改的也是这一份。
当前导航坐标在「📍 点位」弹窗顶部读取（`/zj_humanoid/navigation/odom_info`），不混在点位表每一行里。

---

## 8. 演练（Dry-run）

编辑器里点"演练"会自动拉起 `mock_rosbridge_server` 跑一遍完整流程，结束后关掉
这次拉起的 mock，不碰真实机器人。若本机 9090 上已经有一份 mock 在跑，则直接复用，
演练结束也不会关它。

因为图从 `wait_for_command` 开始，演练没有真实 HTTP。点「▶ 演练」会弹出
**演练输入**：每个等待节点一份 JSON，默认用节点上的 `dryrun_params`
（与 HTTP `params` 同结构，可含 `robot_id`；分拣任务放在 `jobs` 里）。
改这份 JSON 就能测不同货架格、零件种类/数量，也可以直接粘贴
`test_commands/` 里的整包命令。开始演练会把内容写回节点，保存流程即可记住。

### 演练测不到的部分

`mock_rosbridge_server` 不发布机器人里程计（`ROBOT_MOTION_STATE` 话题），
所以凡是依赖实时位姿的逻辑，演练走的都是"拿不到位姿"的降级分支：

| 逻辑 | 演练里的表现 |
|---|---|
| 走廊中间点 | 一律不插中间点，直达目标 |
| 离架后 `adjust_pose` | **仍会发**，走"直达 + 延迟 3 秒补发"的分支（日志里能看到 `无中间点，3s 后 adjust_pose`） |
| `skip_if_at_pose` | 永远不命中，每个导航节点都实打实跑一遍 |

也就是说校正指令本身在演练里能验证，**但"发在第一个离架中间点之后"这个时机不能**，
中间点相关的分支只能在真机上确认。

想看失败出口在图上是什么效果，把演练用的点位改成一个配置里不存在的名字，
导航节点会失败并汇到 `mark_failed`。

---

## 9. 文件结构

```
programs/KAIAO_FLOW/
├── KAIAO_FLOW.py                    命令入口：起线程跑图（入参由等待节点注入）
├── node_handlers.py                 节点处理器 + 专有节点 schema + 通用节点声明
├── dryrun_adapter.py                演练用的 mock 机器人与自动 fire 的示例命令
├── robot_config.json                本现场 robots / 端口（其余段从 KAIAO 合并）
├── flows/
│   ├── kaiao_pick_box_to_sp.json         PICK_BOX_TO_SP 流程图
│   └── kaiao_pick_component_to_sp.json   PICK_COMPONENT_TO_SP 流程图
└── README.md
```

## 10. 再迁移一个命令要做什么

1. 在 `flows/` 下画一张新图（需要入参就画 `wait_for_command`，位置类字段用
   `kaiao_bind_locations` 或条件节点直接引用 `{{字段.子键}}`）
2. `KAIAO_FLOW.py` 的 `COMMAND_FLOWS` 加一行 `cmd_type → 流程图 id` 映射
3. 照着 `handle_pick_box_to_sp` 写一个入口：忙碌检查 → 起线程跑图（先 `fire` 本条命令）
4. 如果需要新的动作类型，在 `node_handlers.py` 加处理器和 schema

不用再改 `cmd_handler`：命令已经全部从 `dispatch()` 进。
新增 HTTP 字段优先改流程图（等待节点会自动注入），不要为每个字段改 Python。

带列表循环的命令（如已迁入的 `PICK_COMPONENT_TO_SP`）不要在图上画三层环：
用绑定节点把 job × target × 件数摊成逐件队列，再用「取下一件 + 条件」成环。
