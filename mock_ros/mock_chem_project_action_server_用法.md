# mock_chem_project_action_server 用法说明

模拟机器人侧的 **chem_project 任务节点**，同时提供：

| 协议 | 默认端点 | 调用方（robot_connect） |
|---|---|---|
| **ROS Action**（异步 topic） | `/chem_project/*` | `hardware.task_utils.send_task_action()` |
| **ROS Service**（同步） | `/chem_project_service` | `robot.send_service_request_task()` |

KAIAO 项目将 Action 前缀改为 `/robot_task`（见下文 §4）。

两种协议共用同一套任务调度逻辑（`_execute_task`），区别仅在于 Action 有 feedback 流、Service 无 feedback。

---

## 1. 消息契约（与 rosbridge 一致）

### Action（`.action` 文件）

```
# ==================== Goal ====================
navi_types/RobotTaskTypes robot_task_types
string task
string area
string extra_params
---
# ==================== Result ====================
bool success
string error_msg
string return_params
---
# ==================== Feedback ====================
string status
string current_params
```

### Action topic 派生规则

由 `--action-base`（默认 `/chem_project`）自动派生 5 个 topic：

| Topic | 消息类型 |
|---|---|
| `<base>/goal` | `chem_project_msgs/ChemProjectActionGoal` |
| `<base>/feedback` | `chem_project_msgs/ChemProjectActionFeedback` |
| `<base>/result` | `chem_project_msgs/ChemProjectActionResult` |
| `<base>/cancel` | `actionlib_msgs/GoalID` |
| `<base>/status` | `actionlib_msgs/GoalStatusArray` |

### Service（`.srv` 文件）

```
# Request
navi_types/RobotTaskTypes robot_task_types
string task
string area
string extra_params
---
# Response
bool   success
string error_msg
string return_params
```

---

## 2. 三种运行模式

```
                    ┌─────────────────────────────────────┐
  main.py           │  mock_chem_project_action_server     │
  (robot_connect)   │                                     │
       │            │  ros-node      → rospy Action/Service │
       │  WebSocket │  bridge-client → 连外部 rosbridge    │
       └───────────►│  embedded-server → 自建假 rosbridge  │
                    └─────────────────────────────────────┘
```

| 模式 | 命令 | 适用场景 |
|---|---|---|
| **ros-node**（默认） | `python3 mock_chem_project_action_server.py` | 有 ROS 环境（Docker `mock_ros/` 容器内） |
| **bridge-client** | `--mode bridge-client --rosbridge-url ws://IP:9090` | 无 ROS，接入已有 rosbridge |
| **embedded-server** | `--mode embedded-server --port 19090` | 本地轻量联调，main.py 直连 mock |

### 2.1 embedded-server（本地最快）

无需 ROS，mock 自己监听 WebSocket，充当 rosbridge：

```bash
cd /path/to/robot_connect

# 默认 /chem_project（ATC 等旧项目）
python3 mock_ros/mock_chem_project_action_server.py \
  --mode embedded-server \
  --port 19090

# KAIAO：action 前缀 /robot_task
python3 mock_ros/mock_chem_project_action_server.py \
  --mode embedded-server \
  --port 19090 \
  --action-base /robot_task
```

`robot_config.json` 机器人连接：

```json
{
  "robots": {
    "robot_a": {
      "host": "127.0.0.1",
      "port": 19090
    }
  }
}
```

### 2.2 ros-node（Docker 完整仿真）

见 `mock_ros/README.md`：

```bash
cd mock_ros/
docker compose up -d
# rosbridge 在 ws://<宿主机IP>:9090
```

容器内 mock 以 **ros-node** 模式运行，经 rosbridge 对外暴露。

### 2.3 bridge-client（挂到真机 rosbridge）

mock 作为客户端连入机器人 rosbridge，在 ROS 网络侧注册 Action/Service：

```bash
python3 mock_ros/mock_chem_project_action_server.py \
  --mode bridge-client \
  --rosbridge-url ws://192.168.1.100:9090 \
  --action-base /robot_task
```

---

## 3. 命令行参数

```bash
python3 mock_ros/mock_chem_project_action_server.py --help
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--mode` | `ros-node` | `ros-node` / `bridge-client` / `embedded-server` |
| `--action-base` | `/chem_project` | Action 基名，派生 5 个 topic |
| `--service-name` | `/chem_project_service` | ROS Service 名 |
| `--action-pkg` | `chem_project_msgs` | 消息包名 |
| `--action-name` | `ChemProject` | 动作名（派生 `ChemProjectActionGoal` 等） |
| `--port` | `19090` | embedded-server 监听端口 |
| `--rosbridge-url` | `ws://127.0.0.1:9090` | bridge-client 目标 |
| `--feedback-steps` | `3` | 默认 task 的 feedback 步数 |
| `--step-interval` | `1.0` | 每步间隔（秒） |
| `--long-duration` | `30.0` | `task=long` 持续时间 |
| `--quiet` | — | 减少日志 |

---

## 4. 与 KAIAO 对齐

KAIAO 代码中 `KAIAO_TASK_ACTION_SPEC`（`programs/KAIAO/constants.py`）：

```
goal_topic     = /robot_task/goal
feedback_topic = /robot_task/feedback
result_topic   = /robot_task/result
cancel_topic   = /robot_task/cancel
status_topic   = /robot_task/status
goal_msg_type  = chem_project_msgs/ChemProjectActionGoal
```

启动 mock 时加 `--action-base /robot_task` 即可。  
Service 名若真机不同，另加 `--service-name /robot_task_service`（按现场配置）。

---

## 5. 测试用 task 名称

mock 根据 `goal.task`（或 service 的 `args.task`）分发行为：

| task | 行为 | feedback |
|---|---|---|
| `success_fast` | 立即成功 | 无 |
| `success_with_steps` | 默认行为 | N 步后成功（`--feedback-steps`，默认 3） |
| `fail` | 立即失败，`error_msg` 带诊断 | 无 |
| `fail_with_steps` | 2 步 feedback 后失败 | 有 |
| `long` | 运行 `--long-duration` 秒 | 持续 feedback，可测 cancel/超时 |
| **其他任意名**（如 `pick_up_box`） | 等同 `success_with_steps` | 有 |

示例：用 `pick_up_box` 测 KAIAO 完整链路，mock 会 echo 回 `extra_params`：

```json
// goal.goal 内层
{
  "robot_task_types": { "value": 0 },
  "task": "pick_up_box",
  "area": "shelf",
  "extra_params": "{\"shelf_level\":2}"
}
```

成功时 `result.return_params` 示例：

```json
{
  "echo_task": "pick_up_box",
  "echo_area": "shelf",
  "echo_extra_params": "{\"shelf_level\":2}",
  "steps": 3
}
```

---

## 6. 手动 WebSocket 测试

### 6.1 Action：发布 Goal

连接 `ws://127.0.0.1:19090`（embedded-server），先订阅 feedback/result，再 publish goal：

```json
{"op":"subscribe","topic":"/robot_task/feedback","type":"chem_project_msgs/ChemProjectActionFeedback"}
```

```json
{"op":"subscribe","topic":"/robot_task/result","type":"chem_project_msgs/ChemProjectActionResult"}
```

```json
{
  "op": "publish",
  "topic": "/robot_task/goal",
  "msg": {
    "header": {"seq": 0, "stamp": {"secs": 0, "nsecs": 0}, "frame_id": ""},
    "goal_id": {"stamp": {"secs": 0, "nsecs": 0}, "id": "test-goal-001"},
    "goal": {
      "robot_task_types": {"value": 0},
      "task": "success_with_steps",
      "area": "shelf",
      "extra_params": ""
    }
  }
}
```

预期收到 3 条 feedback + 1 条 result（`result.success=true`）。

### 6.2 Action：取消

```json
{
  "op": "publish",
  "topic": "/robot_task/cancel",
  "msg": {"stamp": {"secs": 0, "nsecs": 0}, "id": "test-goal-001"}
}
```

### 6.3 Service：同步调用

```json
{
  "op": "call_service",
  "id": "svc-001",
  "service": "/chem_project_service",
  "args": {
    "robot_task_types": {"value": 0},
    "task": "success_fast",
    "area": "",
    "extra_params": ""
  }
}
```

响应：

```json
{
  "op": "service_response",
  "id": "svc-001",
  "service": "/chem_project_service",
  "result": true,
  "values": {
    "success": true,
    "error_msg": "",
    "return_params": "{\"area\":\"\",\"note\":\"fast\"}"
  }
}
```

---

## 7. 与 robot_connect 联调流程

```
1. 启动 mock（embedded-server 或 Docker）
2. robot_config.json → robots.robot_a.host/port 指向 mock
3. active_project = KAIAO（或对应项目）
4. 启动 main.py
5. START_WORKING 激活系统
6. 发送 HTTP 命令（如 test_commands/KAIAO_PICK_BOX_TO_SP_command.json）
7. 观察 mock 终端日志：📥 Goal → 📶 feedback → 🏁 result
```

代码路径：

- KAIAO 任务：`programs/KAIAO/KAIAO.py` → `send_task_action(..., spec=KAIAO_TASK_ACTION_SPEC)`
- 通用封装：`hardware/task_utils.py` → 组装 Goal 四字段并调用 `hardware/action_utils.send_action`

---

## 8. 与 mock_rosbridge 的区别

| | `mock_ros/`（本文件） | `mock_rosbridge/` |
|---|---|---|
| 依赖 | 可选 ROS（三种模式） | 纯 Python |
| 模拟内容 | chem_project **Service + Action** | 更轻量的 rosbridge 仿真 |
| 典型用途 | 完整任务 Action 联调、Docker 仿真 | CI、快速冒烟 |

两者可并行运行在不同端口。

---

## 9. 常见问题

**Q: main.py 发了 goal 但 mock 没反应？**  
检查 `--action-base` 是否与项目 `ActionSpec` 一致（KAIAO 用 `/robot_task`）。

**Q: feedback 有但 result 一直不来？**  
看 mock 日志是否异常；`task=long` 需等满 `--long-duration` 或发 cancel。

**Q: Service 和 Action 测哪个？**  
KAIAO 已切 Action（`send_task_action`）；旧 ATC 部分仍用 Service。mock 两种都支持。

**Q: ros-node 报找不到 chem_project_msgs？**  
mock 会降级为 stub 类型继续运行；完整二进制兼容需安装对应 ROS 包。
