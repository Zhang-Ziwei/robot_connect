# 新功能快速开始

## 5分钟上手指南

### 1. 启动HTTP服务器模式

```bash
python main.py
```

选择模式1（HTTP服务器模式）：
```
选择运行模式:
1. HTTP服务器模式（接收JSON命令）
2. 传统流程模式（手动循环）

请选择模式 (1/2) [默认: 1]: 1
```

✅ 看到以下提示说明服务器启动成功：
```
HTTP服务器运行在: http://0.0.0.0:8080
HTTP服务器已启动，等待接收命令...
```

### 2. 测试服务器（新终端）

```bash
# 健康检查
curl http://localhost:8080

# 应该看到
{"status": "running", "message": "Robot Control HTTP Server is running"}
```

### 3. 发送第一个命令

查询所有瓶子信息：
```bash
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d @test_commands/bottle_get_command.json
```

### 4. 使用测试脚本

```bash
python test_http_commands.py
```

按照提示选择要测试的命令。

## 命令快速参考

### 查询所有瓶子
```bash
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d '{
    "cmd_id": "GET_ALL",
    "cmd_type": "BOTTLE_GET",
    "params": {},
    "detail_params": true
  }'
```

### 拾取瓶子
```bash
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d @test_commands/pickup_command.json
```

### 放置瓶子
```bash
curl -X POST http://localhost:8080 \
  -H 'Content-Type: application/json' \
  -d @test_commands/put_to_command.json
```

## 目录说明

```
robot_connect/
├── bottle_manager.py          # 瓶子管理
├── task_optimizer.py          # 任务优化
├── cmd_handler.py             # 命令处理
├── http_server.py             # HTTP服务器
├── test_commands/             # 测试命令JSON文件
│   ├── pickup_command.json
│   ├── put_to_command.json
│   └── bottle_get_command.json
└── test_http_commands.py      # 测试脚本
```

## 支持的命令类型

| 命令类型 | 说明 | 测试文件 |
|---------|------|---------|
| PICK_UP | 拾取物品到后部平台 | pickup_command.json |
| PUT_TO | 从后部平台放置到目标位置 | put_to_command.json |
| BOTTLE_GET | 查询瓶子信息 | bottle_get_command.json |
| TAKE_BOTTOL_FROM_SP_TO_SP | 转移物品 | - |
| SCAN_QRCODE | 扫描二维码 | - |
| ENTER_ID | 录入ID | - |

## 常用操作

### 停止服务器
在运行服务器的终端按 `Ctrl+C`

### 查看日志
```bash
tail -f logs/error_log_*.txt
```

### 检查端口
```bash
netstat -tulpn | grep 8080
```

## 下一步

查看完整文档：`NEW_FUNCTION_README.md`

## 问题排查

### 问题1：端口已被占用
```bash
# 查看占用端口的进程
lsof -i :8080

# 杀死进程
kill -9 <PID>
```

### 问题2：命令无响应
1. 检查JSON格式是否正确
2. 查看服务器终端的输出
3. 查看日志文件

### 问题3：机器人未连接
新功能可以独立测试，不需要机器人实际连接：
- BOTTLE_GET命令可以直接使用
- 其他命令会尝试连接机器人

**详细文档**: `NEW_FUNCTION_README.md`

