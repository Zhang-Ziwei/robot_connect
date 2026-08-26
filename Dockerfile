# x86_64 架构 Robot Connect Docker 镜像（自启动版本）
# 用于在 x86/amd64 设备上运行机器人控制系统，容器启动后自动运行 main.py
#
# ============================================================================
# 构建命令:
# docker build -t robot_connect2.1/shinaide:amd64 .
# docker save -o robot_connect2.1_shinaide_amd64.tar robot_connect2.1/shinaide:amd64
# scp robot_connect2.1_shinaide_amd64.tar user@<HOST>:/home/user/code/
# 查看实时日志:
# docker logs -f robot_connect2.1_x86
# ============================================================================
# 运行示例:
#
# 1. 基础运行（使用默认配置）:
# docker run -d --name robot_connect2.1 \
#     -p 8090:8090 \
#     -p 8091:8091 \
#     robot_connect2.1/shinaide:amd64
#
# 2. 挂载外部配置目录（推荐，包含 config 和 ssl）:
# docker run -d --name robot_connect2.1_x86 \
#     --restart=unless-stopped \
#     -p 8090:8090 -p 8091:8091 \
#     -v /home/user/code/config:/config \
#     robot_connect2.1/shinaide:amd64
#
# 推荐：加日志轮转，防止设备重启后 docker logs 因 json.log 损坏（\x00）报 "invalid character" 错误:
# docker run -d --name robot_connect2.1_x86 --security-opt seccomp=unconfined --restart=unless-stopped --network host --log-opt max-size=20m --log-opt max-file=3 -v /home/tjuser/Code/config:/config robot_connect2.1/shinaide:amd64
# --security-opt seccomp=unconfined在麒麟系统里运行很重要 config文件放在Code文件夹里面
# 说明：--network host 解决容器内 wss:// 出站握手超时问题（NAT 路由限制）
#       --log-opt 避免 json.log 文件过大，重启断电时损坏段更小，docker logs 更可靠
#
# 3. 完整配置（推荐生产环境）:
# docker run -d --name robot_connect \
#     --restart=always \
#     -p 8090:8090 \
#     -p 8091:8091 \
#     -v /path/to/config:/config \
#     -v /path/to/logs:/app/logs \
#     -e TZ=Asia/Shanghai \
#     robot_connect2.1/shinaide:amd64
#
# 4. 开发调试（前台运行，查看日志）:
# docker run -it --name robot_connect \
#     -p 8090:8090 \
#     -p 8091:8091 \
#     -v /path/to/config:/config \
#     robot_connect2.1/shinaide:amd64
#
# 5. 进入容器调试:
# docker exec -it robot_connect /bin/bash
#
# ============================================================================
# 配置文件优先级:
# 1. /config/robot_config.json （Docker 挂载目录，最高优先级）
# 2. /app/robot_config.json   （容器内）
# 3. 代码默认配置
#
# SSL 证书配置:
# 1. /config/ssl/cert.pem 和 /config/ssl/key.pem （Docker 挂载目录）
# 2. /app/config/ssl/cert.pem 和 /app/config/ssl/key.pem （容器内默认）
#
# ============================================================================
# 外部配置目录结构示例:
# /path/to/config/
# ├── robot_config.json      # 主配置文件
# └── ssl/                   # SSL 证书目录
#     ├── cert.pem           # SSL 证书
#     └── key.pem            # SSL 私钥
#
# ============================================================================
# 外部配置文件示例 (robot_config.json):
# {
#     "robots": {
#         "robot_a": {
#             "host": "192.168.1.100",
#             "port": 9090
#         },
#         "robot_b": {
#             "host": "192.168.1.101",
#             "port": 9090
#         }
#     },
#     "http_server": {
#         "port": 8090
#     },
#     "websocket_server": {
#         "enabled": true,
#         "host": "0.0.0.0",
#         "port": 8091,
#         "ssl_enabled": true,
#         "ssl_cert_file": "/config/ssl/cert.pem",
#         "ssl_key_file": "/config/ssl/key.pem",
#         "default_client_id": "robot_connect_client"
#     },
#     "auto_charging": {
#         "enabled": true,
#         "low_threshold": 0.25,
#         "charging_done_threshold": 0.99,
#         "charging_accept_task_threshold": 0.50
#     }
# }
# ============================================================================

FROM condaforge/miniforge3:latest

# 设置环境变量
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

# 设置工作目录
WORKDIR /app

# 创建配置文件和日志目录（用于外部挂载）
RUN mkdir -p /config/ssl /app/logs

# 创建 conda 环境
RUN conda create -n robot_connect python=3.9 -y && \
    conda clean -afy

# 激活环境并安装依赖
SHELL ["conda", "run", "-n", "robot_connect", "/bin/bash", "-c"]

# 安装核心依赖
RUN pip install --no-cache-dir \
    websockets>=15.0 \
    pymodbus>=3.8.0 \
    requests>=2.32.0 \
    numpy>=2.0.0 \
    pandas>=2.0.0

# 复制项目源代码
COPY . .

# 设置启动脚本权限
RUN chmod +x /app/start_docker.sh

# 暴露 HTTP 和 WebSocket 服务器端口
EXPOSE 8090 8091

# 声明配置文件和日志挂载点
VOLUME ["/config", "/app/logs"]

# 设置入口点为启动脚本
ENTRYPOINT ["/bin/bash", "/app/start_docker.sh"]
