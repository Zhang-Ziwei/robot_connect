# `mock_rosbridge/` — 模拟 ROS Bridge / ROS Action 服务器

本文件夹收集了若干用于**本地/桌面端/ARM 设备**上模拟机器人 `rosbridge` 行为的独立小程序，以便在没有真机的情况下联调 `robot_connect`（主程序 `main.py`）。

所有 mock 程序都是单文件、标准库 + `websockets` 依赖，可独立启动；也可以统一打包成一个 ARM 镜像（见末尾 [Docker 部署](#docker-部署)）。

---

## 文件总览

| 文件 | 类型 | 说明 |
|---|---|---|
| `mock_rosbridge_server.py` | **Mock server (v1)** | 最早版本，模拟老协议的 `call_service`，内置导航状态发布。适合对老式 service 调用做黑盒测试。 |
| `mock_navigation_action_server.py` | **Mock server** | 使用自造 `call_action` / `action_feedback` / `action_result` 操作码，模拟导航 Action。适合测试历史版本的 navigation 行为。 |
| **`mock_chem_project_action_server.py`** | **Mock Action 节点 (最新)** | **默认以真实 ROS 节点身份运行**（`rospy` + `actionlib`）；机器人 rosbridge 作为转接桥，把本节点的 ROS topic 桥接给 main.py。无 ROS 时可加 `--mode bridge-client` 退回 WebSocket 模拟模式。支持新版 `{success, error_msg, return_params}` 结果契约。见 [详细说明](#mock_chem_project_action_serverpy新) |
| `mock_robot_controller.py` | 辅助类 | `MockRobotController`，把 `RobotController` 接口 stub 成本地打印，用于主程序在"不连任何 rosbridge"时做纯逻辑冒烟。 |
| `test_websocket_client.py` | 测试客户端 | 通过 WebSocket 连 `robot_connect` 的 WebSocket 服务端（不是连 rosbridge），用来对业务命令 JSON 文件做回放测试。 |
| `test_websocket_concurrent.py` | 并发测试 | 同上，多个客户端并发连 WebSocket 服务端，测并发与注册。 |
| `Dockerfile_arm` | Docker 构建脚本 | **ARM64** 镜像：基于 `condaforge/miniforge3`，conda 环境 `rosbridge`，内置三个 mock server，默认 `CMD=/bin/bash`（由运行方选择启动哪一个）。 |
| `docker-compose.yml` | compose 配置 | 一键构建 + 启动（默认只暴露 `mock_rosbridge_server.py` 的 9091 端口，如需其他端口自行修改）。 |
| `requirements.txt` | 依赖 | `websockets>=10.0` |
| `run.sh` | 快捷脚本 | `python3 mock_rosbridge_server.py`（老 mock 的快捷启动） |
| `README.md` | 本文件 | 文档 |

---

## mock_chem_project_action_server.py（新）

### 背景与架构

本 mock 同时模拟两种 ROS 通信协议，供 `main.py` 里走不同代码路径时分别使用：

| 协议 | ROS 端点 | 契约 | main.py 调用路径 |
|---|---|---|---|
| **ROS Service**（同步） | `/chem_project_service` | 见下方 srv | `robot.send_service_request_task(...)` |
| **ROS Action**（异步 topic 流） | `/chem_project/*` | 见下方 action | `hardware.task_utils.send_task_action(...)` |

正确架构（本程序角色是 ROS 节点，rosbridge 负责向 main.py 桥接）：

```text
本 mock（ROS 节点：service server + action server）
        │
        │  ROS 原生 topic / service
        ▼
机器人自带 rosbridge（作为"转接桥"把 ROS ↔ WebSocket 双向桥接）
        │
        │  ws://机器人IP:9090
        ▼
main.py / robot_connect
```

### 消息契约

**srv 文件（`/chem_project_service`）**

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

**action 文件（`/chem_project/*`）**

```
# Goal
navi_types/RobotTaskTypes robot_task_types
string task
string area
string extra_params
---
# Result
bool   success
string error_msg
string return_params
---
# Feedback
string status
string current_params
```

### 运行模式

| 模式 | 默认 | 说明 |
|---|---|---|
| `ros-node` | ✅ 默认 | `rospy` + `actionlib`，同时启动 Service Server 和 Action Server，需 source ROS 环境 |
| `bridge-client` | — | 无 ROS 时通过 rosbridge WebSocket 协议模拟（`advertise_service` + action topic 订阅/发布） |
| `embedded-server` | — | 历史兼容：本程序自己伪装成 rosbridge server，main.py 直连 |

### task 分发行为（service / action 共用同一逻辑）

| `task` 值 | 行为 |
|---|---|
| `success_fast` | 立即成功；`return_params='{"area":..,"note":"fast"}'` |
| `success_with_steps`（默认） | N 条 feedback 后成功（action 有 feedback 流，service 只在日志显示） |
| `fail` | 立即 `ABORTED`，`error_msg` 带 `area` 上下文 |
| `fail_with_steps` | 2 步后失败 |
| `long` | 持续 30s，用于测 cancel / 超时 |
| 其他（如空串） | 走 `success_with_steps` 默认行为 |

### CLI

```bash
# ── ros-node 模式（推荐，需 source ROS）──────────────────────────────────
source /opt/ros/noetic/setup.bash
python mock_chem_project_action_server.py

# 自定义端点
python mock_chem_project_action_server.py \
    --action-base /zj_humanoid/chem_project \
    --service-name /zj_humanoid/chem_project_service \
    --action-pkg  chem_project_msgs \
    --action-name ChemProject

# ── bridge-client 模式（无 ROS，只需 websockets）───────────────────────
python mock_chem_project_action_server.py \
    --mode bridge-client \
    --rosbridge-url ws://机器人IP:9090

# ── embedded-server 模式（本地离线）──────────────────────────────────────
python mock_chem_project_action_server.py --mode embedded-server --port 19090
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--mode` | `ros-node` | 运行模式 |
| `--action-base` | `/chem_project` | Action 基名，派生 5 个 topic |
| `--service-name` | `/chem_project_service` | ROS Service 名称 |
| `--action-pkg`  | `chem_project_msgs` | 消息包名 |
| `--action-name` | `ChemProject` | 动作/服务名（用于构造 msg type） |
| `--rosbridge-url` | `ws://127.0.0.1:9090` | `bridge-client` 模式连接地址 |
| `--reconnect-interval` | `3.0` | `bridge-client` 断线重连间隔秒 |
| `--host` / `--port` | `0.0.0.0` / `19090` | 仅 `embedded-server` 模式 |
| `--feedback-steps` | `3` | 默认 task 的 feedback 条数 |
| `--step-interval` | `1.0` | 每条 feedback 间隔秒 |
| `--long-duration` | `30.0` | task=long 的总时长 |
| `--status-rate` | `5.0` | status 广播频率 Hz（非 ros-node 模式） |
| `--quiet` | 否 | 关闭详细日志 |

### 工作流程

**Service（同步）**

```
main.py                    rosbridge（转接桥）         mock（ROS Service Server）
  │                              │                              │
  │ call_service /chem_project_  │                              │ rospy.Service 已注册
  │   service args={task,area,…} │                              │
  │─────────────────────────────►│── 转发 call_service ─────────►│
  │                              │                              │ 执行 task（同步阻塞）
  │                              │◄── service_response ─────────│
  │◄─────────────────────────────│ {success, error_msg, …}      │
```

**Action（异步 topic 流）**

```
main.py                    rosbridge（转接桥）         mock（ROS Action Server）
  │                              │                              │
  │ publish /chem_project/goal   │                              │
  │─────────────────────────────►│──── ROS topic ──────────────►│ 创建 goal
  │                              │                              │ 执行 task（异步）
  │                              │◄── publish /feedback ────────│
  │◄─────────────────────────────│                              │
  │                              │◄── publish /result ──────────│
  │◄─────────────────────────────│                              │
```

### GoalStatus 值（对齐 `actionlib_msgs/GoalStatus`）

```
PENDING=0  ACTIVE=1  PREEMPTED=2  SUCCEEDED=3  ABORTED=4
REJECTED=5 PREEMPTING=6  RECALLING=7  RECALLED=8  LOST=9
```
---

## mock_navigation_action_server.py

使用**自造** `call_action` / `cancel_action` / `action_feedback` / `action_result` 操作码（非标准 rosbridge op）。**历史版本**，`hardware/navigation_utils.py` 已切换到 topic 协议，**不推荐在新场景使用**；保留用于回归测试或兼容性验证。

CLI：

```bash
python mock_navigation_action_server.py --port 9090 \
    --duration 10 --feedback-interval 1
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | `9090` | 监听端口 |
| `--duration` | `10` | 模拟导航总时长秒 |
| `--feedback-interval` | `1` | 发送 feedback 间隔秒 |
| `--failure-rate` | `0` | 随机失败概率 0~1 |

---

## mock_rosbridge_server.py（v1 / 老 service 协议）

最早的 mock：模拟 `call_service` 的 `{finish: true/false, ...}` 契约（**已被升级为 action + 新契约所替代**，但旧测试仍可用）。

- 监听：`0.0.0.0:9091`
- 固定的模拟行为：
  - `cv_detect`: 75% 概率返回 `{finish:true, object_pose, object_type}`
  - `navigation_to_pose`: 启动内部 `simulate_navigation()`
  - `grab_object` / `put_object` / `scan` / `press_button` / 其他：`{finish:true}`
- 内置 `/navigation_status` topic，2Hz 发布

如需改端口，编辑文件开头 `HOST` / `PORT` 常量（没做 CLI 参数化）。

---

## mock_robot_controller.py

一个纯内存版 `RobotController`：不走网络，所有 `send_service_request` / `publish_topic` / `call_service` 都直接打日志并返回 `True`。用于：

- 在 `main.py` 中把一两个机器人换成 mock，做**业务流程冒烟**
- 单元测试里绕开实际 WebSocket 初始化

用法（示意）：

```python
from mock_rosbridge.mock_robot_controller import MockRobotController
robots["robot_a"] = MockRobotController("fake_host", "9090", "robot_a")
```

---

## test_websocket_client.py / test_websocket_concurrent.py

**注意**：这两个脚本测试的是 `robot_connect` 自身的 **WebSocket 服务端**（`network/websocket_server.py`），**不是**机器人的 rosbridge。它们用于：

- 读取 `test_commands/*.json`，通过 WebSocket 把命令推给 `main.py`
- 测试多 client 并发、client_id 注册、心跳等

快捷用法：

```bash
# 交互模式
python test_websocket_client.py localhost 8091

# 直接发命令文件
python test_websocket_client.py localhost 8091 SCAN_QR_CODE_command

# 并发测试（默认 10 个 client）
python test_websocket_concurrent.py localhost 8091
```

---

## Docker 部署

### 镜像内容

`Dockerfile_arm` 基于 `condaforge/miniforge3:latest (linux/arm64)`，建一个 `conda env = rosbridge, python=3.9`，装 `websockets`，`COPY . .` 进 `/app`，默认 `CMD=/bin/bash`。

镜像里包含本文件夹全部 mock：

```
/app/
├── mock_rosbridge_server.py
├── mock_chem_project_action_server.py   ← 推荐
├── mock_navigation_action_server.py
├── mock_robot_controller.py
├── test_websocket_client.py
├── test_websocket_concurrent.py
├── requirements.txt
└── run.sh
```

### 构建

```bash
# 在 mock_rosbridge 目录下执行
docker build -f Dockerfile_arm -t mock-rosbridge:arm .
```

### 镜像归档（便于离线部署到无网的 ARM 设备）

```bash
# 保存镜像到 tar 文件
docker save -o mock-rosbridge-arm.tar mock-rosbridge:arm

# 拷贝到目标设备后加载
docker load -i /path/to/mock-rosbridge-arm.tar
```

### 运行（选一个 mock 启动）

容器默认进 bash，一次一个容器跑一个 mock。

> ⚠ Docker 镜像基于 `condaforge/miniforge3`，不含 ROS 环境。
> 在 Docker 中运行 `mock_chem_project_action_server.py` 时，需加 `--mode bridge-client`
> 指定使用 WebSocket 模拟模式（ros-node 模式需 ROS 环境，不适用于此镜像）。

#### 1. `mock_chem_project_action_server.py`（推荐，新 action 协议）

Docker 内使用 **bridge-client** 模式（无 ROS 环境）：

```bash
docker run -d --name mock_chem \
    --restart unless-stopped \
    mock-rosbridge:arm \
    conda run --no-capture-output -n rosbridge \
    python mock_chem_project_action_server.py \
        --mode bridge-client \
        --rosbridge-url ws://172.16.8.128:9090
```

如果容器部署在机器人本机，且 rosbridge 只监听 `127.0.0.1:9090`，Linux 下建议使用 host 网络：

```bash
docker run -d --name mock_chem \
    --network host \
    --restart unless-stopped \
    mock-rosbridge:arm \
    conda run --no-capture-output -n rosbridge \
    python mock_chem_project_action_server.py \
        --mode bridge-client \
        --rosbridge-url ws://127.0.0.1:9090
```

自定义参数：

```bash
docker run -d --name mock_chem \
    mock-rosbridge:arm \
    conda run --no-capture-output -n rosbridge \
    python mock_chem_project_action_server.py \
        --mode bridge-client \
        --rosbridge-url ws://172.16.8.128:9090 \
        --action-base /zj_humanoid/chem_project \
        --feedback-steps 5 \
        --step-interval 0.5
```

#### 2. `mock_rosbridge_server.py`（老 service 协议）

```bash
docker run -d --name mock_rosbridge \
    -p 9091:9091 \
    mock-rosbridge:arm \
    conda run --no-capture-output -n rosbridge \
    python mock_rosbridge_server.py
```

#### 3. `mock_navigation_action_server.py`（历史 call_action 协议）

```bash
docker run -d --name mock_nav \
    -p 9092:9090 \
    mock-rosbridge:arm \
    conda run --no-capture-output -n rosbridge \
    python mock_navigation_action_server.py --port 9090 --duration 10
```

#### 交互调试（进容器手动启动）

```bash
docker run -it mock-rosbridge:arm bash
# 容器内：
conda activate rosbridge
python mock_chem_project_action_server.py --mode bridge-client --rosbridge-url ws://172.16.8.128:9090
```

### docker-compose 一键启动

当前 `docker-compose.yml` 默认只跑 `mock_rosbridge_server.py`（老版本）。如果要换成新版 action mock 节点，改 `docker-compose.yml`（或在 compose 文件里加第二个 service）。新版节点不暴露端口，只需要能访问机器人 rosbridge：

```yaml
version: '3.8'

services:
  mock_chem:
    build:
      context: .
      dockerfile: Dockerfile_arm
    image: mock-rosbridge:arm
    container_name: mock_chem
    command: >
      conda run --no-capture-output -n rosbridge
      python mock_chem_project_action_server.py
      --mode bridge-client
      --rosbridge-url ws://172.16.8.128:9090
    restart: unless-stopped

  # 可选：同时跑老版
  mock_rosbridge:
    image: mock-rosbridge:arm
    container_name: mock_rosbridge
    ports:
      - "9091:9091"
    command: >
      conda run --no-capture-output -n rosbridge
      python mock_rosbridge_server.py
    restart: unless-stopped
```

启动 / 停止：

```bash
docker compose up -d
docker compose logs -f mock_chem
docker compose down
```

### 常用管理命令

```bash
docker logs -f mock_chem          # 实时日志
docker exec -it mock_chem bash    # 进容器
docker stop mock_chem             # 停
docker start mock_chem            # 启
docker rm -f mock_chem            # 删（-f 强制）
docker ps --filter name=mock_     # 列相关容器
```

### 暴露端口速查

| Mock | 容器内端口 | 常用宿主端口 |
|---|---|---|
| `mock_chem_project_action_server.py` | 无（bridge-client 模式不监听端口） | — |
| `mock_rosbridge_server.py` | 9091 | 9091 |
| `mock_navigation_action_server.py` | 9090（容器内） | 9092（宿主） |

> 三个 mock 同时跑时，要避免容器间端口冲突。最简单的做法：每个容器独立启动，使用不同的宿主映射端口。

---

## 本地运行（不用 Docker）

```bash
pip install -r requirements.txt         # 只装 websockets

# 新版 action mock 节点（ros-node 模式，需 source ROS 环境）
source /opt/ros/noetic/setup.bash
python mock_chem_project_action_server.py

# 无 ROS 时用 bridge-client 模式
python mock_chem_project_action_server.py --mode bridge-client --rosbridge-url ws://172.16.8.128:9090

# 老版
python mock_rosbridge_server.py
./run.sh                                # 同上，封装版
```

---

## 对接 `robot_connect` 主程序

`infrastructure/robot_config.json` 里把对应机器人的 `host` / `port` 改成 mock 的地址即可：

```json
{
    "robots": {
        "robot_b": {
            "host": "192.168.1.100",   ← Docker 宿主 IP（如果跑在别的机器）或 127.0.0.1
            "port": "9090",            ← 对应 mock 的宿主端口
            "robot_type": "robot_b",
            "enabled": true,
            "navigation_map": "test1"
        }
    }
}
```

然后正常启动 `python main.py`，业务命令流就会被导向 mock 并得到模拟响应。

---

## 常见问题

### Q: 客户端握手 `HTTP 400`

`mock_*_server` 默认带 `subprotocols=['rosbridge_v2']`，严格的 `websockets>=13` 客户端必须也带同一个 subprotocol，否则握手失败。

```python
await websockets.connect(uri, subprotocols=["rosbridge_v2"])
```

### Q: `docker run` 后容器立刻退出

默认 `CMD=/bin/bash`，如果用 `-d` 但没有追加要执行的 python 命令，bash 读到 EOF 就退出了。请务必带上 `conda run ... python xxx.py` 的尾部命令，或用 `-it` 进交互。

### Q: 端口占用

```bash
sudo lsof -i :9090                   # 看谁占了
docker run ... -p 19090:9090 ...     # 换个宿主端口
```

### Q: 容器内看不到 print 日志

`PYTHONUNBUFFERED=1` 已经在 Dockerfile 里设置；若还是延迟，检查用的不是 `conda run --no-capture-output`（注意 `--no-capture-output` 必须加，否则 conda 会缓冲 stdout）。

### Q: ARM / x86 混用

`Dockerfile_arm` 明确 `--platform=linux/arm64`；在 x86 桌面上 build 时 Docker 会自动用 QEMU 模拟。x86 生产跑可以复制一份 Dockerfile 把 `--platform` 改为 `linux/amd64`。

---

## 维护日志

- **2026-04-30** `mock_chem_project_action_server.py` 同时支持 ROS Service + ROS Action 双协议；抽出公共 `_execute_task` 逻辑；新增 `ros-node` 默认模式（rospy+actionlib）；更新 README
- **2026-04-23** 新增 `mock_chem_project_action_server.py`；`Dockerfile_arm` 新增 `PYTHONUNBUFFERED` + 暴露 9090/9092 + 明确 CMD 说明；README 重写为总览式文档
- **2026-04-17** 新增 `mock_navigation_action_server.py`（历史 call_action 协议）
- **2024-12-22** 初始版本 `mock_rosbridge_server.py` + `Dockerfile_arm`
