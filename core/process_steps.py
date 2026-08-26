import threading
# from constants import PLCHoldingRegisters, PLCCoils  # PLC功能暂时不需要
import time

# 机器人A步骤函数
def a_step1(robot_a, type):
    """从料箱抓取瓶子扫码然后放到开瓶器"""
    print("\n===== Robot A Step 1: Grab bottle from bin, scan, place on opener =====")
    return robot_a.send_service_request("/get_strawberry_service", "pick_from_box", type)

def a_step_pick_box(robot_a, type, maxtime):
    """从料箱抓取瓶子扫码然后放到开瓶器"""
    print("\n===== Robot A a_step_pick_box: Grab bottle from bin, scan, place on opener =====")
    return robot_a.send_service_request("/get_strawberry_service", "pick_box", type, maxtime)

def a_step_place_box(robot_a, type):
    """从料箱抓取瓶子扫码然后放到开瓶器"""
    print("\n===== Robot A a_step_place_box: Grab bottle from bin, scan, place on opener =====")
    return robot_a.send_service_request("/get_strawberry_service", "place_box", type)

def a_step2(robot_a):
    """从开瓶器抓取瓶子后放到桌面上"""
    print("\n===== Robot A Step 2: Grab from opener and place on table =====")
    return robot_a.send_service_request("/get_strawberry_service", "place_to_table")

def a_step3(robot_a):
    """从桌面放到开瓶器上关盖"""
    print("\n===== Robot A Step 3: Place bottle on opener for capping =====")
    return robot_a.send_service_request("/get_strawberry_service", "place_to_equipment")

'''  # PLC功能暂时不需要
def a_step4(robot_a, plc_server):
    """从开瓶器抓取后放到箱子里"""
    print("\n===== Robot A Step 4: Place bottle from opening to box =====")
    # 先等待关盖完成（状态3）
    if not plc_server.wait_for_state(PLCHoldingRegisters.CLOSE_LID_STATE, 3):
        return False
    # 调用机器人服务
    else:
        return robot_a.send_service_request("/get_strawberry_service", "place_to_shelf")
'''  # PLC功能暂时不需要

def a_step_cback2shelf(robot_a):
    """把箱子搬到货架旁"""
    print("\n===== Robot A Step cback2shelf: Pick box to the shelf =====")
    return robot_a.send_service_request("/get_strawberry_service", "catch_box_back", 0)

def a_step_pbox2shelf(robot_a):
    """把箱子放到货架"""
    print("\n===== Robot A Step pbox2shelf: Place box on the shelf =====")
    return robot_a.send_service_request("/get_strawberry_service", "place_box_to_shelf", 0)

# 机器人B步骤函数
def b_step1(robot_b):
    """抓取瓶子后倒液并将试管1放到转盘上"""
    print("\n===== Robot B Step 1: Handle Test Tube 1 =====")
    return robot_b.send_service_request("/get_halfbodychemical_service", "pure_water", 1)

def b_step2(robot_b):
    """抓取瓶子后倒液并将试管2放到转盘上"""
    print("\n===== Robot B Step 2: Handle Test Tube 2 =====")
    return robot_b.send_service_request("/get_halfbodychemical_service", "pure_water", 2)

def b_step3(robot_b):
    """把样品瓶放回原位并归位"""
    print("\n===== Robot B Step 3: Return sample bottle =====")
    return robot_b.send_service_request("/get_halfbodychemical_service", "place_reagent_bottle")

'''  # PLC功能暂时不需要
def b_step4(robot_b, plc_server):
    """将试管1放到清洗设备上"""
    print("\n===== Robot B Step 4: Move Test Tube 1 to cleaning =====")
    # 先等待检测模块请求取走第一个样品（状态4）
    if not plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 4):
        return False
    return robot_b.send_service_request("/get_halfbodychemical_service", "pour_out_clean", 1)
'''  # PLC功能暂时不需要

def b_step5(robot_b):
    """将试管1从清洗设备上拿到试管架上"""
    print("\n===== Robot B Step 5: Move Test Tube 1 to rack =====")
    return robot_b.send_service_request("/get_halfbodychemical_service", "take_tube_rack", 1)

'''  # PLC功能暂时不需要
def b_step6(robot_b, plc_server):
    """将试管2放到清洗设备上"""
    print("\n===== Robot B Step 6: Move Test Tube 2 to cleaning =====")
    # 先等待检测模块请求取走第二个样品（状态5）
    if not plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 5):
        return False
    return robot_b.send_service_request("/get_halfbodychemical_service", "pour_out_clean", 2)
'''  # PLC功能暂时不需要

def b_step7(robot_b):
    """将试管2从清洗设备上拿到试管架上"""
    print("\n===== Robot B Step 7: Move Test Tube 2 to rack =====")
    return robot_b.send_service_request("/get_halfbodychemical_service", "take_tube_rack", 2)

# ============================================================================
# PLC步骤函数 - 暂时不需要，已注释
# ============================================================================
'''
def plc_step1(plc_server):
    """控制开瓶器开盖，等待开盖完成（状态3）"""
    print("\n===== PLC Step 1: Start opening lid =====")
    plc_server.set_coil(PLCCoils.OPEN_START, True)  # PLC本地写入: 线圈 0 (Coil 1) 设置为 True
    # 开始运行：开盖模块状态 (寄存器 0) 改变: 1 → 2，运行结束:开盖模块状态 (寄存器 0) 改变: 2 → 3
    # 线圈 0 (Coil 1) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.OPEN_LID_STATE, 3)

def plc_step2(plc_server):
    """确认开盖完成，设置开盖完成线圈"""
    print("\n===== PLC Step 2: Confirm open finish =====")
    if plc_server.wait_for_state(PLCHoldingRegisters.OPEN_LID_STATE, 3):
        plc_server.set_coil(PLCCoils.OPEN_FINISH, True)     # 线圈 1 (Coil 2) 设置为 True
        # 执行：开盖模块状态 (寄存器 0) 改变: 3 → 0
        # 关盖模块状态 (寄存器 3) 改变: 0 → 1
        # 线圈 1 (Coil 2) 改变: True → False
        # plc run time:0.5, 注意运行时间

def plc_step3(plc_server):
    """控制检测模块放料位移，等待放料完成（状态2）"""
    print("\n===== PLC Step 3: Control detect module dispense =====")
    plc_server.set_coil(PLCCoils.DETECT_DISPENSE, True)     # 线圈 4 (Coil 5) 设置为 True
    # 检测模块状态 (寄存器 2) 改变: 1 → 2
    # 线圈 4 (Coil 5) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 2)

def plc_step4(plc_server):
    """启动检测模块，等待请求取走第一个样品（状态4）"""
    print("\n===== PLC Step 4: Start detection =====")
    plc_server.set_coil(PLCCoils.DETECT_START, True)    # 线圈 5 (Coil 6) 设置为 True
    # 检测模块状态 (寄存器 2) 改变: 2 → 3
    # 线圈 5 (Coil 6) 改变: True → False
    # 检测模块状态 (寄存器 2) 改变: 3 → 4
    return plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 4)

def plc_step5(plc_server):
    """控制检测模块取料位移，等待请求取走第二个样品（状态5）"""
    print("\n===== PLC Step 5: Control detect module pick =====")
    plc_server.set_coil(PLCCoils.DETECT_PICK, True)     # 线圈 6 (Coil 7) 设置为 True
    # 检测模块状态 (寄存器 2) 改变: 4 → 5
    # 线圈 6 (Coil 7) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 5)

def plc_step6(plc_server):
    """启动清洗模块清洗试管1，等待清洗完成（状态3）"""
    print("\n===== PLC Step 6: Start cleaning Test Tube 1 =====")
    plc_server.set_coil(PLCCoils.CLEAN_START, True)     # 线圈 8 (Coil 9) 设置为 True
    # 清洗模块状态 (寄存器 1) 改变: 1 → 2
    # 线圈 8 (Coil 9) 改变: True → False
    # 清洗模块状态 (寄存器 1) 改变: 2 → 3
    return plc_server.wait_for_state(PLCHoldingRegisters.CLEAN_STATE, 3)

def plc_step7(plc_server):
    """启动清洗模块清洗试管2，等待清洗完成（状态3）"""
    print("\n===== PLC Step 7: Start cleaning Test Tube 2 =====")
    if plc_server.wait_for_state(PLCHoldingRegisters.CLEAN_STATE, 1):
        plc_server.set_coil(PLCCoils.CLEAN_START, True)     # 线圈 8 (Coil 9) 设置为 True
        # 清洗模块状态 (寄存器 1) 改变: 1 → 2
        # 线圈 8 (Coil 9) 改变: True → False
        # 清洗模块状态 (寄存器 1) 改变: 2 → 3
    return plc_server.wait_for_state(PLCHoldingRegisters.CLEAN_STATE, 3)

def plc_step8(plc_server):
    """启动关盖模块，等待关盖完成（状态3）"""
    print("\n===== PLC Step 8: Start capping =====")
    plc_server.set_coil(PLCCoils.CLOSE_START, True)     #  线圈 2 (Coil 3) 设置为 True
    # 关盖模块状态 (寄存器 3) 改变: 1 → 2
    # 线圈 2 (Coil 3) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.CLOSE_LID_STATE, 3)

def plc_step9(plc_server):
    """确认关盖完成，设置关盖完成线圈"""
    print("\n===== PLC Step 9: Confirm close finish =====")
    plc_server.set_coil(PLCCoils.CLOSE_FINISH, True)
    return plc_server.wait_for_state(PLCHoldingRegisters.CLOSE_LID_STATE, 0)

def plc_step10(plc_server):
    """确认清洗取料完成，设置清洗完成线圈并等待就绪（状态1）"""
    print("\n===== PLC Step 10: Confirm clean material finish =====")
    plc_server.set_coil(PLCCoils.CLEAN_FINISH, True)    # 线圈 9 (Coil 10) 设置为 True
    # 清洗模块状态 (寄存器 1) 改变: 3 → 1
    # 线圈 9 (Coil 10) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.CLEAN_STATE, 1)

def plc_step11(plc_server):
    """检测模块取料完成，检测模块状态（1-准备就绪）"""
    print("\n===== PLC Step 11: Confirm material retrieval completed =====")
    plc_server.set_coil(PLCCoils.DETECT_FINISH, True)   # 线圈 7 (Coil 8) 设置为 True
    # 检测模块状态 (寄存器 2) 改变: 5 → 1
    # 线圈 7 (Coil 8) 改变: True → False
    return plc_server.wait_for_state(PLCHoldingRegisters.DETECT_STATE, 1)

def plc_step12(plc_server):
    print("\n######PLC复位######")
'''  # PLC功能暂时不需要


'''  # PLC功能暂时不需要
def execute_parallel_tasks(robot_a, robot_b, plc_server):
    """执行并行任务：机器人A和机器人B的后续步骤"""
    task_a_success = [True]  # 使用列表以便在闭包中修改
    
    # 机器人A任务线程
    def task_a():
        # B_step3：机器人B放回样品瓶
        if not b_step3(robot_b):
            task_a_success[0] = False
            return
            
        # A_step3：从桌面放到开瓶器上关盖
        if not a_step3(robot_a):
            task_a_success[0] = False
            return
            
        # PLC_step8：启动关盖模块
        if not plc_step8(plc_server):
            task_a_success[0] = False
            return
            
        # A_step4：从开瓶器抓取后放到桌面上
        if not a_step4(robot_a, plc_server):
            task_a_success[0] = False
            return

        # PLC_step9：确认关盖完成
        if not plc_step9(plc_server):
            task_a_success[0] = False
            return

        # a_step_cback：把箱子搬到货架旁
        if not a_step_cback2shelf(robot_a):
            task_a_success[0] = False
            return

        # A_step4：把箱子放到货架
        if not a_step_pbox2shelf(robot_a):
            task_a_success[0] = False
            return
    
    # 启动机器人A任务线程
    thread_a = threading.Thread(target=task_a)
    thread_a.start()
    
    # 机器人B任务主线程执行
    task_b_success = True
    
    # PLC_step4：启动检测模块
    if not plc_step4(plc_server):
        task_b_success = False
    # B_step4：将试管1放到清洗设备上
    elif not b_step4(robot_b, plc_server):
        task_b_success = False
    # PLC_step6：启动清洗模块清洗试管1
    elif not plc_step6(plc_server):
        task_b_success = False
    # B_step5：将试管1从清洗设备上拿到试管架上
    elif not b_step5(robot_b):
        task_b_success = False
        #input("press enter to continue...")
    # PLC_step10：确认清洗取料完成
    elif not plc_step10(plc_server):
        task_b_success = False
        #input("press enter to continue...")
    # PLC_step5：控制检测模块取料位移
    elif not plc_step5(plc_server):
        task_b_success = False
        #input("press enter to continue...")
    # B_step6：将试管2放到清洗设备上
    elif not b_step6(robot_b, plc_server):
        task_b_success = False
        #input("press enter to continue...")
    # PLC_step11：确认检测取料完成
    elif not plc_step11(plc_server):
        task_b_success = False
        #input("press enter to continue...")
    # PLC_step7：启动清洗模块清洗试管2
    elif not plc_step7(plc_server):
        task_b_success = False
        #input("press enter to continue...")
    # B_step7：将试管2从清洗设备上拿到试管架上
    elif not b_step7(robot_b):
        task_b_success = False
        #input("press enter to continue...")
    # PLC_step10：确认清洗取料完成
    elif not plc_step10(plc_server):
        task_b_success = False
        #input("press enter to continue...")
    
    # 等待机器人A的任务完成
    thread_a.join()
    
    return task_a_success[0] and task_b_success
'''  # PLC功能暂时不需要


'''  # PLC功能暂时不需要
def execute_test_process(robot_b, plc_server, type=1):
    while True:
        if not(b_step1(robot_b)): break
        if not(plc_step3(plc_server)): break
        if not(b_step2(robot_b)): break
        if not(b_step3(robot_b)): break
        if not(plc_step4(plc_server)): break
        if not(b_step4(robot_b,plc_server)): break
        if not(plc_step5(plc_server)): break
        if not(plc_step6(plc_server)): break
        if not(plc_step10(plc_server)): break
        if not(b_step5(robot_b)): break
        if not(plc_step5(plc_server)): break
        if not(b_step6(robot_b,plc_server)): break
        if not(plc_step11(plc_server)): break
        if not(plc_step7(plc_server)): break
        if not(b_step7(robot_b)): break
        #plc_step10(plc_server)
        if(plc_step10(plc_server) and input('是否进入下个循环，输入y/n') == 'n'):
            break
'''  # PLC功能暂时不需要


'''  # PLC功能暂时不需要
def execute_plc_process(plc_server):
    plc_step1(plc_server)
    plc_step2(plc_server)
    plc_step3(plc_server)
    plc_step4(plc_server)
    plc_step6(plc_server)
    plc_step10(plc_server)
    plc_step5(plc_server)
    plc_step11(plc_server)
    plc_step7(plc_server)
    plc_step10(plc_server)
    plc_step8(plc_server)
    plc_step9(plc_server)
'''  # PLC功能暂时不需要


# 主流程函数
'''  # PLC功能暂时不需要
def execute_full_process(robot_a, robot_b, plc_server, type=1):
    # 执行完整流程
    print("\n===== Starting Full Process =====")
    # if not a_step_pick_box(robot_a, 0):
    #     print("\n===== Fail a_step_search =====")
    #     return False
    # else:
    #     print("\n===== Success a_step_search =====")
    if not a_step_pick_box(robot_a, 0, 360):
        print("\n===== Fail a_step_pick_box =====")
        return False
    else:
        print("\n===== Success a_step_pick_box =====")

    if not a_step_place_box(robot_a, 0):
        print("\n===== Fail a_step_place_box =====")
        return False
    else:
        print("\n===== Success a_step_place_box =====")

    time.sleep(1.5)
    # A_step1：机器人A从料箱抓取瓶子放到开瓶器上
    if not a_step1(robot_a, type):
        print("\n===== Fail a_step1 =====")
        return False
    else:
        print("\n===== Success a_step1 =====")
    #input("press enter to continue...")
    # PLC_step1：开瓶器开盖
    if not plc_step1(plc_server):
        print("\n===== Fail plc_step1 =====")
        return False
    else:
        print("\n===== Success plc_step1 =====")
    #input("press enter to continue...")
    # A_step2：机器人A将瓶子放到桌面
    if not a_step2(robot_a):
        print("\n===== Fail a_step2 =====")
        return False
    else:
        print("\n===== Success a_step2 =====")
    #input("press enter to continue...")
    # PLC_step2：PLC确认开盖完成
    plc_step2(plc_server)
    
    # B_step1：机器人B处理试管1
    if not b_step1(robot_b):
        print("\n===== Fail b_step1 =====")
        return False
    else:
        print("\n===== Success b_step1 =====")
    #input("press enter to continue...")
    # PLC_step3：PLC控制检测模块放料
    if not plc_step3(plc_server):
        print("\n===== Fail plc_step3 =====")
        return False
    else:
        print("\n===== Success plc_step3 =====")
    #input("press enter to continue...")
    # B_step2：机器人B处理试管2
    if not b_step2(robot_b):
        print("\n===== Fail b_step2 =====")
        return False
    else:
        print("\n===== Success b_step2 =====")
    #input("press enter to continue...")

    # 并行执行后续任务
    if not execute_parallel_tasks(robot_a, robot_b, plc_server):
        return False
    
    print("\n===== Full Process Completed Successfully =====")
    return True
'''  # PLC功能暂时不需要


'''  # PLC功能暂时不需要
def execute_robotA_test(robot_a, plc_server):
    """机器人A单独测试流程"""
    print("\n===== Starting Robot A Test =====")
    
    # 这里实现机器人A的测试流程
    # A_step1：从料箱抓取瓶子放到开瓶器
    if not a_step_pick_box(robot_a, 0, 240):
        return False
    if not a_step_place_box(robot_a, 0):
        return False
    time.sleep(1.5)
    if not a_step1(robot_a, 1):
        return False
    
    # PLC_step1：开瓶器开盖
    if not plc_step1(plc_server):
        return False
    
    # A_step2：从开瓶器抓取放到桌面
    if not a_step2(robot_a):
        return False
    
    # PLC_step2：确认开盖完成
    plc_step2(plc_server)
    
    # A_step3：从桌面放到开瓶器关盖
    if not a_step3(robot_a):
        return False
    
    # PLC_step8：启动关盖
    if not plc_step8(plc_server):
        return False
    
    # A_step4：从开瓶器抓取放到桌面
    if not a_step4(robot_a, plc_server):
        return False

    # PLC_step9：确认关盖完成
    if not plc_step9(plc_server):
        return False

    # a_step_cback：把箱子搬到货架旁
    if not a_step_cback2shelf(robot_a):
        return False

    # A_step4：把箱子放到货架
    if not a_step_pbox2shelf(robot_a):
        return False

    print("\n===== Robot A Test Completed Successfully =====")
    return True
'''  # PLC功能暂时不需要
