# 部署流程
sudo apt update && sudo apt install -y wget

wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh

# 赋予权限
chmod +x Miniconda3-latest-Linux-x86_64.sh

# 执行安装（全程按提示操作）
./Miniconda3-latest-Linux-x86_64.sh

source ~/.bashrc    # 刷新配置

conda create -n robot_connect python=3.9

conda activate robot_connect

pip install -r requirement.txt

# 操作流程
cd main.py所在文件夹

conda activate robot_connect

python main.py

# 注意事项
运行程序后即可全流程执行化工实验室流程
流程运行时不要随意退出程序
一台机器人运行完成以后断电会影响其他设备
全部执行完成后会进行加水操作，完成后输入y即可继续运行
