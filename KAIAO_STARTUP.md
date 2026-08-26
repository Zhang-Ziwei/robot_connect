# KAIAO 启动与联调

在程序根目录操作。确认 `programs/KAIAO/robot_config.json` 中 `active_project` 为 `KAIAO`，机器人 IP/端口正确。

## 1. 启动主程序

```bash
cd /path/to/robot_connect
conda activate robot_connect   # 若使用 conda
python main.py
```

启动后 HTTP 监听 `8090`，控制台提示等待 `START_WORKING`。

## 2. 激活系统

另开一个终端，在根目录执行：

```bash
curl -X POST http://localhost:8090 \
  -H 'Content-Type: application/json' \
  -d @test_commands/START_WORKING_command.json
```

等待主程序连接机器人成功（出现连接成功日志）后再发业务命令。

## 3. 逐步下发测试命令（推荐）

根目录脚本会按顺序发送命令，**每条之间用 `input()` 暂停**，按 Enter 继续下一条：

```bash
python run_kaiao_test_commands.py
```

顺序：

1. `START_WORKING_command.json`
2. `KAIAO_PICK_COMPONENT_TO_SP_command.json`
3. `KAIAO_PICK_BOX_TO_SP_command_1.json` … `_6.json`

跳过激活（主程序已 START_WORKING）：

```bash
python run_kaiao_test_commands.py --skip-start
```

## 4. 手动 curl（等价步骤）

```bash
# 激活
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/START_WORKING_command.json

# 零件分拣
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_COMPONENT_TO_SP_command.json

# 搬箱 1～6（每条执行完、任务空闲后再发下一条）
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_1.json
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_2.json
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_3.json
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_4.json
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_5.json
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/KAIAO_PICK_BOX_TO_SP_command_6.json
```

异步任务需等上一任务完成（或主程序不再 busy）再发下一条，否则可能返回忙碌。

## 5. 查询任务状态（可选）

```bash
curl -X POST http://localhost:8090 -H 'Content-Type: application/json' \
  -d @test_commands/GET_TASK_STATE_command.json
```
