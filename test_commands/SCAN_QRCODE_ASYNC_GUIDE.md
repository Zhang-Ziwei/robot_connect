# SCAN_QRCODE 异步状态机使用指南

## 📖 概述

SCAN_QRCODE命令执行时间较长（通常5-10分钟），为避免HTTP超时和提供更好的用户体验，现已升级为**异步模式**。

### 核心改进

| 特性 | 原同步模式 | 新异步模式 |
|-----|----------|----------|
| **响应时间** | 5-10分钟 | **立即（<100ms）** |
| **HTTP超时风险** | 高 | **无** |
| **状态可见性** | 无法查询 | **实时查询** |
| **错误诊断** | 困难 | **详细步骤信息** |
| **并发支持** | 阻塞其他命令 | **非阻塞** |

## 🚀 快速开始

### 1. 启动扫码任务

```bash
curl -X POST http://localhost:8090 \
  -H 'Content-Type: application/json' \
  -d '{
    "cmd_type": "SCAN_QRCODE",
    "cmd_id": "scan_001",
    "params": {}
  }'
```

**立即返回**（无需等待任务完成）：

```json
{
  "success": true,
  "message": "SCAN_QRCODE任务已启动",
  "task_id": "SCAN_a1b2c3d4",
  "note": "使用 SCAN_QRCODE_RESULT 命令查询任务状态"
}
```

⚠️ **重要**: 请保存返回的 `task_id`，后续查询状态时需要使用。

### 2. 查询任务状态

```bash
curl -X POST http://localhost:8090 \
  -H 'Content-Type: application/json' \
  -d '{
    "cmd_type": "SCAN_QRCODE_RESULT",
    "cmd_id": "query_001",
    "params": {
      "task_id": "SCAN_a1b2c3d4"
    }
  }'
```

**返回示例**：

```json
{
  "success": true,
  "message": "状态查询成功",
  "data": {
    "task_id": "SCAN_a1b2c3d4",
    "status": "运行中",
    "current_step": {
      "name": "GRABBING_BOTTLE",
      "description": "抓取瓶子 (glass_bottle_500)"
    },
    "completed_steps": [...],
    "scanned_bottles": [...],
    "duration_seconds": 35.8,
    "scanned_count": 1
  }
}
```

### 3. 循环查询直到完成

建议每3-5秒查询一次，直到状态为 `已完成`、`错误` 或 `已取消`。

## 📊 状态机详解

### 任务状态

| 状态 | 英文 | 说明 | 建议操作 |
|-----|------|------|---------|
| 未开始 | NOT_STARTED | 任务尚未创建 | - |
| 运行中 | RUNNING | 任务正在执行某个步骤 | 继续查询 |
| 等待中 | WAITING | 等待外部输入（如ID录入） | 发送对应命令 |
| 已完成 | COMPLETED | 任务成功完成 | 无需操作 |
| 错误 | ERROR | 任务执行失败 | 查看error_message |
| 已取消 | CANCELLED | 任务被取消 | 重新启动 |

### 执行步骤

SCAN_QRCODE流程包含10个步骤：

| # | 步骤名称 | 说明 | 预计耗时 |
|---|---------|------|---------|
| 1 | NAVIGATING_TO_SCAN | 导航到扫描台 | 30-60秒 |
| 2 | GRAB_SCAN_GUN | 抓取扫描枪（可选） | 10-20秒 |
| 3 | CV_DETECTING | 视觉检测瓶子 | 5-10秒 |
| 4 | GRABBING_BOTTLE | 抓取瓶子 | 10-15秒 |
| 5 | SCANNING | 扫描二维码 | 5-10秒 |
| 6 | WAITING_ID_INPUT | 等待ID录入 | 变量（需人工） |
| 7 | PUTTING_TO_BACK | 放置到后部平台 | 10-15秒 |
| 8 | TURNING_BACK_FRONT | 转回正面 | 5-10秒 |
| 9 | NAVIGATING_TO_SPLIT | 导航到分液台 | 30-60秒 |
| 10 | PUTTING_DOWN | 放下瓶子 | 10-15秒 |

## 🔄 完整工作流程

```
┌─────────────┐
│  HTTP客户端  │
└──────┬──────┘
       │
       │ ① 发送 SCAN_QRCODE
       ▼
┌─────────────────────┐
│   Robot系统          │
│  - 立即返回task_id   │◄─────────┐
│  - 启动后台线程      │          │
└──────┬──────────────┘          │
       │                         │
       │ ② 后台执行步骤1-10        │
       │                         │
       │                         │
       ▼                         │
┌─────────────────────┐          │
│  状态机更新          │          │
│  - 记录当前步骤      │          │
│  - 记录已完成步骤    │          │
│  - 记录已扫描瓶子    │          │
└──────┬──────────────┘          │
       │                         │
       │ ③ HTTP客户端轮询查询      │
       └─────────────────────────┘
```

## 📝 使用示例

### 示例1: Python自动化脚本

```python
import requests
import time

SERVER_URL = "http://localhost:8090"

# 1. 启动扫码任务
response = requests.post(SERVER_URL, json={
    "cmd_type": "SCAN_QRCODE",
    "cmd_id": "scan_001",
    "params": {}
})
task_id = response.json()["task_id"]
print(f"任务已启动: {task_id}")

# 2. 循环查询状态
while True:
    response = requests.post(SERVER_URL, json={
        "cmd_type": "SCAN_QRCODE_RESULT",
        "cmd_id": "query",
        "params": {"task_id": task_id}
    })
    
    data = response.json()["data"]
    status = data["status"]
    current_step = data["current_step"]["description"]
    
    print(f"状态: {status}, 当前步骤: {current_step}")
    
    # 检查是否需要录入ID
    if status == "等待中" and "等待ID录入" in current_step:
        print("需要录入ID，请发送 SCAN_QRCODE_ENTER_ID 命令")
        # 这里可以自动发送ID或等待人工介入
    
    # 检查任务是否完成
    if status in ["已完成", "错误", "已取消"]:
        print(f"任务结束: {status}")
        if data.get("error_message"):
            print(f"错误信息: {data['error_message']}")
        break
    
    time.sleep(3)  # 等待3秒再查询
```

### 示例2: 使用测试脚本

我们提供了现成的测试脚本：

```bash
# 运行自动测试（包含轮询查询）
python test_scan_qrcode_async.py
```

测试脚本会：
1. 自动发送SCAN_QRCODE命令
2. 每3秒自动查询一次状态
3. 显示当前步骤和已完成步骤
4. 列出已扫描的瓶子
5. 提示何时需要录入ID

## 📤 状态响应详解

### 完整响应示例

```json
{
  "success": true,
  "message": "状态查询成功",
  "data": {
    "task_id": "SCAN_a1b2c3d4",
    "status": "运行中",
    
    "current_step": {
      "name": "GRABBING_BOTTLE",
      "description": "抓取瓶子 (glass_bottle_500)"
    },
    
    "completed_steps": [
      {
        "step": "NAVIGATING_TO_SCAN",
        "step_name": "导航到扫描台",
        "message": "开始导航到扫描台",
        "timestamp": "2025-11-17T10:30:15.123456",
        "duration": 45.2
      },
      {
        "step": "CV_DETECTING",
        "step_name": "视觉检测瓶子",
        "message": "视觉检测瓶子",
        "timestamp": "2025-11-17T10:31:00.456789",
        "duration": 50.5
      }
    ],
    
    "scanned_bottles": [
      {
        "bottle_id": "BTL-2025-001",
        "type": "glass_bottle_500",
        "slot_index": 0,
        "timestamp": "2025-11-17T10:32:45.789012"
      }
    ],
    
    "current_bottle_info": {
      "type": "glass_bottle_500",
      "pose": "pose_0",
      "slot_index": 1
    },
    
    "error_message": null,
    "start_time": "2025-11-17T10:30:10.000000",
    "end_time": null,
    "duration_seconds": 155.8,
    "scanned_count": 1
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|-----|------|------|
| `task_id` | string | 任务唯一标识符 |
| `status` | string | 任务当前状态 |
| `current_step` | object | 当前正在执行的步骤 |
| `completed_steps` | array | 已完成的步骤列表（按时间顺序） |
| `scanned_bottles` | array | 已扫描的瓶子列表 |
| `current_bottle_info` | object | 当前正在处理的瓶子信息 |
| `error_message` | string | 错误信息（如有） |
| `start_time` | string | 任务开始时间（ISO格式） |
| `end_time` | string | 任务结束时间（如已结束） |
| `duration_seconds` | number | 已执行时长（秒） |
| `scanned_count` | number | 已扫描瓶子数量 |

## 🎯 最佳实践

### 1. 轮询策略

```python
# 推荐的轮询间隔
POLL_INTERVAL = 3  # 秒

# 设置最大查询次数（避免无限循环）
MAX_POLLS = 200  # 约10分钟

for i in range(MAX_POLLS):
    status = query_task_status(task_id)
    
    if status in ["已完成", "错误", "已取消"]:
        break
    
    time.sleep(POLL_INTERVAL)
```

### 2. 错误处理

```python
try:
    response = requests.post(SERVER_URL, json=cmd_data, timeout=10)
    response.raise_for_status()
    result = response.json()
    
    if not result.get("success"):
        print(f"命令失败: {result.get('message')}")
        # 处理失败情况
        
except requests.exceptions.Timeout:
    print("请求超时，请检查网络连接")
except requests.exceptions.RequestException as e:
    print(f"请求错误: {e}")
```

### 3. ID录入处理

```python
def handle_waiting_state(data):
    """处理等待ID录入状态"""
    if data["status"] == "等待中":
        step_desc = data["current_step"]["description"]
        
        if "等待ID录入" in step_desc:
            bottle_info = data.get("current_bottle_info", {})
            bottle_type = bottle_info.get("type")
            
            print(f"需要录入瓶子ID，类型: {bottle_type}")
            
            # 方式1: 等待人工输入
            bottle_id = input("请输入瓶子ID: ")
            
            # 方式2: 从数据库查询
            # bottle_id = get_bottle_id_from_database()
            
            # 发送ID
            send_enter_id(bottle_id, bottle_type)
```

### 4. 状态展示

```python
def display_status(data):
    """友好地显示任务状态"""
    print(f"\n{'='*60}")
    print(f"任务ID: {data['task_id']}")
    print(f"状态: {data['status']}")
    print(f"当前步骤: {data['current_step']['description']}")
    print(f"执行时长: {data['duration_seconds']:.1f}秒")
    print(f"已扫描: {data['scanned_count']}个瓶子")
    
    if data['completed_steps']:
        print(f"已完成步骤: {len(data['completed_steps'])}")
        for step in data['completed_steps'][-3:]:  # 显示最近3步
            print(f"  - {step['step_name']}")
    
    if data.get('error_message'):
        print(f"⚠️  错误: {data['error_message']}")
    
    print('='*60)
```

## ⚠️ 注意事项

### 1. task_id管理

- 每次调用SCAN_QRCODE都会生成新的task_id
- task_id格式：`SCAN_xxxxxxxx`（8位随机字符）
- 必须保存task_id用于后续查询
- 目前只支持单任务模式（新任务会覆盖旧任务状态）

### 2. 轮询频率

- **推荐间隔**: 3-5秒
- **最小间隔**: 1秒（避免过于频繁）
- **最大间隔**: 10秒（可能错过关键状态）

### 3. 超时处理

- 单个步骤通常在10-60秒内完成
- 如果某步骤超过5分钟未更新，可能需要人工介入
- 建议设置总超时时间（如15分钟）

### 4. ID录入

- 当状态为"等待中"时，任务会暂停
- 必须发送`SCAN_QRCODE_ENTER_ID`命令才能继续
- 等待超时时间：300秒（5分钟）
- 超时后任务将失败

### 5. 错误恢复

- 任务失败后状态机不会自动重置
- 需要重新发送SCAN_QRCODE命令启动新任务
- 可以查看`error_message`了解失败原因

## 🛠️ 故障排查

### 问题1: 查询状态返回"任务不存在"

**可能原因**:
- task_id错误或已过期
- 服务器重启导致状态丢失

**解决方案**:
```bash
# 查询当前任务（不指定task_id）
curl -X POST http://localhost:8090 \
  -H 'Content-Type: application/json' \
  -d '{
    "cmd_type": "SCAN_QRCODE_RESULT",
    "cmd_id": "query",
    "params": {}
  }'
```

### 问题2: 任务长时间停留在某一步骤

**可能原因**:
- 机器人卡住
- 网络断开
- 等待外部输入

**解决方案**:
1. 检查机器人实际状态
2. 查看日志文件：`logs/error_log_*.txt`
3. 如需重启，发送新的SCAN_QRCODE命令

### 问题3: 状态查询请求失败

**可能原因**:
- HTTP服务器未启动
- 端口号错误
- 网络问题

**解决方案**:
```bash
# 检查服务器是否运行
curl http://localhost:8090

# 应该返回健康检查信息
```

## 📚 相关文档

- **完整功能清单**: `新功能清单.md`
- **快速开始**: `QUICK_START_NEW_FUNCTION.md`
- **HTTP通信指南**: `HTTP异步通信使用指南.md`
- **测试脚本**: `test_scan_qrcode_async.py`

## 📞 技术支持

遇到问题？

1. 查看日志：`logs/error_log_*.txt`
2. 运行测试脚本验证功能：`python test_scan_qrcode_async.py`
3. 查看状态机代码：`scan_state_machine.py`
4. 查看命令处理代码：`cmd_handler.py`

---

**版本**: v1.0  
**最后更新**: 2025-11-17  
**作者**: Robot Control System Team

