# mock_ros — 带完整 ROS 的 mock 节点容器

本目录提供一个独立 Docker 容器，内含：

| 组件 | 说明 |
|------|------|
| ROS Noetic | `ros:noetic-ros-base` |
| rosbridge_suite | 将 ROS topic/service 桥接为 WebSocket（端口 9090） |
| mock_chem_project_action_server | 模拟 `/chem_project_service` + `/chem_project/*` action |

## 架构

```
本容器内
  roscore
    ↓ ROS 原生 topic / service
  mock_chem_project_action_server（ros-node 模式）
    ↓ ROS 原生 topic / service
  rosbridge_server（ws://0.0.0.0:9090）
         │
         │  ws://机器人IP:9090
         ▼
  main.py / robot_connect（宿主机）
```

`main.py` 把容器当作真实机器人连接，robot_config.json 里填容器宿主机 IP 即可。

---

## 快速启动

### 构建并启动

```bash
cd mock_ros/

# 构建镜像（首次或修改代码后）
docker compose build

# 启动容器（前台，可看到完整日志）
docker compose up

# 后台启动
docker compose up -d

# 查看日志
docker compose logs -f
```

### 停止

```bash
docker compose down
```

---

## 测试方法

容器启动后，rosbridge 监听 `ws://<宿主机IP>:9090`。

### 1. 确认 rosbridge 已就绪

用任意 WebSocket 客户端连接，发送以下 JSON 应收到响应：

```bash
# 安装 websocat（可选）
websocat ws://localhost:9090
# 粘贴：{"op":"call_service","service":"/rosapi/get_time","args":{}}
```

### 2. 测试 Service（`/chem_project_service`）

修改 `robot_config.json` 中机器人地址指向本容器：

```json
{
  "host": "localhost",
  "port": 9090
}
```

然后通过 HTTP 发送命令（在宿主机另一个终端）：

```bash
# 先发 START_WORKING 启动任务
curl -s -X POST http://localhost:8848 \
  -H "Content-Type: application/json" \
  -d '{"cmd_type":"START_WORKING","cmd_id":"test-001","params":{}}'

# 查询任务状态
curl -s -X POST http://localhost:8848 \
  -H "Content-Type: application/json" \
  -d '{"cmd_type":"GET_TASK_STATE","cmd_id":"test-002","params":{}}' \
  | python3 -m json.tool
```

### 3. 测试 task 行为

mock 支持以下特殊 task 名称触发不同行为：

| task 名 | 行为 |
|---------|------|
| `success_fast` | 立即成功，无 feedback |
| `success_with_steps` | 3 条 feedback 后成功（默认） |
| `fail` | 立即失败，error_msg 带诊断 |
| `fail_with_steps` | 2 条 feedback 后失败 |
| `long` | 运行 30s，用于测试超时 |
| 其他（如 `SCAN_TABLE`） | 等同 `success_with_steps` |

通过 `robot.send_service_request_task(service, task="fail")` 可测试错误处理分支。

### 4. 在容器内检查 ROS topic

```bash
# 进入容器
docker exec -it mock-ros-chem bash
source /opt/ros/noetic/setup.bash

# 列出所有 topic
rostopic list

# 查看 action 状态
rostopic echo /chem_project/status

# 查看 service 是否存在
rosservice list | grep chem_project
```

---

## 与 mock_rosbridge 的区别

| | mock_rosbridge | mock_ros（本目录） |
|-|-|-|
| 基础镜像 | miniforge3（纯 Python） | ros:noetic-ros-base |
| 镜像大小 | ~200 MB | ~1.5 GB |
| 运行模式 | bridge-client / embedded-server | **ros-node**（真实 rospy） |
| 需要 ROS | ✗ | ✓ |
| 适用场景 | 轻量联调、CI | 完整仿真机器人 ROS 环境 |

mock_rosbridge 不受本目录影响，两个容器可并行运行在不同端口。

---

## mock_chem_project_action_server 详细用法

Action/Service 消息契约、三种运行模式、命令行参数、KAIAO `/robot_task` 联调、WebSocket 手测示例见：

**[mock_chem_project_action_server_用法.md](./mock_chem_project_action_server_用法.md)**
