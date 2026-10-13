# ZHANTING — 展厅双杯分液检测（图形化）

在现有流程编辑器的项目列表选择「展厅」，打开 `zhanting_main`。所有步骤、参数、并行分支和汇合都在图上维护；机器人操作、导航、HTTP 和 PLC 均使用通用节点。

## 流程

人工上料确认 → 机械臂送瓶开盖 → PLC 开盖并等完成 → 机械臂取瓶到机器人前方 → 通知开盖取料完成 → 第一杯分液。

随后并行进行第二杯分液与检测放料移位，两项都完成后并行进行：

- 关盖归位：机械臂从桌面取瓶到关盖器 → 关盖完成 → 机械臂取瓶回上料位 → 通知关盖取料完成。
- 检测倒液：检测完成 → 读取样品二维码 → 第一杯倒废液 → 检测取料移位完成 → 第二杯倒废液 → 通知检测取料完成。

两条分支均成功才结束。每次运行一轮；不自动重复。

| 机器人操作 | task | extra_params |
|---|---|---|
| 第一杯分液、放杯到旋转机构 | `pure_water` | `{"cube":1}` |
| 第二杯分液、放杯并放下试剂瓶 | `pure_water` | `{"cube":2}` |
| 第一杯倒废液 | `pour_water` | `{"cube":1}` |
| 第二杯倒废液 | `pour_water` | `{"cube":2}` |

默认 `send_operation` 使用标准 `/robot_task` Action，也可在节点中选 Service。

展厅机器人固定位，默认图里没有移动步骤。通用节点不分项目，侧边栏照样能看到「导航到点位」以及全部 HTTP / PLC 节点，随时可以拖到画布上；导航点位在本项目 `robot_config.json` 的 `navigation_poses.ZHANTING` 里填实测坐标后出现在下拉框中。

## 启用与运行

本次未切换当前 `infrastructure/robot_config.json` 的项目。启用实机时将其 `active_project` 改为 `ZHANTING`，或把本目录 `robot_config.json` 挂载到 `/config/robot_config.json`。填写本项目配置中的机器人连接、PLC 映射和机械臂 URL；主程序已启动时先重置系统。

运行控制顺序：启动（`START_WORKING`）→ 流程开始（`PROCESS_BEGINS`）→ 人工放好瓶子 → 上料完成（`ZHANTING_LOADED`）。提前发送上料完成会被拒绝，避免确认信号遗留到下一轮。

暂停、恢复、结束分别使用 `PROCESS_PAUSED`、`PROCESS_RESUMED`、`PROCESS_ENDED`。暂停在节点边界生效；已经发送的 HTTP/机器人动作由该接口完成或超时后退出。结束会停止后续调度和 PLC 等待，并清理本轮握手输出；它不代替设备急停。旧动作尚未退出时不能重开新一轮，重置也会等待退出。

顶部「演练」使用 mock 机器人，自动模拟开始和上料确认；HTTP/PLC 节点不访问现场，即使 URL 和字节映射未填写也能演练。

## 机械臂 HTTP 约定

`zhanting.arm_url` 现为 `http://127.0.0.1:18080/`。运行控制打开 Mock 后，本机 `mock_http_service` 会在这个地址上把任意 POST 回成 HTTP 200 与 `{"success":true}`。接真机时改成机械臂的完整 http 地址。节点发送 POST JSON：

```json
{"task":"load_bottle_to_opener","extra_params":{}}
```

| 配置中的键 | 默认 task | 动作 |
|---|---|---|
| `load_to_opener` | `load_bottle_to_opener` | 上料位取瓶到开盖器 |
| `opener_to_robot` | `take_bottle_to_robot` | 开盖器取瓶到机器人前方 |
| `bottle_to_closer` | `load_bottle_to_closer` | 桌面取试剂瓶到关盖器 |
| `closer_to_loading` | `return_bottle_to_loading` | 关盖器取瓶回上料位 |

本项目约定：动作真正完成后返回 HTTP 200 和 `{"success":true}`。`arm_response_mode=completed`、`arm_success_field=success`、`arm_success_value=true` 可按对接协议修改。通用 HTTP 节点拒绝 HTTP 202、非 JSON、缺少完成字段或完成字段不匹配；仅接收/排队响应不能直接推进，若机械臂最终采用异步协议，需在图上另加完成查询/通知等待。

## PLC S7 地址

已填现场 IP `192.168.217.40`，端口 102，所有点使用 DB2。`rack=0`、`slot=1` 是待现场核对的连接参数。依赖 `python-snap7>=2.0,<3`（兼容项目 Python 3.9，已加入 Docker 和 environment.yml）。S7 访问使用显式 DB 读写，API 见 [python-snap7 官方文档](https://python-snap7.readthedocs.io/en/2.0.0/API/client.html)。

点表里的 40000～40018 和 0～8 沿用新乡 Modbus 的编号习惯，只是逻辑点号，**不参与寻址也不推导字节偏移**；S7 实际按现场 TIA 工程 DB2 的结构定位，每个点的 `db` / `byte`（BOOL 再加 `bit`）都已按下表填实。INT 占两个字节，BOOL 占一位。配置里留 `null` 会在真实运行前被拒绝启动，演练不受影响。

DB2 分两段：`WMS_PLC` 是机器人写给 PLC 的 9 个 BOOL，连续排在 0.0～1.0；`PLC_WMS` 是 PLC 给机器人的 19 个 INT，从字节 2 起每点两字节排到 38。

| 点名 | 逻辑点号 | DB2 偏移 | 类型/权限 |
|---|---|---|---|
| `open_state` / `close_state` / `detect_state` | 40000 / 40001 / 40002 | 2 / 4 / 6 | INT，只读 |
| `device_status` / `open_flow` / `detect_flow` | 40003 / 40004 / 40005 | 8 / 10 / 12 | INT，只读 |
| `qr_1`～`qr_10` | 40006～40015 | 14～32 | INT，只读 |
| `detect_lift_servo` / `detect_turntable_servo` / `open_lift_servo` | 40016 / 40017 / 40018 | 34 / 36 / 38 | INT，只读 |
| `detect_dispense` / `detect_start` / `detect_pick` / `detect_finish` | 0 / 1 / 2 / 3 | 0.0 / 0.1 / 0.2 / 0.3 | BOOL，可写 |
| `open_start` / `open_finish` / `close_start` / `close_finish` | 4 / 5 / 6 / 7 | 0.4 / 0.5 / 0.6 / 0.7 | BOOL，可写 |
| `device_reset` | 8 | 1.0 | BOOL，可写 |

9 个输出挤在同两个字节里，所以置位走读改写，只动自己那一位，不碰邻位。`tests/test_common_plc_s7.py` 用假传输层把这套映射钉住了：写 `open_start` 只置 0.4、读 `detect_state` 只取字节 6、二维码一次读字节 14 起 20 字节。

二维码占 10 个连续字，必须一次读完——逐点读会在中途被 PLC 刷新，拼出新旧混合的码。图上用一个「PLC 读取」节点，点名 `qr_1`、连续读取点数 10，结果存进流程变量 `qr_codes`（10 个整数的列表），后续节点用 `{{qr_codes}}` 取用。默认读取时机是检测完成（状态 4）之后、倒第一杯废液之前；若现场是上料时就出码，把该节点拖到前面即可。`qr_codes` 目前只存不发，要上报请在图上接 HTTP 节点。

展厅不写输入状态。握手等待状态满足后置位输出，主动轮询完成状态，最后复位；超时、停止、异常也尝试复位本次输出。多个并行节点共享同一连接锁，BOOL 读改写保留同字节的其他位。

| 握手 | 置位前状态 | 等待完成状态 |
|---|---:|---:|
| 开盖启动 / 关盖启动 | 1 | 3 |
| 开盖取料完成 / 关盖取料完成 | 3 | 表格未定义后续状态，默认保持 0.2 秒脉冲后复位，可在图上调整 |
| 检测放料移位 | 1 | 2 |
| 检测启动 | 2 | 4 |
| 检测取料移位 | 4 | **6**（5 仍在移位中） |
| 检测取料完成 | 6 | 1（约定回就绪，需与 PLC 程序确认） |

设备复位输出只作为可选通用节点使用，不在开始或结束时自动触发。

## 上线前要现场核对的几项

地址映射和动作顺序已经按图纸填实，下面这些是图纸上没写、按惯例先填的值，接通设备后逐条核对，改动都只在配置或画布上，不用改代码。

| 项 | 当前取值 | 怎么核对 |
|---|---|---|
| S7 连接槽位 | `rack=0`、`slot=1` | TIA 工程里的硬件组态 |
| 开盖/关盖取料完成的完成状态 | 未配置，发 0.2 秒脉冲后复位 | 若 PLC 有确认状态，在握手节点填「完成状态」改成轮询 |
| 检测取料完成的完成状态 | `1`（按回就绪理解） | 与 PLC 程序确认取料完成后状态机回到几 |
| 二维码读取时机 | 检测完成后、倒第一杯废液前 | 若上料时就出码，把该节点往前拖 |
| 机械臂地址与动作名 | `http://127.0.0.1:18080/`（运行控制 Mock 的 HTTP 模拟） | 接真机时改 `arm_url`，动作名按机械臂侧实际接口改 |
| 机械臂完成语义 | HTTP 200 且 `{"success":true}` | 若改成异步协议，需要在图上补完成查询或等待节点 |

## 与新乡共用的 PLC 模块

`plc_controller/common_plc.py` 提供 S7、Modbus TCP 客户端和原新乡 Modbus 服务端适配；`core/plc_nodes.py` 提供 `plc_read`、`plc_write`、`plc_wait`、`plc_handshake`。协议和点位放配置中，图节点只引用点名，所以同一张图换协议只改配置。「PLC 读取」的连续读取在四种协议下都可用：S7 一次 `db_read`、Modbus 一次读多个保持寄存器，都是单请求，保证成组数据的一致性。

新乡仍保留 `xinxiang_detect` 和原有协议时序，包括放料前写本机检测状态为 1。新乡新建图可使用通用 PLC 节点，共享现有服务端，不会改为主动连接现场 PLC；若用通用节点复现原放料流程，应先 `plc_write detect_state=1` 再握手。新乡取料等待 5，展厅等待 6，两项目分别配置。

通用 PLC 实机路径 `enabled=false` 会失败；原 `xinxiang_detect` 禁用时跳过的行为保留。显式 `protocol=memory` 仅用于开发模拟。
