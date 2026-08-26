# 机器人自动重连功能使用指南

## 概述

`RobotController` 类现在支持自动重连功能，可以在以下情况下自动尝试重新连接：
1. 初始连接失败时
2. 连接中断时
3. 发送请求失败时

## 配置参数

### 初始化参数

```python
robot = RobotController(
    host="192.168.217.100",      # 机器人IP地址
    port="9091",                  # 机器人端口
    robot_type=RobotType.ROBOT_A, # 机器人类型
    max_retry_attempts=None,      # 最大重试次数（可选）
    retry_interval=5              # 重试间隔秒数（可选）
)
```

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_retry_attempts` | int 或 None | None | 最大重试次数。`None` 表示无限重试，数字表示最多重试该次数 |
| `retry_interval` | int/float | 5 | 每次重试之间的等待时间（秒） |

## 使用场景

### 场景1：无限重试（推荐用于生产环境）

适用于需要长时间运行、机器人可能临时断电或重启的场景：

```python
robot_a = RobotController(
    "192.168.217.100", 
    "9091", 
    RobotType.ROBOT_A,
    max_retry_attempts=None,  # 无限重试
    retry_interval=5          # 每5秒重试一次
)

# 连接会一直尝试直到成功
robot_a.connect()
```

### 场景2：有限重试（适合快速失败）

适用于测试环境或需要快速知道连接失败的场景：

```python
robot_b = RobotController(
    "192.168.217.80", 
    "9090", 
    RobotType.ROBOT_B,
    max_retry_attempts=10,    # 最多重试10次
    retry_interval=3          # 每3秒重试一次
)

# 最多尝试10次后放弃
if not robot_b.connect():
    print("连接失败，请检查机器人状态")
```

### 场景3：快速重试

适用于网络状态良好但需要快速建立连接的场景：

```python
robot_c = RobotController(
    "192.168.217.120", 
    "9091", 
    RobotType.ROBOT_A,
    max_retry_attempts=20,    # 重试20次
    retry_interval=1          # 每1秒重试一次
)
```

## 自动重连行为

### 初始连接重试

调用 `connect()` 方法时：
- 如果连接失败，会自动按照配置的间隔重试
- 显示详细的重试进度信息
- 达到最大重试次数后返回 `False`（如果配置了限制）

```python
# 这个调用会自动重试
connected = robot_a.connect()
```

### 通信失败自动重连

在发送请求时如果检测到连接断开：
- 自动尝试重新连接
- 重连成功后会提示用户重新发送请求
- 重连失败则返回 `False`

```python
# 即使连接中断，也会自动尝试重连
success = robot_a.send_service_request("/control_service", "move")
if not success:
    # 可以再次尝试，可能已经重连成功
    success = robot_a.send_service_request("/control_service", "move")
```

## 连接状态监控

系统会显示详细的连接诊断信息：

```
============================================================
正在连接 Robot A (192.168.217.100:9091)...
重试策略：无限重试，间隔 5 秒
============================================================

=== Robot A 网络诊断 ===
目标地址: 192.168.217.100:9091
✓ DNS 解析成功: 192.168.217.100 -> 192.168.217.100
正在测试 TCP 连接到 192.168.217.100:9091...
✗ 连接被拒绝 - 端口 9091 上没有服务监听
✗ Robot A 连接失败
⏳ 等待 5 秒后重试...

[重试 2/∞] 尝试连接 Robot A...
```

## 常见问题

### Q1: 机器人暂时断电，程序会怎样？
**A:** 程序会持续重试连接，显示重试进度。当机器人恢复供电后，会自动连接成功。

### Q2: 如何停止无限重试？
**A:** 使用 `Ctrl+C` 中断程序，或者在初始化时设置 `max_retry_attempts` 为具体数字。

### Q3: 重试间隔应该设置多长？
**A:** 
- **生产环境**：推荐 5-10 秒，避免过于频繁的连接尝试
- **测试环境**：可以设置 1-3 秒，快速验证连接
- **机器人启动慢**：设置 10-30 秒，给机器人足够的启动时间

### Q4: 已经连接成功后断开了怎么办？
**A:** 下次调用 `send_service_request()` 时会自动检测并重连。

### Q5: 如何判断是否成功连接？
**A:** 
```python
if robot_a.is_connected():
    print("已连接")
else:
    print("未连接")
```

## 最佳实践

1. **生产环境使用无限重试**：
   ```python
   max_retry_attempts=None, retry_interval=5
   ```

2. **测试时使用有限重试**：
   ```python
   max_retry_attempts=5, retry_interval=2
   ```

3. **在发送重要指令前检查连接状态**：
   ```python
   if not robot_a.is_connected():
       robot_a.connect()
   ```

4. **处理发送失败的情况**：
   ```python
   max_attempts = 3
   for attempt in range(max_attempts):
       if robot_a.send_service_request("/service", "action"):
           break
       print(f"发送失败，重试 {attempt + 1}/{max_attempts}")
       time.sleep(1)
   ```

## 日志输出说明

系统会输出详细的状态信息，包括：
- ✓ 成功操作
- ✗ 失败操作
- ⚠ 警告信息
- ⏳ 等待中
- [DEBUG] 调试信息

这些符号帮助快速识别系统状态。

