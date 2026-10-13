# XINXIANG —— 定量分液 / 上料检测 / 数据采集（图形化）

旧化工线上的定量分液、检测模块放料/启动/取料、数据采集，做成一张线性流程图。  
`START_WORKING` 只连机器人。图的第一块是「等待外部命令」，收到 `PROCESS_BEGINS` 后才往下跑。跑到没有出边就停；只有把最后一步连回等待节点，才会再等下一次 `PROCESS_BEGINS`。

## 启用

把 `programs/XINXIANG/robot_config.json` 挂到容器 `/config/robot_config.json`，或把其中 / 外部配置的 `active_project` 改成 `XINXIANG`。不参与 `ALL` 模式。

## 主流程（图上可改）

`flows/xinxiang_main.json`：

```
等待 PROCESS_BEGINS → 开始一轮 → 试管1
试管2 ────────┐
              ├─（都完成）→ 检测启动 → 检测取料位移 → 检测取料完成 → 结束
检测放料位移 ─┘
```

| 步骤 | 节点 | 实际动作 |
|---|---|---|
| 试管1 | `send_operation` task=`pure_water` extra `type=1` | `/robot_task` |
| 试管2 | `send_operation` task=`pure_water` extra `cube=2` | `/robot_task` |
| 检测放料位移 | `xinxiang_detect` step=`dispense` | 置位线圈 5，等 4003=2 |
| 检测启动 | `xinxiang_detect` step=`start` | 线圈 6，等 DETECT_STATE=4 |
| 检测取料位移 | `xinxiang_detect` step=`pick` | 线圈 7，等 DETECT_STATE=5 |
| 检测取料完成 | `xinxiang_detect` step=`finish` | 线圈 8，等 DETECT_STATE=1 |

检测握手：本机是 Modbus TCP **服务器**，检测模块作为 **客户端** 连进来。放料位移置位线圈 5，等对方把 4003 写成 2。`plc.enabled=false` 或编辑器演练时跳过握手、视为成功。

上位机写出的布尔信号不会一直保持。模块状态到达下表的复位值后，本机把对应线圈拉回 0：

| 信号 | 置位时的状态 | 复位时的状态 |
|---|---|---|
| 开盖启动 | 1 | 2 |
| 开盖取料完成 | 3 | 0 |
| 关盖启动 | 1 | 2 |
| 关盖取料完成 | 3 | 1 |
| 检测放料位移 | 1 | 2 |
| 检测启动 | 2 | 3 |
| 检测取料位移 | 4 | 5 |
| 检测取料完成 | 5 | 1 |
| 清洗启动 | 1 | 2 |
| 清洗取料完成 | 3 | 1 |

操作只走标准 `/robot_task`（图上 `call_type=action`）。附加参数写在 `extra_params`。

## 运行控制

1. `active_project` 设为 `XINXIANG`（改完后若 main.py 已在跑，先「重置系统」）
2. 用 mock 时打开 **Mock**，再点 **启动**（`START_WORKING`）连机器人
3. 点 **流程开始**，或在「模拟 HTTP 发送」里发 `PROCESS_BEGINS`（也可用 curl 打 8090）
4. 暂停 / 恢复 / 结束用 `PROCESS_PAUSED` / `PROCESS_RESUMED` / `PROCESS_ENDED`
5. 这一遍结束后要再跑：再发一次 `PROCESS_BEGINS`。若图上已把结束连回「等待外部命令」，同一次运行会停在那里等下一条

`flow_control.require_process_begins` 默认 `true`。改成 `false` 则 `START_WORKING` 连上后自动开跑这一遍。

顶部「▶ 演练」只空跑当前画布，不经过运行控制。
