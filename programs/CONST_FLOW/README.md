# CONST_FLOW —— 康斯特压力表全自动检定（图形化）

机器人侧调度：转接 ACal MQTT 消息，并下发导航 / ROS Service。  
`START_WORKING` 连上机器人。是否立刻开跑看 `flow_control.require_process_begins`（本项目默认 false，连上即跑；设 true 则必须先发 `PROCESS_BEGINS`）。暂停/恢复/结束用 `PROCESS_PAUSED` / `PROCESS_RESUMED` / `PROCESS_ENDED`。

演练对接现场已有的 MQServer / 上位机模拟器（默认 `127.0.0.1:8870`），不在本仓库里假扮检定系统。

## 启用

把 `programs/CONST_FLOW/robot_config.json` 挂到容器 `/config/robot_config.json`，或改其中 / 外部配置的 `active_project` 为 `CONST_FLOW`。不参与 `ALL` 模式。

依赖：`paho-mqtt`（Docker 镜像已加入）。本机：`pip install paho-mqtt`。

## 自动主循环（图上可改）

1. 连 MQTT，发现检定系统，记住回传的 `calsysId`，之后一直用它  
2. 等 `bindingRobots` 包含本机  
3. 来料货架 → 检查 PLC（配置未启用则跳过）→ `pick_up_box`  
   - `return_params`: `{"has_box": false}` 则隔几秒再搬  
   - `{"has_box": true, "gauge_count": N}` 写入剩余表数  
4. 检定系统空闲（`isRunning=false` 且 `isPendingConfirm=false`）时，从 live.stations 里选 `待装表` 工位  
5. 装表：见下方「装表：三张图分层」，整段编排都在图上，不用改 Python  
6. 请求启动检定；等 `end_notify`  
   - `isLeak`：全部工位重装再 `start`  
   - `isNormal=false` 且未泄漏：等 `isPendingConfirm` 变 false，再 `start`  
   - 正常：按 `stationDetails.isPassed` 拆表回后部  
7. 箱内还有未放的表 → 回到第 4 步（例如 4 块表只有 3 个工位）  
8. 下料货架 `put_down_box` → 搬箱等待区 → 全部工位 `待装表` 后再去来料货架

## 装表：三张图分层

装表这段原先整块封装在 `station_logic.install_station()` 里，图上只有一个方块，
想调顺序、加一步、改重插次数都得改 Python。现在编排搬到了图上，分三层：

只有 `const_main` 是**入口流程**（`PROCESS_BEGINS` 跑的就是它），另外三张是**子流程**，
在 JSON 里用 `"role": "subflow"` 标着。子流程没有自己的启动入口，不参与"现场只能激活一份"
的判断，编辑器里它们的激活开关是禁用的、下拉框里标「子流程」。
新增子流程时记得带上这个字段，否则会被当成第二个入口，主流程启动时会报
"同时激活了多份流程"。

| 流程图 | 管什么 | 想改什么就打开它 |
|---|---|---|
| `const_main` | 主循环（30 个节点，装表这里仍是一个块）· **入口** | 搬箱、选工位、检定启停的整体顺序 |
| `const_install_station` | 单工位：重插循环 + 三种结局收尾 | 重插几次、超次数怎么处理、什么时候呼人工 |
| `const_install_once` | 装一次表的完整步骤 | 让位/合龙的时机、等龙门架几秒、装表前后加步骤 |
| `const_uninstall_once` | 拆一次表的完整步骤 | 拆表的让位/合龙时机、前后加步骤 |

在编辑器里选中「装表并等识别/检漏」这个块，属性面板上有 **✎ 打开子流程** 按钮，点了直接进去改。

### 机器人动作用通用功能块

导航、装表、拆表都是**通用节点**，不是 ConST 专有的：

| 动作 | 用哪个节点 | 关键参数 |
|---|---|---|
| 导航到工位 | 「导航到点位」`navigate` | `pose` = `{{station_pose}}` |
| 机器人装表 | 「发送操作动作」`send_operation` | `call_type=service`、`task=install_gauge`、`area={{station_pose}}` |
| 机器人拆表 | 「发送操作动作」`send_operation` | 同上，`task=uninstall_gauge`、`extra_params={{uninstall_extra}}` |

ConST 专有节点只剩上位机 MQTT 往来和记账（让位/合龙、取表信息、等识别/检漏、记为已装表、料箱表数 -1），
它们没有通用节点可以替代。机器人上报状态用「设置机器人上报状态」节点，显式画在图上。

拆表的 `extra_params` 取自上下文变量 `uninstall_extra`，调用子流程前用「设置变量」写好：
重插前拆填 `{"reinstall": true}`，按检定结果拆回填 `{"is_passed": true/false}`。

### 几个约定

- **子流程结果靠变量传回。** `sub_flow` 节点的 success 只表示图跑完了，不代表装表成功。
  `const_install_once` 把结果写进 `install_result`（`ok` / `human` / `retry`），
  `const_uninstall_once` 写 `uninstall_result`（`ok` / `human` / `fail`），
  外层用条件分支去判。加新结局时记得同步外层的分支。
- **表掉了不算动作失败。** 机器人带回 `can_reinsert=false` 时动作本身是成功的，
  `send_operation` 会把它写成 `gauge_dropped`，图上用条件分支判后转呼人工。
- **呼人工只在外层做。** `const_call_human` 节点是「发通知 + 等处理」两件事，
  子流程里只标结果不要自己呼，否则会等两次。
- **等龙门架的 3 秒是图上的「延时等待」节点**，不再是 `ConstTimeout.GANTRY_WAIT` 常量，直接改节点参数即可。
- **重插次数是 `chk_retry` 节点的比较值**（默认 `install_attempt < 2`，即最多试 3 次），
  计数用「设置变量」的 `add` 运算。
- **每种结局都要走到「料箱表数 -1」。** 成功、呼人工、超次数拆回三条路都得汇到 `const_consume_gauge`，
  否则 `remaining` 不减，主图的装表循环退不出来。保存时会自动检查这一点并给出警告。

底层能力没动：机器人动作仍在 `robot_ops.py`，上位机 MQTT 协议仍在 `calsys_ops.py`，
图节点只负责把参数翻译成对它们的一次调用。

`station_logic.py` 保留着 `install_once` / `uninstall_once` / `install_station`
（`reinstall_all`、`uninstall_all` 还在用，旧流程图也还能跑），
但 `const_install_station` 这个整块节点已不在编辑器面板上，不建议再用。

## ROS Service 约定（`/robot_task`）

| task | area | extra_params | return_params |
|---|---|---|---|
| `pick_up_box` | `inbound_shelf` | | `has_box`, `gauge_count` |
| `put_down_box` | `outbound_shelf` | | |
| `install_gauge` | `station_<n>` | | `can_reinsert`（缺省视为 true） |
| `uninstall_gauge` | `station_<n>` | `is_passed` 或 `reinstall: true` | `can_reinsert` |

点位：`inbound_shelf` / `outbound_shelf` / `box_wait` / `home` / `station_1`… 工位变多在编辑器「点位」按 `station_<sequenceNumber>` 添加。

## MQTT 心跳扩展（规范 4.1）

在 live 里增加 `needHuman`、`humanReason`（`gauge_dropped` / `mqtt_timeout` / `calsys_error`）。  
上位机处理后对本服务发 `CONST_HUMAN_HANDLED`。

检定系统 live 使用 `isRunning` / `isPendingConfirm`（不再靠 status 字符串判断能不能装表）。

## 命令

| cmd_type | 说明 |
|---|---|
| `START_WORKING` | 连机器人，自动开流程 |
| `PROCESS_PAUSED` / `RESUMED` / `ENDED` | 暂停 / 恢复 / 停自动循环 |
| `CONST_HUMAN_HANDLED` | 清除呼叫人工 |
| `CANCEL_NAVIGATION` | 取消当前导航 |
| `RESET_SYSTEM` | 停流程、断 MQTT，回休眠 |

## PLC

`robot_config.json` 的 `plc.enabled=false` 时检查节点直接成功。打开后按 `ready_holding_registers` 读保持寄存器，值等于 `ready_value` 才算就绪。具体地址现场填。
