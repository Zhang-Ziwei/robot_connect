#!/bin/bash
set -e

echo "========================================"
echo "Robot Connect 自启动容器"
echo "========================================"
echo "启动时间: $(date)"
echo "Python环境: robot_connect (conda)"
echo "HTTP端口: 8090"
echo "WebSocket端口: 8091"
echo "图形化编辑器: 8099"
echo ""

# 检查外部配置文件
echo "检查配置文件..."
if [ -f /config/robot_config.json ]; then
    echo "✓ 检测到外部配置文件: /config/robot_config.json"
    cp /config/robot_config.json /app/robot_config.json
    echo "  已复制到工作目录"
else
    echo "⚠ 未检测到外部配置文件，使用默认配置"
fi

# 检查外部SSL证书
echo ""
echo "检查SSL证书..."
if [ -f /config/ssl/cert.pem ] && [ -f /config/ssl/key.pem ]; then
    echo "✓ 检测到外部SSL证书: /config/ssl/"
    mkdir -p /app/config/ssl
    cp /config/ssl/cert.pem /app/config/ssl/cert.pem
    cp /config/ssl/key.pem /app/config/ssl/key.pem
    echo "  已复制到工作目录"
    echo "  - cert.pem: $(ls -la /config/ssl/cert.pem | awk '{print $5}') bytes"
    echo "  - key.pem: $(ls -la /config/ssl/key.pem | awk '{print $5}') bytes"
elif [ -f /app/config/ssl/cert.pem ] && [ -f /app/config/ssl/key.pem ]; then
    echo "✓ 使用容器内默认SSL证书: /app/config/ssl/"
else
    echo "⚠ 未检测到SSL证书，WebSocket将使用ws://（非加密）"
fi

echo ""
echo "正在启动 main.py..."
echo "========================================"

# 激活conda环境并运行
source /opt/conda/bin/activate
conda activate robot_connect

# 设置Python无缓冲输出
export PYTHONUNBUFFERED=1

# 保持前台运行 (-u 强制无缓冲输出)
exec python -u main.py
