# 文件锁功能说明

## 功能概述

实现了文件锁机制，**防止程序被同时运行多次**，确保系统同时只有一个实例在运行。

## 为什么需要文件锁？

在实际使用中，如果程序被意外运行多次，会导致：
- ❌ 多个程序同时控制机器人，造成冲突
- ❌ PLC端口被多次绑定，导致错误
- ❌ 机器人收到重复指令，行为异常
- ❌ 日志文件冲突

**文件锁可以彻底避免这些问题！**

## 工作原理

1. 程序启动时创建锁文件 `robot_control.lock`
2. 使用Linux系统级文件锁（fcntl）
3. 如果锁已被占用，提示并退出
4. 程序退出时自动释放锁并删除锁文件

## 使用演示

### 正常启动（第一次）

```bash
$ python main.py
日志文件: logs/error_log_20251031_143025.txt
Waiting for all connections to be ready...
...
```

✅ 程序正常启动

### 尝试重复启动（第二次）

在另一个终端窗口再次运行：

```bash
$ python main.py
======================================================================
❌ 错误：程序已经在运行中！
======================================================================
正在运行的进程ID: 12345

请检查:
  1. 是否已经在另一个终端窗口运行了此程序
  2. 如果确认程序未运行，可能是上次异常退出
     解决方法: 删除锁文件 'rm robot_control.lock'
======================================================================
```

❌ 程序检测到已在运行，自动退出

## 特性

### ✅ 自动管理
- 启动时自动获取锁
- 退出时自动释放锁
- 异常退出也能正确清理

### ✅ 友好提示
- 清晰的错误信息
- 显示正在运行的进程ID
- 提供解决方案

### ✅ 可靠性高
- 使用系统级文件锁
- 不依赖PID文件
- 即使程序崩溃也能正确处理

## 文件说明

### 1. `file_lock.py` - 文件锁模块

核心功能：
```python
class FileLock:
    def acquire(self)        # 获取锁
    def release(self)        # 释放锁
    def is_running(self)     # 检查是否运行
    def get_running_pid(self)  # 获取运行中的进程ID

def ensure_single_instance(lock_file)  # 确保单实例
```

### 2. `robot_control.lock` - 锁文件

- 程序运行时自动创建
- 包含当前进程ID
- 程序退出时自动删除
- 已添加到 `.gitignore`

## 常见场景

### 场景1：正常使用

```bash
# 终端1 - 启动程序
$ python main.py
# ✅ 正常运行

# 终端2 - 尝试再次启动
$ python main.py
# ❌ 提示程序已在运行，自动退出
```

### 场景2：程序正常退出

```bash
$ python main.py
# ... 运行中 ...
# 按 Ctrl+C 或正常退出
程序锁已释放
# ✅ 锁已释放，可以再次启动
```

### 场景3：程序异常退出

如果程序崩溃或被强制杀死：

```bash
# 检查是否有残留进程
$ ps aux | grep main.py

# 如果没有进程，但提示程序在运行
# 可能是锁文件残留，手动删除
$ rm robot_control.lock

# 然后重新启动
$ python main.py
```

## 故障排查

### 问题1：提示程序在运行，但实际没有

**原因**：程序上次异常退出，锁文件残留

**解决**：
```bash
# 1. 确认程序确实没在运行
ps aux | grep main.py

# 2. 删除锁文件
rm robot_control.lock

# 3. 重新启动
python main.py
```

### 问题2：无法获取锁，提示权限错误

**原因**：没有写入权限

**解决**：
```bash
# 检查当前目录权限
ls -la

# 给予写入权限
chmod +w .
```

### 问题3：想强制运行多个实例

**不推荐！** 但如果确实需要：

**方法1**：使用不同的锁文件名
```python
# 修改 main.py 中的锁文件名
lock = ensure_single_instance("robot_control_2.lock")
```

**方法2**：临时禁用锁检查
```python
# 注释掉 main.py 中的锁检查代码
# lock = ensure_single_instance("robot_control.lock")
# if not lock:
#     sys.exit(1)
```

## 技术细节

### Linux文件锁（fcntl）

```python
import fcntl

# 获取排他锁（非阻塞）
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

# 释放锁
fcntl.flock(fd, fcntl.LOCK_UN)
```

优势：
- ✅ 系统级锁，可靠性高
- ✅ 进程退出自动释放
- ✅ 不需要清理PID文件

### 自动清理机制

```python
import atexit

# 注册退出时自动执行
atexit.register(self.release)
```

即使程序异常退出，也会尝试释放锁。

## 在其他项目中使用

### 方法1：直接使用模块

```python
from file_lock import ensure_single_instance
import sys

def main():
    # 检查单实例
    lock = ensure_single_instance("my_program.lock")
    if not lock:
        sys.exit(1)
    
    # 你的代码
    print("程序运行中...")
    
    # 退出时自动释放
```

### 方法2：使用with语句

```python
from file_lock import FileLock

def main():
    try:
        with FileLock("my_program.lock"):
            # 你的代码
            print("程序运行中...")
    except RuntimeError as e:
        print(f"错误: {e}")
        sys.exit(1)
```

## 测试方法

### 测试1：基本功能

```bash
# 终端1
python main.py

# 终端2（会失败）
python main.py
```

### 测试2：使用测试脚本

```bash
python test_file_lock.py
```

### 测试3：锁释放

```bash
# 启动程序
python main.py

# 按 Ctrl+C 退出

# 确认锁文件已删除
ls -la robot_control.lock
# ls: cannot access 'robot_control.lock': No such file or directory

# 可以再次启动
python main.py
```

## 最佳实践

### ✅ 推荐做法

1. **保持默认配置**：使用默认的锁文件名
2. **正常退出**：使用 Ctrl+C 或正常流程退出
3. **定期检查**：偶尔检查是否有残留锁文件

### ❌ 避免做法

1. 不要强制杀死进程（kill -9）
2. 不要手动编辑锁文件
3. 不要在NFS共享目录使用（锁可能不可靠）

## 常见问题

### Q1: 锁文件在哪里？
**A:** 在程序运行目录下，文件名：`robot_control.lock`

### Q2: 锁文件会提交到Git吗？
**A:** 不会，已添加到 `.gitignore`

### Q3: 如何查看正在运行的程序？
**A:** 
```bash
ps aux | grep main.py
# 或者查看锁文件内容（进程ID）
cat robot_control.lock
```

### Q4: 文件锁会影响性能吗？
**A:** 几乎没有影响，只在启动和退出时操作一次

### Q5: 支持Windows吗？
**A:** 当前版本使用Linux的fcntl，不支持Windows。Windows需要使用msvcrt模块

### Q6: 多台机器可以同时运行吗？
**A:** 可以！文件锁只限制同一台机器的多个实例

## 总结

✅ **已实现功能**
- 防止程序重复运行
- 自动获取和释放锁
- 友好的错误提示
- 可靠的系统级锁

✅ **使用简单**
- 无需配置
- 自动管理
- 开箱即用

✅ **安全可靠**
- 系统级文件锁
- 自动清理机制
- 异常情况处理

现在你的程序已经具备了防重复运行的能力，大大提高了系统的稳定性和安全性！

