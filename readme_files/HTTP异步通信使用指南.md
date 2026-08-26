# HTTP异步通信使用指南

## 问题背景

机器人执行动作（如SCAN_QRCODE）需要很长时间（可能几分钟），如果使用**同步模式**：
- HTTP客户端必须等待整个流程完成
- 容易**超时**（默认30-60秒）
- 容易**网络中断**
- 无法并发处理

## 解决方案：使用队列模式（异步执行）

### 工作流程

```
【同步模式 - 有问题】
对方发送 → 等待3分钟 → 收到结果
         (HTTP连接一直保持，容易超时❌)

【队列模式 - 推荐】
对方发送 → 立即收到task_id (0.1秒✅)
         ↓
      后台执行任务（3分钟）
         ↓
对方查询 → 获取任务状态/结果
```

---

## 使用步骤

### 第1步：启动服务器（队列模式）

```bash
python main.py
# 选择模式 1（HTTP服务器模式）
# 选择执行模式 2（队列模式） ← 重要！
```

输出示例：
```
✓ 任务队列模式已启用
  - 多个命令会排队执行
  - 每个命令执行完成后才会执行下一个

✓ HTTP服务器已启动
监听地址: 0.0.0.0:8090 (所有网络接口)
本机IP: 172.16.11.130
模式: 队列模式
```

### 第2步：对方发送命令（立即返回）

```bash
curl -X POST http://172.16.11.130:8090 \
  -H 'Content-Type: application/json' \
  -d @SCAN_QR_CODE_command.json
```

**立即收到响应**（不用等待执行完成）：
```json
{
    "success": true,
    "message": "任务已加入队列",
    "task_id": "task_20231215_143022_abc123",
    "queue_size": 1,
    "note": "使用 GET /task/<task_id> 查询任务状态"
}
```

### 第3步：查询任务状态

#### 方法A：查询特定任务

```bash
curl http://172.16.11.130:8090/task/task_20231215_143022_abc123
```

响应示例（执行中）：
```json
{
    "task_id": "task_20231215_143022_abc123",
    "status": "running",
    "cmd_type": "SCAN_QRCODE",
    "submit_time": "2023-12-15 14:30:22",
    "start_time": "2023-12-15 14:30:23",
    "result": null
}
```

响应示例（已完成）：
```json
{
    "task_id": "task_20231215_143022_abc123",
    "status": "completed",
    "cmd_type": "SCAN_QRCODE",
    "submit_time": "2023-12-15 14:30:22",
    "start_time": "2023-12-15 14:30:23",
    "end_time": "2023-12-15 14:33:45",
    "result": {
        "success": true,
        "message": "SCAN_QRCODE完成",
        "scanned_count": 3,
        "scanned_bottles": [...]
    }
}
```

#### 方法B：查询队列整体状态

```bash
curl http://172.16.11.130:8090/queue/status
```

响应示例：
```json
{
    "queue_size": 2,
    "total_tasks": 5,
    "completed_tasks": 3,
    "failed_tasks": 0,
    "running_task": "task_20231215_143022_abc123"
}
```

---

## 对方电脑的Python实现示例

```python
import requests
import time
import json

SERVER_URL = "http://172.16.11.130:8090"

def send_command(json_file):
    """发送命令，立即返回task_id"""
    with open(json_file, 'r') as f:
        data = json.load(f)
    
    response = requests.post(SERVER_URL, json=data)
    result = response.json()
    
    if result.get('success'):
        task_id = result['task_id']
        print(f"✓ 任务已提交: {task_id}")
        return task_id
    else:
        print(f"✗ 提交失败: {result.get('message')}")
        return None

def wait_for_task(task_id, timeout=600, interval=2):
    """等待任务完成（轮询）"""
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        response = requests.get(f"{SERVER_URL}/task/{task_id}")
        task_info = response.json()
        
        status = task_info.get('status')
        print(f"任务状态: {status}")
        
        if status == 'completed':
            print("✓ 任务完成")
            return task_info.get('result')
        elif status == 'failed':
            print("✗ 任务失败")
            return task_info.get('result')
        
        time.sleep(interval)  # 每2秒查询一次
    
    print("✗ 等待超时")
    return None

# 使用示例
if __name__ == "__main__":
    # 1. 发送命令
    task_id = send_command("SCAN_QR_CODE_command.json")
    
    if task_id:
        # 2. 等待完成
        result = wait_for_task(task_id)
        
        # 3. 处理结果
        if result and result.get('success'):
            print(f"扫描完成，数量: {result.get('scanned_count')}")
        else:
            print("任务失败")
```

---

## 任务状态说明

| 状态 | 说明 |
|------|------|
| `pending` | 任务在队列中等待 |
| `running` | 任务正在执行 |
| `completed` | 任务成功完成 |
| `failed` | 任务执行失败 |

---

## 优势对比

### 同步模式（不推荐长任务）
❌ HTTP连接需要保持3-5分钟  
❌ 容易超时  
❌ 网络中断会导致失败  
❌ 无法并发  

### 队列模式（推荐✅）
✅ HTTP请求立即返回（<1秒）  
✅ 不会超时  
✅ 网络断开不影响任务执行  
✅ 支持多个命令并发提交（按顺序执行）  
✅ 可以随时查询状态  
✅ 服务器重启后任务不丢失（可选）  

---

## 常见问题

### Q1: 如果忘记task_id怎么办？
可以查询队列状态获取所有任务信息：
```bash
curl http://172.16.11.130:8090/queue/status
```

### Q2: 任务执行失败怎么办？
查询任务状态会返回失败原因：
```json
{
    "status": "failed",
    "result": {
        "success": false,
        "message": "具体错误信息"
    }
}
```

### Q3: 可以取消正在执行的任务吗？
当前版本不支持，任务一旦开始执行就无法取消（可以扩展实现）。

### Q4: 多个命令同时发送会怎样？
队列会按顺序执行，一个完成后再执行下一个，不会冲突。

---

## 配置端口

端口配置在 `constants.py`：
```python
HTTP_SERVER_PORT = 8090  # 修改这里即可全局生效
```

---

## 测试命令

```bash
# 1. 发送任务
TASK_ID=$(curl -s -X POST http://172.16.11.130:8090 \
  -H 'Content-Type: application/json' \
  -d @SCAN_QR_CODE_command.json | jq -r '.task_id')

echo "Task ID: $TASK_ID"

# 2. 循环查询状态
while true; do
    STATUS=$(curl -s http://172.16.11.130:8090/task/$TASK_ID | jq -r '.status')
    echo "Status: $STATUS"
    
    if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
        break
    fi
    
    sleep 2
done

# 3. 获取最终结果
curl http://172.16.11.130:8090/task/$TASK_ID | jq
```

---

## 总结

**关键点**：
1. **使用队列模式**（模式2）避免HTTP超时
2. 发送命令后**立即返回task_id**
3. 通过**轮询**或**定时查询**获取任务状态
4. 任务在**后台执行**，不受网络影响

这样可以完美解决长时间任务导致的HTTP超时问题！

