# 新功能使用说明

## 概述

根据`new_function.md`的需求，系统新增了可复用的机器人任务调度功能，支持通过HTTP JSON消息控制机器人执行各种操作。

## 核心概念

### 1. 基础动作（action_type）

最基础的ROS节点动作：
- `waiting_navigation_status` - 进入等待导航移动的安全姿势
- `navigation_to_pose` - 导航到目标位置
- `grab_object` - 抓取物件
- `turn_waist` - 转腰
- `put_object` - 放置物件
- `scan` - 扫码动作
- `cv_detect` - 视觉检测

### 2. 命令类型（CMD_TYPES）

由基础动作组合而成的可复用技能：
- `PICK_UP` - 拿取东西到平台
- `PUT_TO` - 放下东西到某个地方
- `TAKE_BOTTOL_FROM_SP_TO_SP` - 从某处拿到某处
- `SCAN_QRCODE` - 扫描二维码
- `ENTER_ID` - 录入ID
- `BOTTLE_GET` - 获取样品瓶信息

## 系统架构

```
HTTP Client (外部系统)
    ↓ JSON命令
HTTP Server (http_server.py:8080)
    ↓
Command Handler (cmd_handler.py)
    ↓ 调用
Task Optimizer (task_optimizer.py) - 优化导航路径
    ↓
Bottle Manager (bottle_manager.py) - 管理瓶子信息
    ↓
Robot Controller (robot_controller.py) - 执行机器人动作
```

## 新增模块

### 1. bottle_manager.py - 瓶子管理器
**功能**：
- 管理所有样品瓶的信息和参数
- 管理目标点位的状态和容量
- 提供瓶子查询和更新接口

**主要类**：
- `BottleInfo` - 瓶子信息类
- `TargetPose` - 目标点位类
- `BottleManager` - 瓶子管理器

### 2. task_optimizer.py - 任务优化器
**功能**：
- 优化机器人导航路径，减少导航次数
- 最大化每次导航的拾取效率
- 合理安排拾取和放置顺序

**主要方法**：
- `optimize_pickup_task()` - 优化PICK_UP任务
- `optimize_put_task()` - 优化PUT_TO任务
- `optimize_transfer_task()` - 优化TRANSFER任务

### 3. cmd_handler.py - 命令处理器
**功能**：
- 接收并解析HTTP JSON命令
- 调用任务优化器
- 控制机器人执行具体操作
- 返回执行结果

**支持的命令**：
- `PICK_UP` - 拾取物品
- `PUT_TO` - 放置物品
- `TAKE_BOTTOL_FROM_SP_TO_SP` - 转移物品
- `SCAN_QRCODE` - 扫描二维码
- `ENTER_ID` - 录入ID
- `BOTTLE_GET` - 查询瓶子信息

### 4. http_server.py - HTTP服务器
**功能**：
- 监听HTTP端口（默认8080）
- 接收JSON格式的命令
- 返回执行结果

## 使用方法

### 启动系统

```bash
python main.py
```

选择运行模式：
```
选择运行模式:
1. HTTP服务器模式（接收JSON命令）
2. 传统流程模式（手动循环）

请选择模式 (1/2) [默认: 1]: 1
```

系统启动后会显示：
```
HTTP服务器已启动，等待接收命令...
可以通过以下方式发送命令:
  curl -X POST http://localhost:8080 -H 'Content-Type: application/json' -d @command.json
```

### 发送命令

#### 方法1：使用curl
```bash
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d @test_commands/pickup_command.json
```

#### 方法2：使用Python requests
```python
import requests

with open('test_commands/pickup_command.json') as f:
    cmd_data = json.load(f)

response = requests.post(
    'http://localhost:8080',
    json=cmd_data
)

result = response.json()
print(result)
```

#### 方法3：使用测试脚本
```bash
python test_http_commands.py
```

## 命令示例

### 1. PICK_UP - 拾取物品

**请求**:
```json
{
    "cmd_id": "PICK_UP_001",
    "cmd_type": "PICK_UP",
    "params": {
        "target_params": [
            {"bottle_id": "glass_bottle_1000_001"},
            {"bottle_id": "glass_bottle_1000_002"}
        ],
        "timeout": 10.0
    }
}
```

**响应**:
```json
{
    "cmd_id": "PICK_UP_001",
    "success": true,
    "message": "PICK_UP完成",
    "success_count": 2,
    "failed_bottles": [],
    "total": 2
}
```

### 2. PUT_TO - 放置物品

**请求**:
```json
{
    "cmd_id": "PUT_TO_001",
    "cmd_type": "PUT_TO",
    "params": {
        "release_params": [
            {
                "bottle_id": "glass_bottle_1000_001",
                "release_pose": "worktable_temp_001"
            }
        ],
        "timeout": 10.0
    }
}
```

### 3. BOTTLE_GET - 查询瓶子信息

**查询所有瓶子**:
```json
{
    "cmd_id": "BOTTLE_GET_001",
    "cmd_type": "BOTTLE_GET",
    "params": {},
    "detail_params": true
}
```

**查询指定瓶子**:
```json
{
    "cmd_id": "BOTTLE_GET_002",
    "cmd_type": "BOTTLE_GET",
    "params": {
        "bottle_id": "glass_bottle_1000_001"
    },
    "detail_params": true
}
```

**查询指定点位**:
```json
{
    "cmd_id": "BOTTLE_GET_003",
    "cmd_type": "BOTTLE_GET",
    "params": {
        "pose_name": "shelf_temp_1000_001"
    },
    "detail_params": true
}
```

## 任务优化算法

### PICK_UP优化
1. 按导航点位（navigation_pose）分组
2. 每个导航点位尽量拾取多个瓶子
3. 检查后部平台容量限制（最多4个）
4. 检查目标点位是否已满

**输出**: `{navigation_pose: [bottle_ids]}`

### PUT_TO优化
1. 按放置点位对应的导航点位分组
2. 同一导航点位的瓶子一起处理
3. 检查目标点位容量

**输出**: `{navigation_pose: [(bottle_id, release_pose)]}`

### TRANSFER优化
1. 组合PICK_UP和PUT_TO
2. 每批次尽量填满后部平台（4个瓶子）
3. 相同release_pose的瓶子尽量同批次
4. 最小化导航次数

**输出**: 多批次任务列表

## 测试

### 1. 测试HTTP服务器

```bash
# 终端1 - 启动服务器
python main.py
# 选择模式1

# 终端2 - 健康检查
curl http://localhost:8080
```

### 2. 测试命令

```bash
# 测试BOTTLE_GET
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d @test_commands/bottle_get_command.json
```

### 3. 运行完整测试

```bash
python test_http_commands.py
```

## 配置说明

### 瓶子参数配置

在`bottle_manager.py`中可以配置默认瓶子信息：

```python
def _init_default_bottles(self):
    default_bottles = [
        {
            "bottle_id": "glass_bottle_1000_001",
            "object_type": "glass_bottle_1000",
            "hand": "right",
            "target_pose": "shelf_temp_1000_001",
            "navigation_pose": "shelf"
        },
        # 添加更多瓶子...
    ]
```

### 点位配置

```python
def _init_default_poses(self):
    pose_names = [
        "shelf_temp_1000_001",      # 货架暂存区
        "back_temp_1000_001",       # 后部平台
        "worktable_temp_001",       # 工作台
        # 添加更多点位...
    ]
```

### HTTP服务器配置

在`main.py`中修改：

```python
http_server = get_http_server(host='0.0.0.0', port=8080)
```

## 目录结构

```
robot_connect/
├── bottle_manager.py          # 瓶子管理器
├── task_optimizer.py          # 任务优化器
├── cmd_handler.py             # 命令处理器
├── http_server.py             # HTTP服务器
├── main.py                    # 主程序（已更新）
├── test_commands/             # 测试命令
│   ├── pickup_command.json
│   ├── put_to_command.json
│   └── bottle_get_command.json
├── test_http_commands.py      # HTTP测试脚本
└── NEW_FUNCTION_README.md     # 本文档
```

## 常见问题

### Q1: HTTP服务器启动失败？
**A:** 检查端口8080是否被占用：
```bash
netstat -tulpn | grep 8080
```

### Q2: 命令发送后无响应？
**A:** 
1. 检查服务器是否运行
2. 检查JSON格式是否正确
3. 查看服务器日志

### Q3: 机器人连接失败？
**A:** 
1. 检查机器人IP和端口
2. 确认机器人已开机
3. 查看错误日志文件

### Q4: 如何添加新的CMD_TYPE？
**A:** 在`cmd_handler.py`中：
1. 添加处理函数 `handle_xxx()`
2. 在`handle_command()`中注册
3. 实现具体逻辑

## 性能优化

### 导航优化
- 系统会自动合并相同导航点位的任务
- 减少机器人移动次数
- 提高整体效率

### 容量管理
- 自动检查后部平台容量（4个瓶子）
- 自动检查目标点位容量（2个瓶子）
- 超出容量的瓶子会返回失败列表

### 批次处理
- TRANSFER命令支持多批次执行
- 每批次尽量填满后部平台
- 自动优化批次划分

## 日志

### 查看运行日志
```bash
tail -f logs/error_log_*.txt
```

### 查看HTTP请求
```bash
grep "HTTP服务器" logs/error_log_*.txt
```

### 查看命令执行
```bash
grep "命令处理器" logs/error_log_*.txt
```

## 下一步

1. **扩展命令类型**：实现POUR_SEPARATE、PIPETTE_SEPARATE等
2. **增强优化算法**：考虑更多约束条件
3. **添加状态机**：管理复杂的多步骤流程
4. **实现重试机制**：失败命令自动重试
5. **添加WebSocket**：支持实时状态推送

## 支持

如有问题，请查看：
- 错误日志：`logs/error_log_*.txt`
- 文档：本文档
- 测试：`test_http_commands.py`

