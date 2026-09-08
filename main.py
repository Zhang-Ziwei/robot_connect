import time
import threading
import sys
import json
import os
# from plc_modbus import PLCServer  # PLC功能暂时不需要
from hardware.robot_controller import RobotController
from infrastructure.constants import RobotType, MODBUS_PORT, HTTP_SERVER_PORT, WEBSOCKET_SERVER_PORT, WEBSOCKET_SERVER_ENABLED, get_robot_configs
import core.process_steps
from infrastructure.error_logger import get_error_logger
from infrastructure.file_lock import ensure_single_instance
from infrastructure.console_capture import start_console_capture
from programs.TJSH.cmd_handler import init_cmd_handler, get_cmd_handler
from handlers.dispatcher_base import set_system_activating
from network.http_server import get_http_server
from network.flow_api_server import FlowAPIServer
from infrastructure.config_loader import get_flow_api_server_config
from core.task_queue import get_task_queue
from infrastructure.storage_manager import init_storage_manager, get_storage_manager
from hardware.battery_monitor import init_battery_monitor, get_battery_monitor
from network.websocket_server import init_websocket_server, get_websocket_server
from network.websocket_client import init_websocket_client, get_websocket_client, start_websocket_client, stop_websocket_client

# 机器人实例字典（在收到START_WORKING后初始化）
robots = {}

def main():
    # 检查程序是否已在运行（文件锁）
    lock = ensure_single_instance("robot_control.lock")
    if not lock:
        sys.exit(1)

    # 终端详细输出同时写入 logs/main_console.log，编辑器「主程序输出」页能实时看。
    start_console_capture()
    
    # 初始化错误日志
    logger = get_error_logger()
    logger.info("系统", "机器人控制系统启动")
    print(f"日志文件: {logger.get_log_file()}")
    
    # 初始化存储管理器（后部暂存区状态管理）
    print("\n" + "="*70)
    print("后部暂存区状态管理")
    print("="*70)
    
    # 检查是否存在历史状态文件
    storage_state_file = "storage_state.json"
    if os.path.exists(storage_state_file):
        print(f"检测到历史暂存区状态文件: {storage_state_file}")
        try:
            with open(storage_state_file, 'r', encoding='utf-8') as f:
                existing_storage = json.load(f)
            print("\n当前保存的暂存区状态:")
            print("-" * 60)
            for bottle_type, slots in existing_storage.items():
                occupied = sum(1 for slot in slots if slot != 0)
                print(f"{bottle_type}: {occupied}/{len(slots)} 已占用, 状态: {slots}")
            print("-" * 60)
        except:
            print("无法读取历史状态文件")
        
        # reset_choice = input("\n是否重置暂存区状态为全空？(y/n) [默认: n]: ").strip().lower()
        reset_choice = "y"
        reset_storage = (reset_choice == 'y')
    else:
        print("未检测到历史状态文件，将使用默认配置（全空）")
        reset_storage = True
    
    # 初始化存储管理器
    init_storage_manager(reset=reset_storage)
    storage_mgr = get_storage_manager()
    
    if reset_storage:
        print("\n✓ 暂存区状态已重置为全空")
        logger.info("系统", "暂存区状态已重置")
    else:
        print("\n✓ 已加载历史暂存区状态")
        logger.info("系统", "已加载历史暂存区状态")
    
    # 显示当前状态
    print("\n当前暂存区状态:")
    print(storage_mgr.display_storage_status())
    
    # 初始化组件
    # plc_server = PLCServer()  # PLC功能暂时不需要
    plc_server = None  # PLC功能暂时不需要
    
    # 初始化机器人控制器，配置自动重连参数：
    # max_retry_attempts: None=无限重试, 数字=最大重试次数
    # retry_interval: 重试间隔（秒）
    # 
    # WiFi模式（不需要端口）：
    # robot_a = RobotController("172.16.8.119", robot_type=RobotType.ROBOT_A, max_retry_attempts=None, retry_interval=5)
    # 
    # 有线模式（需要端口）：
    '''robot_a = RobotController(
        "172.16.10.231",
        "9090",
        RobotType.ROBOT_A,
        max_retry_attempts=None,  # 无限重试直到连接成功
        retry_interval=5  # 每5秒重试一次
    )'''
    robot_a = None
    '''robot_b = RobotController(
        "192.168.217.80", 
        "9090", 
        RobotType.ROBOT_B,
        max_retry_attempts=None,  # 无限重试直到连接成功
        retry_interval=5  # 每5秒重试一次
    )'''
    robot_b = None  # 测试模式下不需要robot_b
    
    # 启动PLC服务器
    # plc_server.start_server(port=MODBUS_PORT)  # PLC功能暂时不需要
    # 等待所有连接就绪
    #print("Waiting for all connections to be ready...")
    #time.sleep(2)  # 简单等待，实际应用中可能需要更复杂的检查
    # 连接机器人
    #print("Connecting to robots...")
    #robot_a_connected = robot_a.connect()
    #robot_b_connected = robot_b.connect()
    # 添加HTTP服务器模式
    # 询问运行模式
    print("\n" + "="*70)
    print("选择运行模式:")
    print("1. HTTP服务器模式（接收JSON命令）")
    print("2. 传统流程模式（手动循环）")
    print("3. 测试模式（SCAN_QRCODE测试）")
    print("="*70)
    
    try:
        # mode = input("请选择模式 (1/2/3) [默认: 1]: ").strip() or "1"
        mode = "1"
        if mode == "1":
            # HTTP服务器模式
            run_http_server_mode(robot_a, robot_b, plc_server, logger, lock)
        elif mode == "2":
            # 传统流程模式
            run_traditional_mode(robot_a, robot_b, plc_server, logger, lock)
        else:
            # 测试模式
            run_test_mode(robot_a, logger, lock)

    except KeyboardInterrupt:
        logger.warning("系统", "用户中断程序 (Ctrl+C)")
        print("\n程序被用户中断")
    except Exception as e:
        logger.exception_occurred("系统", "主循环", e)
        print(f"发生错误: {e}")
    finally:
        logger.info("系统", "开始清理资源")
        
        # 显示最终暂存区状态并保存
        try:
            storage_mgr = get_storage_manager()
            print("\n" + "="*70)
            print("最终暂存区状态:")
            print("="*70)
            print(storage_mgr.display_storage_status())
            print(f"\n✓ 状态已保存到: {storage_state_file}")
            logger.info("系统", "暂存区状态已保存")
        except:
            pass  # 如果存储管理器未初始化，跳过
        
        # 关闭机器人连接
        '''try:
            if robot_a and robot_a.is_connected():
                robot_a.close()
                print("✓ 机器人A连接已关闭")
        except:
            pass'''
        # robot_b.close()  # 测试模式下不需要
        # plc_server.stop()  # PLC功能暂时不需要
        logger.info("系统", "机器人控制系统已停止")
        # 释放文件锁
        if lock:
            lock.release()
            print("程序锁已释放")


def run_http_server_mode(robot_a, robot_b, plc_server, logger, lock):
    """HTTP服务器模式 - 支持休眠/激活/重置循环"""
    logger.info("系统", "启动HTTP服务器模式")
    print("\n" + "="*70)
    print("HTTP服务器模式")
    print("="*70)
    
    # 询问是否启用任务队列
    print("\n选择执行模式:")
    print("  1. 同步模式（立即执行，适合单个命令）")
    print("  2. 队列模式（排队执行，适合多个命令）")
    
    # mode_choice = input("请选择模式 (1/2) [默认: 2]: ").strip() or "2"
    mode_choice = "1"
    use_queue = (mode_choice == "2")
    
    # 启动HTTP服务器
    http_server = get_http_server(host='0.0.0.0', port=HTTP_SERVER_PORT)
    http_server.set_command_callback(lambda cmd: get_cmd_handler().handle_command(cmd))
    
    # 如果启用队列模式
    task_queue = None
    if use_queue:
        task_queue = get_task_queue()
        task_queue.start()
        http_server.set_task_queue(task_queue)
        print("\n✓ 任务队列模式已启用")
        print("  - 多个命令会排队执行")
        print("  - 每个命令执行完成后才会执行下一个")
    else:
        print("\n✓ 同步模式已启用")
        print("  - 命令会立即执行（不排队）")
    
    # 先设置一个临时的命令处理器
    init_cmd_handler({})
    
    http_server.start()

    # 图形化编辑器：与命令端口独立，监听 0.0.0.0 以便其他机器用 IP:8099 打开。
    # 端口被占用时只告警，不拖垮主程序（现场可能已有一份编辑器在跑）。
    flow_api_server = None
    flow_cfg = get_flow_api_server_config() or {}
    if flow_cfg.get("enabled", True):
        flow_api_server = FlowAPIServer(host="0.0.0.0")
        try:
            flow_api_server.start()
        except OSError:
            flow_api_server = None
    
    # 启动WebSocket服务器（如果启用）
    websocket_server = None
    if WEBSOCKET_SERVER_ENABLED:
        websocket_server = init_websocket_server()
        if websocket_server:
            websocket_server.set_command_callback(lambda cmd: get_cmd_handler().handle_command(cmd))
            websocket_server.start()
    
    # 初始化WebSocket客户端（主动连接外部服务器）
    websocket_client = init_websocket_client()
    if websocket_client:
        # 设置消息回调（收到服务端推送的消息时处理）
        def handle_server_message(data):
            cmd_type = data.get("cmd_type")
            if cmd_type:
                # 如果是命令消息，交给cmd_handler处理
                result = get_cmd_handler().handle_command(data)
                # 发送响应
                websocket_client.send(result)
            else:
                # 其他消息打印日志
                print(f"📨 收到服务端推送: {data}")
        
        websocket_client.set_message_callback(handle_server_message)
        start_websocket_client()
    
    # 获取本机IP
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "无法获取"
    
    print("\n" + "="*70)
    print("✓ HTTP服务器已启动")
    print("="*70)
    print(f"监听地址: 0.0.0.0:{HTTP_SERVER_PORT} (所有网络接口)")
    if local_ip != "无法获取":
        print(f"本机IP: {local_ip}")
    print(f"模式: {'队列模式' if use_queue else '同步模式'}")
    print("="*70)
    
    if websocket_server:
        print("\n" + "="*70)
        print("✓ WebSocket服务器已启动")
        print("="*70)
        print(f"监听地址: ws://0.0.0.0:{WEBSOCKET_SERVER_PORT}")
        if local_ip != "无法获取":
            print(f"连接地址: ws://{local_ip}:{WEBSOCKET_SERVER_PORT}")
        print("="*70)
    
    if websocket_client:
        print("\n" + "="*70)
        print("✓ WebSocket客户端已启动")
        print("="*70)
        print(f"目标服务器: {websocket_client.uri}")
        print(f"Client ID: {websocket_client.client_id}")
        print("="*70)
    
    print("\n【HTTP测试】")
    print(f"  curl -X POST http://localhost:{HTTP_SERVER_PORT} -H 'Content-Type: application/json' -d @command.json")
    
    if local_ip != "无法获取":
        print(f"\n【远程HTTP】")
        print(f"  curl -X POST http://{local_ip}:{HTTP_SERVER_PORT} -H 'Content-Type: application/json' -d @command.json")
    
    if websocket_server and local_ip != "无法获取":
        print(f"\n【WebSocket连接】")
        print(f"  ws://{local_ip}:{WEBSOCKET_SERVER_PORT}")
    
    if use_queue:
        print("\n【查询队列状态】")
        print(f"  curl http://localhost:{HTTP_SERVER_PORT}/queue/status")
        print("\n【查询任务状态】")
        print(f"  curl http://localhost:{HTTP_SERVER_PORT}/task/<task_id>")
    
    if flow_api_server and flow_api_server.running:
        print("\n" + "="*70)
        print("✓ 图形化流程编辑器已启动")
        print("="*70)
        print(f"监听地址: 0.0.0.0:{flow_api_server.port}")
        if local_ip != "无法获取":
            print(f"浏览器打开: http://{local_ip}:{flow_api_server.port}/")
        print("="*70)
    
    print("\n按 Ctrl+C 停止服务器")
    print("="*70 + "\n")
    
    try:
        # 外层循环：支持重置后重新进入休眠状态
        while True:
            # 进入休眠状态
            _enter_sleep_mode(logger, local_ip)
            
            # 等待START_WORKING或被中断
            cmd_handler = get_cmd_handler()
            
            # 清除之前的重置事件（如果有）
            cmd_handler.reset_system_event.clear()
            
            # 等待START_WORKING事件
            cmd_handler.start_working_event.wait()
            
            # 检查是否是被RESET_SYSTEM触发的（不应该发生，但以防万一）
            if cmd_handler.reset_system_event.is_set():
                logger.info("系统", "检测到重置事件，重新进入休眠状态")
                continue
            
            print("\n" + "="*70)
            print("✅ 已接收START_WORKING命令")
            print("="*70)
            logger.info("系统", "已接收START_WORKING命令，开始连接机器人")
            set_system_activating(True)
            
            # 创建并连接机器人
            global robots
            robots = {}
            connection_aborted = False
            
            # 从配置文件或默认配置获取机器人配置
            robot_configs = get_robot_configs()
            
            if not robot_configs:
                print("\n⚠️  没有启用的机器人，跳过连接步骤")
                logger.info("系统", "没有启用的机器人，跳过连接步骤")
            
            for robot_id, config in robot_configs.items():
                # 检查是否收到重置命令
                if cmd_handler.reset_system_event.is_set():
                    logger.info("系统", "连接过程中收到重置命令，中止连接")
                    connection_aborted = True
                    break
                
                print(f"\n创建机器人 {robot_id}: {config['host']}:{config['port']}")
                robot = RobotController(
                    config["host"],
                    config["port"],
                    config["robot_type"],
                    max_retry_attempts=None,
                    retry_interval=5,
                    navigation_map=config.get("navigation_map"),
                )
                robots[robot_id] = robot
                
                # 在单独线程中连接，以便能够响应重置命令
                print(f"正在连接 {robot_id}...")
                connected = _connect_robot_with_reset_check(robot, cmd_handler)
                
                if cmd_handler.reset_system_event.is_set():
                    logger.info("系统", "连接过程中收到重置命令，中止连接")
                    connection_aborted = True
                    break
                
                if not connected:
                    print(f"❌ {robot_id} 连接失败或被中止")
                    logger.error("系统", f"{robot_id} 连接失败或被中止")
                else:
                    print(f"✅ {robot_id} 连接成功")
                    logger.info("系统", f"{robot_id} 连接成功")
            
            # 如果连接被中止，清理并重新进入休眠
            if connection_aborted:
                set_system_activating(False)
                _cleanup_robots(robots)
                robots = {}
                init_cmd_handler({})
                continue
            
            # 重新初始化命令处理器（使用robots字典）
            # started=False：此时地图尚未下发完成，保持休眠门卫，待下方地图等待结束后再激活
            print("\n重新初始化命令处理器...")
            init_cmd_handler(robots, started=False)
            logger.info("系统", "命令处理器已重新初始化（等待地图就绪后激活）")
            
            # 等待所有已连接机器人的导航地图下发完成再激活系统
            # 目的：防止在地图尚未加载就绪时，业务任务已经触发导航导致失败
            try:
                from infrastructure.constants import (
                    get_navigation_map_runtime_config,
                    NAVIGATION_MAP_SET_WAIT_IDLE_TIMEOUT,
                    NAVIGATION_MAP_SET_SERVICE_TIMEOUT,
                )
                nav_map_cfg = get_navigation_map_runtime_config()
                # 等待时长上限 = wait_idle_timeout + service_timeout + 10s 缓冲
                nav_map_wait_timeout = (
                    nav_map_cfg.get("wait_idle_timeout", NAVIGATION_MAP_SET_WAIT_IDLE_TIMEOUT)
                    + nav_map_cfg.get("service_timeout", NAVIGATION_MAP_SET_SERVICE_TIMEOUT)
                    + 10
                )
            except Exception:
                nav_map_wait_timeout = 120.0
            
            connected_robots = [(rid, r) for rid, r in robots.items() if r.connected]
            if connected_robots:
                print("\n⏳ 等待导航地图下发完成...")
                for rid, r in connected_robots:
                    done = r.wait_for_navigation_map_ready(timeout=nav_map_wait_timeout)
                    if not done:
                        print(f"⚠ {rid} 导航地图下发超时（{nav_map_wait_timeout}s），系统仍将激活但后续导航可能失败")
                        logger.warning("系统", f"{rid} 导航地图下发超时")
                    elif not r._nav_map_ready_success:
                        print(f"⚠ {rid} 导航地图下发未成功: {r._nav_map_ready_message}")
                        logger.warning("系统", f"{rid} 导航地图下发未成功: {r._nav_map_ready_message}")
                    else:
                        msg = r._nav_map_ready_message or "ok"
                        print(f"✓ {rid} 导航地图就绪 ({msg})")
            
            # 地图下发完成（或超时/失败），正式激活系统门卫
            # 此后休眠门卫放行所有命令
            get_cmd_handler().start_working_event.set()
            set_system_activating(False)
            logger.info("系统", "系统门卫已激活，开始接受业务命令")

            # 启动电池监控器
            battery_monitor = init_battery_monitor()
            battery_monitor.set_robots(robots)
            # 设置命令处理器引用和任务状态回调
            cmd_handler = get_cmd_handler()
            battery_monitor.set_cmd_handler(cmd_handler)  # 用于复用导航等待方法
            battery_monitor.set_task_state_callback(
                lambda: cmd_handler.task_state_machine.get_state() if cmd_handler.task_state_machine else None
            )
            battery_monitor.start()
            
            print("\n" + "="*70)
            print("✅ 系统已激活，开始正常工作")
            print("="*70 + "\n")
            
            # 工作循环：检查重置事件
            while True:
                # 每秒检查一次重置事件
                if cmd_handler.reset_system_event.wait(timeout=1.0):
                    # 收到重置事件
                    logger.info("系统", "收到重置命令，停止工作并进入休眠状态")
                    print("\n" + "="*70)
                    print("🔄 收到RESET_SYSTEM命令")
                    print("="*70)
                    
                    # 停止电池监控器
                    if battery_monitor:
                        battery_monitor.stop()
                    
                    # 清理机器人连接
                    _cleanup_robots(robots)
                    robots = {}
                    
                    # 重新初始化空的命令处理器
                    init_cmd_handler({})
                    
                    # 清除重置事件，准备下一轮
                    cmd_handler = get_cmd_handler()
                    cmd_handler.reset_system_event.clear()
                    
                    print("✅ 系统已重置，重新进入休眠状态\n")
                    break  # 跳出工作循环，回到外层循环
                    
    except KeyboardInterrupt:
        print("\n停止服务器...")
    finally:
        # 停止电池监控器
        battery_monitor = get_battery_monitor()
        if battery_monitor:
            battery_monitor.stop()
        
        # 清理机器人连接
        _cleanup_robots(robots)
        
        # 停止HTTP服务器
        if http_server.is_running():
            http_server.stop()

        if flow_api_server:
            flow_api_server.stop()
        
        # 停止WebSocket服务器
        if websocket_server:
            websocket_server.stop()
        
        # 停止WebSocket客户端
        stop_websocket_client()
        
        if use_queue and task_queue:
            task_queue.stop()


def _enter_sleep_mode(logger, local_ip):
    """进入休眠模式，显示等待信息"""
    set_system_activating(False)
    print("\n" + "="*70)
    print("⏸️  程序进入休眠状态")
    print("="*70)
    print("等待接收 START_WORKING 命令...")
    print("（此时机器人未连接，仅HTTP服务器在运行）")
    print(f"\n发送启动命令:")
    print(f"  curl -X POST http://localhost:{HTTP_SERVER_PORT} -H 'Content-Type: application/json' -d @test_commands/START_WORKING_command.json")
    if local_ip != "无法获取":
        print(f"\n远程发送:")
        print(f"  curl -X POST http://{local_ip}:{HTTP_SERVER_PORT} -H 'Content-Type: application/json' -d @test_commands/START_WORKING_command.json")
    print(f"\n发送重置命令:")
    print(f"  curl -X POST http://localhost:{HTTP_SERVER_PORT} -H 'Content-Type: application/json' -d @test_commands/RESET_SYSTEM_command.json")
    print("\n" + "="*70 + "\n")
    
    logger.info("系统", "程序进入休眠状态，等待START_WORKING命令")


def _connect_robot_with_reset_check(robot, cmd_handler):
    """连接机器人，同时检查重置事件"""
    import threading
    
    result = [False]
    connect_done = threading.Event()
    
    def connect_thread():
        try:
            result[0] = robot.connect()
        except:
            result[0] = False
        finally:
            connect_done.set()
    
    # 启动连接线程
    thread = threading.Thread(target=connect_thread, daemon=True)
    thread.start()
    
    # 等待连接完成或重置事件
    while not connect_done.is_set():
        if cmd_handler.reset_system_event.is_set():
            # 收到重置命令，停止机器人重连
            robot.stop_reconnect()
            return False
        connect_done.wait(timeout=0.5)
    
    return result[0]


def _cleanup_robots(robots_dict):
    """清理所有机器人连接（未连接的也要停掉监听重连线程）"""
    for robot_id, robot in list((robots_dict or {}).items()):
        if robot:
            try:
                robot.stop_reconnect()
                robot.close()
            except Exception:
                pass


def run_traditional_mode(robot_a, robot_b, plc_server, logger, lock):
    """传统流程模式"""
    logger.info("系统", "启动传统流程模式")
    print("\n" + "="*70)
    print("传统流程模式")
    print("="*70)
    
    # 启动自动复位线圈线程
    # process_steps.execute_plc_process(plc_server)
    #process_steps.execute_robotA_test(robot_a, plc_server)      # 单独运行机器人A
    #process_steps.execute_test_process(robot_b, plc_server)    # 单独运行机器人B
    
    try:
        cycle_count = 0
        while input('\n是否进入下个循环，输入y/n: ').strip().lower() == 'y':
            cycle_count += 1
            logger.info("系统", f"开始执行第 {cycle_count} 次完整流程")
            try:
                process_steps.execute_full_process(robot_a, robot_b, plc_server)
                logger.info("系统", f"第 {cycle_count} 次完整流程执行完成")
            except Exception as e:
                logger.exception_occurred("系统", f"第{cycle_count}次流程执行", e)
                print(f"执行出错: {e}")
    except KeyboardInterrupt:
        print("\n流程被中断")
        raise
    #if(process_steps.execute_full_process(robot_a, robot_b, plc_server) and input('是否进入下个循环，输入y/n') == 'n'): # 全流程测试
        #process_steps.execute_test_process(robot_b, plc_server)

def run_test_mode(robot_a, logger, lock):
    """测试模式 - 通过HTTP命令调用handle_scan_qrcode"""
    logger.info("系统", "启动测试模式")
    print("\n" + "="*70)
    print("测试模式 - SCAN_QRCODE测试")
    print("="*70)
    
    # 初始化命令处理器（测试模式只需要robot_a，robot_b传None）
    init_cmd_handler(robot_a, None)
    logger.info("系统", "命令处理器初始化完成")
    
    # 启动HTTP服务器（用于接收SCAN_QRCODE_ENTER_ID消息）
    http_server = get_http_server(host='0.0.0.0', port=HTTP_SERVER_PORT)
    http_server.set_command_callback(lambda cmd: get_cmd_handler().handle_command(cmd))
    http_server.start()
    print(f"\n✓ HTTP服务器已启动在端口 {HTTP_SERVER_PORT}")
    print("  可以接收SCAN_QRCODE_ENTER_ID消息\n")
    
    try:
        # 读取SCAN_QR_CODE命令JSON文件
        json_file = "test_commands/SCAN_QR_CODE_command.json"
        print(f"\n读取命令文件: {json_file}")
        
        with open(json_file, 'r', encoding='utf-8') as f:
            cmd_data = json.load(f)
        
        # 调用命令处理器
        cmd_handler = get_cmd_handler()
        result = cmd_handler.handle_command(cmd_data)
        
        # 显示执行结果
        print("\n" + "="*70)
        print("执行结果:")
        print("="*70)
        print(f"成功: {result.get('success')}")
        print(f"消息: {result.get('message')}")
        if 'scanned_count' in result:
            print(f"扫描数量: {result.get('scanned_count')}")
        if 'scanned_bottles' in result:
            print(f"扫描瓶子: {result.get('scanned_bottles')}")
        print("="*70)
        
        logger.info("系统", f"SCAN_QRCODE测试完成 - 结果: {result.get('success')}")
        
    except FileNotFoundError:
        error_msg = f"找不到命令文件: {json_file}"
        print(f"\n✗ 错误: {error_msg}")
        logger.error("系统", error_msg)
        
    except json.JSONDecodeError as e:
        error_msg = f"JSON解析错误: {e}"
        print(f"\n✗ 错误: {error_msg}")
        logger.error("系统", error_msg)
        
    except Exception as e:
        error_msg = f"测试执行出错: {e}"
        print(f"\n✗ 错误: {error_msg}")
        logger.exception_occurred("系统", "测试模式执行", e)
    
    finally:
        # 停止HTTP服务器
        if http_server.is_running():
            http_server.stop()
            print("\n✓ HTTP服务器已停止")
    
    # 启动自动复位线圈线程
    # process_steps.execute_plc_process(plc_server)
    #process_steps.execute_robotA_test(robot_a, plc_server)      # 单独运行机器人A
    
    '''   
    if not robot_a_connected or not robot_b_connected:
        print("Failed to connect to one or more robots")
        plc_server.stop()
        return
    
    # 等待所有连接就绪
    print("Waiting for all connections to be ready...")
    time.sleep(2)  # 简单等待，实际应用中可能需要更复杂的检查
    
    try:
        # 执行完整流程，类型为1
        success = process_steps.execute_full_process(robot_a, robot_b, plc_server, 1)
        
        if success:
            print("Full process executed successfully")
        else:
            print("Full process failed")
            
        # 可以根据需要执行其他测试流程
        # process_steps.execute_robotA_test(robot_a, plc_server)
        
    except KeyboardInterrupt:
        print("Process interrupted by user")
    finally:
        # 清理资源
        robot_a.close()
        robot_b.close()
        plc_server.stop()
        print("All resources cleaned up")
'''
if __name__ == "__main__":
    main()
