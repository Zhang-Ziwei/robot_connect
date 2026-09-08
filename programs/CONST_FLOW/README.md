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
5. 装表：MQTT action=0 → **硬等 3 秒龙门架** → `install_gauge` → action=1 → 等识别（超时则 `dutInfo`）  
   - `leakTestPassed=false` 或识别失败：重插 2 次，超过按坏表拆回后部  
   - 机器人 `can_reinsert=false`：心跳 `needHuman=true`，等 `CONST_HUMAN_HANDLED`  
6. 请求启动检定；等 `end_notify`  
   - `isLeak`：全部工位重装再 `start`  
   - `isNormal=false` 且未泄漏：等 `isPendingConfirm` 变 false，再 `start`  
   - 正常：按 `stationDetails.isPassed` 拆表回后部  
7. 箱内还有未放的表 → 回到第 4 步（例如 4 块表只有 3 个工位）  
8. 下料货架 `put_down_box` → 搬箱等待区 → 全部工位 `待装表` 后再去来料货架

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
