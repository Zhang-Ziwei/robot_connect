# 错误日志 - 快速开始

## 5分钟上手指南

### 1. 运行程序（无需改动）

```bash
python main.py
```

启动时会看到：
```
日志文件: logs/error_log_20251031_143025.txt
```

### 2. 实时查看日志

打开新终端窗口：
```bash
tail -f logs/error_log_*.txt
```

### 3. 查看错误

```bash
grep "ERROR" logs/error_log_*.txt
```

### 4. 测试日志功能

```bash
python test_error_log.py
```

然后查看生成的日志文件。

## 就这么简单！

✅ 日志自动记录
✅ 无需配置
✅ 开箱即用

## 详细文档

- **使用说明**: 查看 `ERROR_LOG_README.md`
- **实现说明**: 查看 `错误日志功能说明.md`

## 常用命令

```bash
# 实时查看日志
tail -f logs/error_log_*.txt

# 查看所有错误
grep "ERROR" logs/error_log_*.txt

# 查看Robot A的日志
grep "Robot A" logs/error_log_*.txt

# 查看最近50行
tail -n 50 logs/error_log_*.txt

# 清空所有日志
rm -f logs/*.txt
```

## 需要帮助？

查看完整文档：`ERROR_LOG_README.md`

