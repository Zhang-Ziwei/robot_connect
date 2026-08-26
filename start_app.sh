#!/bin/bash
set -e  # 脚本执行出错时立即退出，避免静默失败

source /opt/conda/bin/activate
conda activate robot_connect

python main.py

