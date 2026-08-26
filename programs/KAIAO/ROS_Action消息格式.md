# KAIAO ROS Action 消息格式参考

KAIAO 通过 **rosbridge WebSocket** 发布/订阅标准 **actionlib topic 协议**。  
机器人任务使用 `/robot_task/*`；导航使用 `/zj_humanoid/navigation/navigation/*`。

代码中的 Spec 定义见 `programs/KAIAO/constants.py` → `KAIAO_TASK_ACTION_SPEC`（引用 `infrastructure.constants`）。

---

## 1. 通用 actionlib 协议

所有 Action 均遵循同一套 topic 收发模式（与 `hardware/action_utils.py` 一致）：

| 方向 | Topic | 消息类型 |
|---|---|---|
| 发布 Goal | `<base>/goal` | `<pkg>/<Name>ActionGoal` |
| 订阅 Feedback | `<base>/feedback` | `<pkg>/<Name>ActionFeedback` |
| 订阅 Result | `<base>/result` | `<pkg>/<Name>ActionResult` |
| 发布 Cancel | `<base>/cancel` | `actionlib_msgs/GoalID` |
| 订阅 Status | `<base>/status` | `actionlib_msgs/GoalStatusArray` |

### 1.1 发布 Goal（外层包装）

发布到 `goal_topic` 的完整消息结构：

```json
{
  "header": {
    "seq": 0,
    "stamp": { "secs": 1696000000, "nsecs": 123456789 },
    "frame_id": ""
  },
  "goal_id": {
    "stamp": { "secs": 1696000000, "nsecs": 123456789 },
    "id": "robot_connect-1696000000123-a1b2c3d4"
  },
  "goal": { }
}
```

- `goal` 字段为各 Action 自定义的 **内层 Goal**（见下文）。
- `goal_id.id` 用于过滤 feedback/result，避免多任务串扰。

### 1.2 收到 Feedback

```json
{
  "header": { "seq": 1, "stamp": { "secs": 1696000001, "nsecs": 0 }, "frame_id": "" },
  "status": {
    "goal_id": { "stamp": { "secs": 1696000000, "nsecs": 123456789 }, "id": "robot_connect-..." },
    "status": 1
  },
  "feedback": { }
}
```

`status.status` 为 actionlib GoalStatus：`0=PENDING, 1=ACTIVE, 3=SUCCEEDED, 4=ABORTED, ...`

### 1.3 收到 Result

```json
{
  "header": { "seq": 2, "stamp": { "secs": 1696000010, "nsecs": 0 }, "frame_id": "" },
  "status": {
    "goal_id": { "stamp": { "secs": 1696000000, "nsecs": 123456789 }, "id": "robot_connect-..." },
    "status": 3
  },
  "result": { }
}
```

### 1.4 取消 Goal

发布到 `cancel_topic`：

```json
{
  "stamp": { "secs": 1696000005, "nsecs": 0 },
  "id": "robot_connect-1696000000123-a1b2c3d4"
}
```

`id` 为空字符串时表示取消全部 Goal。

---

## 2. 机器人任务 Action（`/robot_task/*`）

### 2.1 Topic 与消息类型

| 字段 | 值 |
|---|---|
| `goal_topic` | `/robot_task/goal` |
| `feedback_topic` | `/robot_task/feedback` |
| `result_topic` | `/robot_task/result` |
| `cancel_topic` | `/robot_task/cancel` |
| `status_topic` | `/robot_task/status` |
| `goal_msg_type` | `navi_types/RobotActionActionGoal` |
| `feedback_msg_type` | `navi_types/RobotActionActionFeedback` |
| `result_msg_type` | `navi_types/RobotActionActionResult` |

### 2.2 `.action` 定义（rosbridge 契约）

与真机 / mock 一致的 Action 三段定义：

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

经 rosbridge 传输时，上述三段分别落在：

| 协议层 | 消息类型 | 承载字段 |
|---|---|---|
| 发布 Goal | `RobotActionActionGoal` | 外层 `goal` = Goal 段四字段 |
| 订阅 Feedback | `RobotActionActionFeedback` | 外层 `feedback` = Feedback 段两字段 |
| 订阅 Result | `RobotActionActionResult` | 外层 `result` = Result 段三字段 |

### 2.3 内层 Goal（`goal` 字段内容）

```json
{
  "robot_task_types": { "type": 0 },
  "task": "pick_up_box",
  "area": "shelf",
  "extra_params": "{\"shelf_level\":2}"
}
```

| 字段 | ROS 类型 | 说明 |
|---|---|---|
| `robot_task_types` | `navi_types/RobotTaskTypes` | rosbridge 中为 `{"type": uint8}`，KAIAO 默认 `0` |
| `task` | `string` | 任务名：`pick_up_box` / `put_down_box` / `pick_up_component` / `put_down_component` |
| `area` | `string` | `"agv_car"` 或 `"shelf"` |
| `extra_params` | `string` | **必须是 JSON 字符串**；箱子任务仅含 `shelf_level`（HTTP `shelf_num[1]` row） |

### 2.4 内层 Feedback（`feedback` 字段内容）

```json
{
  "status": "step_1_of_3",
  "current_params": "{\"progress\":0.33}"
}
```

| 字段 | ROS 类型 | 说明 |
|---|---|---|
| `status` | `string` | 业务状态文本，如 `step_1_of_3`、`FINISHED`、`FAILED` |
| `current_params` | `string` | 附加参数，通常为 JSON 字符串 |

### 2.5 内层 Result（`result` 字段内容）

```json
{
  "success": true,
  "error_msg": "",
  "return_params": "{\"echo_task\":\"pick_up_box\"}"
}
```

| 字段 | ROS 类型 | 说明 |
|---|---|---|
| `success` | `bool` | **业务成败判定依据**（`send_task_action` 以此为准） |
| `error_msg` | `string` | 失败原因 |
| `return_params` | `string` | 返回载荷，通常为 JSON 字符串 |

> actionlib 外层 `status.status`（3=SUCCEEDED / 4=ABORTED）仅作参考；本契约以 `result.success` 为准。

---

## 3. 各 task 的 Goal 示例

以下均为发布到 `/robot_task/goal` 时 `goal` 字段的内容。  
完整消息需套上 **§1.1 外层包装**。

### 3.1 `pick_up_box` — 搬起箱子

**场景**：`PICK_BOX_TO_SP` 取箱；`PICK_UP_BOX` 单步抓箱。

```json
{
  "robot_task_types": { "type": 0 },
  "task": "pick_up_box",
  "area": "agv_car",
  "extra_params": "{\"shelf_level\":2}"
}
```

| 参数 | 说明 |
|---|---|
| `area` | HTTP `shelf_type`：`"agv_car"` 或 `"shelf"` |
| `extra_params.shelf_level` | 垂直层 0–3（HTTP `shelf_num[1]` row，从下往上） |

---

### 3.2 `put_down_box` — 放下箱子

**场景**：`PICK_BOX_TO_SP` 放箱；`PUT_DOWN_BOX` 单步放箱。

```json
{
  "robot_task_types": { "type": 0 },
  "task": "put_down_box",
  "area": "shelf",
  "extra_params": "{\"shelf_level\":2}"
}
```

| 参数 | 说明 |
|---|---|
| `area` | HTTP `shelf_type`：`"agv_car"` 或 `"shelf"` |
| `extra_params.shelf_level` | 同 `pick_up_box`，为目标层（row） |

---

### 3.3 `pick_up_component` — 抓取零件

**场景**：`PICK_COMPONENT_TO_SP` 在闪攀小车（AGV）侧取件。

```json
{
  "robot_task_types": { "type": 0 },
  "task": "pick_up_component",
  "area": "agv_car",
  "extra_params": "{\"shelf_level\":1,\"type\":\"black_screw\",\"number\":1}"
}
```

| 参数 | 说明 |
|---|---|
| `area` | 固定 `"agv_car"`（闪攀小车侧） |
| `extra_params.type` | 零件类型，见下表 |
| `extra_params.number` | 固定 `1`（每次 Goal 搬 1 个） |
| `extra_params.shelf_level` | 固定 `1`（固件暂不读取） |

**零件类型 `type` 枚举：**

| 值 |
|---|
| `black_screw` |
| `black_tube` |
| `black_square` |
| `black_joystick` |
| `black_fan` |

---

### 3.4 `put_down_component` — 放下零件

**场景**：`PICK_COMPONENT_TO_SP` 在播种墙格位放下。

```json
{
  "robot_task_types": { "type": 0 },
  "task": "put_down_component",
  "area": "shelf",
  "extra_params": "{\"shelf_level\":2,\"type\":\"black_screw\",\"number\":1}"
}
```

| 参数 | 说明 |
|---|---|
| `area` | 放件导航点名（如 `shelf0_0`，由 `box_target_area` 的 `编号 + column` 推导） |
| `extra_params.shelf_level` | 来自 HTTP `target.box_target_area[1]`（row，0–3） |
| `extra_params.type` | 与取件时 `component_type` 一致 |
| `extra_params.number` | 固定 `1` |

---

## 4. HTTP 大任务 → ROS Goal 对照

### PICK_BOX_TO_SP

| 步骤 | task | area | extra_params 来源 |
|---|---|---|---|
| 取箱 | `pick_up_box` | `shelf_type`（`agv_car`/`shelf`） | `shelf_num[1]`（row）→ `shelf_level` |
| 放箱 | `put_down_box` | `shelf_type`（`agv_car`/`shelf`） | `shelf_num[1]`（row）→ `shelf_level` |

前后各有一次 **导航 Action**（§5），导航点位由 `shelf_type + shelf_num[0]/[2]` 推导（如 `agv_car0_0`、`shelf0_1`）。

### PICK_COMPONENT_TO_SP

| 步骤 | task | area | extra_params |
|---|---|---|---|
| 取件（循环） | `pick_up_component` | `component_car` | `type` + `number:1` |
| 放件（循环） | `put_down_component` | 放件导航点（如 `shelf0_0`） | `shelf_level`（row）+ `type` + `number:1` |

取件前导航至 HTTP `box_initial_area`（如 `"point1"`）；放件前导航至 `shelf{编号}_{column}`（来自 `box_target_area[0]/[2]`）。

---

## 5. 导航 Action（`/zj_humanoid/navigation/navigation/*`）

KAIAO 的 `NAVIGATION` 及大任务中的移动步骤使用导航 Action，与机器人任务 Action **独立**。

| 字段 | 值 |
|---|---|
| `goal_topic` | `/zj_humanoid/navigation/navigation/goal` |
| `feedback_topic` | `/zj_humanoid/navigation/navigation/feedback` |
| `result_topic` | `/zj_humanoid/navigation/navigation/result` |
| `cancel_topic` | `/zj_humanoid/navigation/navigation/cancel` |
| `status_topic` | `/zj_humanoid/navigation/navigation/status` |
| `goal_msg_type` | `navigation/NavigationActionGoal` |

### 5.1 内层 Goal 示例（导航至 `point1`）

路点坐标来自 `robot_config.json` → `navigation_poses.KAIAO.point1`。

```json
{
  "header": {
    "seq": 0,
    "stamp": { "secs": 1696000000, "nsecs": 0 },
    "frame_id": "map"
  },
  "task_type": { "value": 0 },
  "translation": {
    "enable": true,
    "heading": 0.08
  },
  "waypoints": [
    {
      "pose": {
        "position": { "x": 0.0, "y": 0.0, "z": 0.0 },
        "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 }
      },
      "distance_tolerance": 0.08,
      "heading_tolerance": 0.08
    }
  ]
}
```

多段路径时 `waypoints` 为数组，按顺序经过各点，最后一点为目标。

### 5.2 Feedback / Result

**Feedback 内层：**

```json
{
  "header": { "seq": 1, "stamp": { "secs": 1696000001, "nsecs": 0 }, "frame_id": "map" },
  "state": { "value": 2 },
  "faults": []
}
```

`state.value` 对应 `NavigationState`：`6=SUCCEEDED`, `7=FAILED`, `9=ABORTED`, ...

**Result 内层：**

```json
{
  "header": { "seq": 2, "stamp": { "secs": 1696000010, "nsecs": 0 }, "frame_id": "map" },
  "duration": 12.5,
  "distance_deviation": 0.02,
  "heading_deviation": 0.01,
  "state": { "value": 6 },
  "causes": []
}
```

---

## 6. 代码调用入口

```python
from hardware.task_utils import send_task_action
from programs.KAIAO.constants import KAIAO_TASK_ACTION_SPEC, KAIAOTask, KAIAOArea

# 示例：pick_up_box
result = send_task_action(
    robot,
    task=KAIAOTask.PICK_UP_BOX,
    area=KAIAOArea.SHELF,
    extra_params={"shelf_level": 2},
    spec=KAIAO_TASK_ACTION_SPEC,
    timeout=120,
)
if not result:
    print(result.error_msg)
```

导航：

```python
from hardware.navigation_utils import build_navigation_goal, send_navigation_action
from programs.KAIAO.constants import get_nav_pose, KAIAONavTolerance

goal = build_navigation_goal(
    get_nav_pose("point1"),
    distance_tolerance=KAIAONavTolerance.DISTANCE,
    heading_tolerance=KAIAONavTolerance.HEADING,
    translation_enable=True,
    translation_heading=KAIAONavTolerance.TRANSLATION_HEADING,
)
nav_result = send_navigation_action(robot, goal, timeout=180)
```

---

## 7. 本地 Mock 测试

用法详见 **`mock_ros/mock_chem_project_action_server_用法.md`**。

KAIAO 联调时将 action 前缀改为 `/robot_task`：

```bash
python3 mock_ros/mock_chem_project_action_server.py \
  --mode embedded-server \
  --port 19090 \
  --action-base /robot_task
```

`robot_config.json` 中机器人地址指向 `ws://127.0.0.1:19090`。

测试用 task 名：`success_fast`、`success_with_steps`、`fail`、`long` 等（见 mock 文件头注释）。
