# PLC 客户端消息监控 - 测试说明

## 测试步骤

### 步骤 1: 启动 PLC 服务器

打开终端 1：
```bash
cd /home/zhangziwei/Code/robot_connect
source ~/anaconda3/etc/profile.d/conda.sh
conda activate robot_connect
python main.py
```

您应该看到：
```
╔════════════════════════════════════════════════╗
║   Modbus TCP Server started on 0.0.0.0:1502   ║
║   等待PLC客户端连接...                         ║
║   客户端消息变化将实时显示                     ║
╚════════════════════════════════════════════════╝
PLC: Auto-reset coils thread started
PLC: Monitoring client messages...
[DEBUG] 首次读取线圈数据: [False, False, False, False, False]...
[DEBUG] 首次读取寄存器数据: [0, 0, 0, 0]
```

### 步骤 2: 运行简单测试客户端

打开终端 2：
```bash
cd /home/zhangziwei/Code/robot_connect
source ~/anaconda3/etc/profile.d/conda.sh
conda activate robot_connect
python simple_test_client.py
```

### 步骤 3: 观察服务器输出

在终端 1（服务器端）您应该看到：

```
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 0 → 1
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 1 → 2
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 2 → 3
📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: False → True
📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: True → False
```

## 如果没有看到消息

### 检查清单：

1. **确认服务器正在运行**
   - 检查是否看到 "Modbus TCP Server started"
   - 检查是否看到 "PLC: Monitoring client messages..."

2. **确认客户端连接成功**
   - 客户端应该显示 "✓ 已连接到PLC服务器"
   - 如果连接失败，检查端口是否正确（1502）

3. **查看调试信息**
   - 服务器应该显示 "[DEBUG] 首次读取线圈数据..."
   - 服务器应该每5秒显示 "[DEBUG] 监控线程运行中..."

4. **检查防火墙**
   ```bash
   # 检查端口是否开放
   sudo netstat -tulpn | grep 1502
   ```

5. **手动测试 Modbus 连接**
   ```bash
   # 使用 telnet 测试
   telnet localhost 1502
   ```

## 调试模式

如果需要关闭详细调试信息，编辑 `plc_modbus.py` 第 112 行：
```python
debug_mode = False  # 改为 False
```

## 预期的完整输出示例

### 服务器端（终端 1）：
```
╔════════════════════════════════════════════════╗
║   Modbus TCP Server started on 0.0.0.0:1502   ║
║   等待PLC客户端连接...                         ║
║   客户端消息变化将实时显示                     ║
╚════════════════════════════════════════════════╝
PLC: Starting Modbus TCP server on 0.0.0.0:1502
PLC: Waiting for client connections...
PLC: Auto-reset coils thread started
PLC: Monitoring client messages...
[DEBUG] 首次读取线圈数据: [False, False, False, False, False]...
[DEBUG] 首次读取寄存器数据: [0, 0, 0, 0]

===== PLC Step 1: Start opening lid =====
📤 PLC本地写入: 线圈 0 (Coil 1) 设置为 True
Waiting for Open Lid Module to reach state 3

[DEBUG] 监控线程运行中... (循环 50)
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 0 → 1
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 1 → 2
📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 2 → 3
📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: False → True
📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: True → False
[DEBUG] 监控线程运行中... (循环 100)
```

### 客户端端（终端 2）：
```
============================================================
简单PLC客户端测试
============================================================

连接到 localhost:1502...
✓ 已连接到PLC服务器

测试1: 写入保持寄存器 0 = 1
  ✓ 写入成功

测试2: 写入保持寄存器 0 = 2
  ✓ 写入成功

测试3: 写入保持寄存器 0 = 3
  ✓ 写入成功

测试4: 写入线圈 1 = True
  ✓ 写入成功

测试5: 写入线圈 1 = False
  ✓ 写入成功

✓ 所有测试完成

请查看服务器端输出，应该能看到:
  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 0 → 1
  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 1 → 2
  📩 PLC客户端消息: 开盖模块状态 (寄存器 0) 改变: 2 → 3
  📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: False → True
  📩 PLC客户端消息: 线圈 1 (Coil 2) 改变: True → False

客户端已断开
============================================================
```

## 真实 PLC 设备测试

当您连接到真实的 PLC 设备时：

1. 确保 PLC 设备配置：
   - IP: 您的服务器 IP
   - 端口: 1502
   - 从机 ID: 1

2. 从 PLC 设备写入数据后，服务器应该立即显示消息

3. 如果没有显示，检查：
   - PLC 设备是否成功连接（查看 PLC 设备日志）
   - 网络连接是否正常（ping 测试）
   - 端口是否正确

## 常见问题

### Q: 为什么看不到客户端消息？
A: 可能原因：
1. 客户端没有实际写入数据（只是读取）
2. 写入的值和当前值相同（没有变化）
3. 监控线程出错（查看错误日志）

### Q: 如何确认监控线程正在运行？
A: 查看是否有 "[DEBUG] 监控线程运行中..." 消息（每5秒一次）

### Q: 消息显示太多怎么办？
A: 将 `debug_mode = False` 关闭调试模式

## 下一步

测试成功后，您可以：
1. 关闭调试模式（`debug_mode = False`）
2. 连接真实的 PLC 设备
3. 观察实际的生产数据流

