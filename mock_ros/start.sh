#!/bin/bash
# 启动 mock 服务器（embedded-server 模式）
#
# embedded-server 模式：mock 自身作为 WebSocket 服务端监听 9090，
# main.py 直连 mock，无需 roscore / rosbridge。
# 所有 ROS topic/service 通信在 Python 层以 JSON 处理，不依赖任何 ROS 消息包。
#
# 对比 ros-node 模式（需要 roscore + rosbridge + 目标消息包）：
#   ros-node 模式在 Docker 中因缺少 chem_project_msgs 和 navigation 包而无法正常工作。
#   embedded-server 模式纯 Python 实现，功能等价，更适合 Docker mock 测试。

set -e

echo "======================================================"
echo " Mock 任务服务器启动（embedded-server 模式）"
echo " 监听端口: 9090"
echo " 支持: 任务 Action（/robot_task/*）+ 导航 Action 模拟"
echo "======================================================"

python3 /app/mock_chem_project_action_server.py \
    --mode embedded-server \
    --port 9090 \
    "$@"
