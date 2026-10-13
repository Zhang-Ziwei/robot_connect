import threading
import time
from pymodbus.server import StartTcpServer
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.framer import FramerRTU, FramerAscii
from constants import PLCHoldingRegisters, PLCCoils

class PLCServer:
    def __init__(self):
        self.running = True
        self.current_process = 0  # 0-无流程 1-完整流程 2-开盖关盖 3-检测清洗
        
        # 初始化保持寄存器和线圈
        self.holding_registers = [0] * PLCHoldingRegisters.HOLDING_REG_COUNT
        self.coils = [False] * PLCCoils.COIL_COUNT
        
        # 用于检测客户端数据变化的副本
        self.prev_holding_registers = [0] * PLCHoldingRegisters.HOLDING_REG_COUNT
        self.prev_coils = [False] * PLCCoils.COIL_COUNT
        
        self.mutex = threading.Lock()
        self.server_thread = None
        self.auto_reset_thread = None
        
        # 设置Modbus服务器
        self.setup_server()
    
    def _get_register_name(self, reg_idx):
        """获取寄存器的友好名称"""
        register_names = {
            PLCHoldingRegisters.OPEN_LID_STATE: "开盖模块状态",
            PLCHoldingRegisters.CLEAN_STATE: "清洗模块状态",
            PLCHoldingRegisters.DETECT_STATE: "检测模块状态",
            PLCHoldingRegisters.CLOSE_LID_STATE: "关盖模块状态",
            PLCHoldingRegisters.DEVICE_STATUS: "设备状态显示",
            PLCHoldingRegisters.OPEN_CLOSE_FLOW: "开关盖流程",
            PLCHoldingRegisters.DETECT_FLOW: "分析检测流程",
            PLCHoldingRegisters.CLEAN_FLOW: "翻转清洗流程",
            PLCHoldingRegisters.SERVO_OPEN_UD: "开盖上下伺服",
            PLCHoldingRegisters.SERVO_OPEN_TELESCOPE: "开盖伸缩伺服",
            PLCHoldingRegisters.SERVO_DETECT_UD: "检测上下步进",
            PLCHoldingRegisters.SERVO_DETECT_TURNTABLE: "检测转盘伺服",
            PLCHoldingRegisters.SERVO_CLEAN_UD: "清洗上下伺服",
            PLCHoldingRegisters.SERVO_CLEAN_ROTATE: "清洗旋转伺服",
        }
        return register_names.get(reg_idx, f"保持寄存器{reg_idx}")
    
    def setup_server(self):
        """设置Modbus服务器数据存储"""
        # 线圈存储
        coils = ModbusSequentialDataBlock(0, self.coils)
        # 保持寄存器存储
        holding_regs = ModbusSequentialDataBlock(0, self.holding_registers)
        
        # 创建从机上下文
        store = ModbusSlaveContext(
            di=ModbusSequentialDataBlock(0, [0]*100),
            co=coils,
            hr=holding_regs,
            ir=ModbusSequentialDataBlock(0, [0]*100)
        )
        
        # 创建服务器上下文
        self.context = ModbusServerContext(slaves=store, single=True)
        
        # 设备标识
        self.identity = ModbusDeviceIdentification()
        self.identity.VendorName = 'ChemicalIndustry'
        self.identity.ProductCode = 'CI'
        self.identity.VendorUrl = 'http://example.com'
        self.identity.ProductName = 'Chemical Industry Controller'
        self.identity.ModelName = 'CI-1000'
        self.identity.MajorMinorRevision = '1.0'
    
    def start_server(self, host='0.0.0.0', port=502):
        """启动Modbus服务器线程"""
        self.server_thread = threading.Thread(
            target=self.run_server, 
            args=(host, port),
            daemon=True
        )
        self.server_thread.start()
        
        # 启动自动复位线圈线程
        self.auto_reset_thread = threading.Thread(
            target=self.auto_reset_coils,
            daemon=True
        )
        self.auto_reset_thread.start()
    
    def run_server(self, host, port):
        """运行Modbus服务器"""
        try:
            print(f"PLC: Starting Modbus TCP server on {host}:{port}")
            print(f"PLC: Waiting for client connections...")
            
            # 使用 pymodbus 3.x 的同步服务器
            StartTcpServer(
                context=self.context,
                identity=self.identity,
                address=(host, port)
            )
        except Exception as e:
            if self.running:
                print(f"Modbus server error: {e}")
                import traceback
                traceback.print_exc()
    
    def auto_reset_coils(self):
        """自动复位PLC线圈线程函数"""
        print("PLC: Auto-reset coils thread started")
        print("PLC: Monitoring client messages...")
        
        # 添加调试标志
        debug_mode = True  # 设置为 False 可以关闭详细调试
        loop_count = 0
        
        while self.running:
            loop_count += 1
            
            # 每50次循环（5秒）输出一次心跳信息
            if debug_mode and loop_count % 50 == 0:
                print(f"[DEBUG] 监控线程运行中... (循环 {loop_count})")
            with self.mutex:
                # === 第一步：从 Modbus 上下文同步数据到内部数组（读取客户端写入的数据）===
                # 这对应 C++ 代码中的 memcpy 操作
                try:
                    # 读取线圈状态（客户端可能修改了）
                    slave_context = self.context[1]
                    # 注意：pymodbus 的 getValues 返回长度可能比请求的少 1
                    modbus_coils = slave_context.getValues(1, 0, PLCCoils.COIL_COUNT)
                    
                    # 调试：显示读取到的原始数据
                    if debug_mode and loop_count == 1:
                        print(f"[DEBUG] 首次读取线圈数据: {modbus_coils[:5] if modbus_coils else 'None'}...")
                    
                    # 安全检查：确保返回的数据存在
                    if modbus_coils:
                        # 检测线圈变化并显示
                        for i in range(min(len(modbus_coils), PLCCoils.COIL_COUNT)):
                            new_value = bool(modbus_coils[i])
                            if new_value != self.prev_coils[i]:
                                print(f"📩 PLC客户端消息: 线圈 {i} (Coil {i+1}) 改变: {self.prev_coils[i]} → {new_value}")
                                self.prev_coils[i] = new_value
                            self.coils[i] = new_value
                    
                    # 读取保持寄存器（客户端可能修改了）
                    modbus_regs = slave_context.getValues(3, 0, PLCHoldingRegisters.HOLDING_REG_COUNT)
                    
                    # 调试：显示读取到的原始数据
                    if debug_mode and loop_count == 1:
                        print(f"[DEBUG] 首次读取寄存器数据: {modbus_regs if modbus_regs else 'None'}")
                    
                    # 安全检查：确保返回的数据存在
                    if modbus_regs:
                        # 检测保持寄存器变化并显示
                        for i in range(min(len(modbus_regs), PLCHoldingRegisters.HOLDING_REG_COUNT)):
                            new_value = modbus_regs[i]
                            if new_value != self.prev_holding_registers[i]:
                                register_name = self._get_register_name(i)
                                print(f"📩 PLC客户端消息: {register_name} (寄存器 {i}) 改变: {self.prev_holding_registers[i]} → {new_value}")
                                self.prev_holding_registers[i] = new_value
                            self.holding_registers[i] = new_value
                        
                except Exception as e:
                    print(f"PLC: Error reading from Modbus context: {e}")
                    import traceback
                    traceback.print_exc()
                
                # === 第二步：执行自动复位逻辑 ===
                # 1. 开盖模块线圈复位逻辑
                if self.coils[PLCCoils.OPEN_START] and \
                   self.holding_registers[PLCHoldingRegisters.OPEN_LID_STATE] == 2:
                    self.coils[PLCCoils.OPEN_START] = False
                    print("PLC: Coil 1 (open start) reset due to state 2")
                
                if self.coils[PLCCoils.OPEN_FINISH] and \
                   self.holding_registers[PLCHoldingRegisters.OPEN_LID_STATE] == 0:
                    self.coils[PLCCoils.OPEN_FINISH] = False
                    print("PLC: Coil 2 (open finish) reset due to state 1")
                
                # 2. 关盖模块线圈复位逻辑
                if self.coils[PLCCoils.CLOSE_START] and \
                   self.holding_registers[PLCHoldingRegisters.CLOSE_LID_STATE] == 2:
                    self.coils[PLCCoils.CLOSE_START] = False
                    print("PLC: Coil 3 (close start) reset due to state 2")
                
                if self.coils[PLCCoils.CLOSE_FINISH] and \
                   self.holding_registers[PLCHoldingRegisters.CLOSE_LID_STATE] == 0:
                    self.coils[PLCCoils.CLOSE_FINISH] = False
                    print("PLC: Coil 4 (close finish) reset due to state 1")
                
                # 3. 检测模块线圈复位逻辑
                if self.coils[PLCCoils.DETECT_DISPENSE] and \
                   self.holding_registers[PLCHoldingRegisters.DETECT_STATE] == 2:
                    self.coils[PLCCoils.DETECT_DISPENSE] = False
                    print("PLC: Coil 5 (detect dispense) reset due to state 2")
                
                if self.coils[PLCCoils.DETECT_START] and \
                   self.holding_registers[PLCHoldingRegisters.DETECT_STATE] == 3:
                    self.coils[PLCCoils.DETECT_START] = False
                    print("PLC: Coil 6 (detect start) reset due to state 3")
                
                if self.coils[PLCCoils.DETECT_PICK] and \
                   self.holding_registers[PLCHoldingRegisters.DETECT_STATE] == 5:
                    self.coils[PLCCoils.DETECT_PICK] = False
                    print("PLC: Coil 7 (detect pick) reset due to state 5")
                
                if self.coils[PLCCoils.DETECT_FINISH] and \
                   self.holding_registers[PLCHoldingRegisters.DETECT_STATE] == 1:
                    self.coils[PLCCoils.DETECT_FINISH] = False
                    print("PLC: Coil 8 (detect finish) reset due to state 1")
                
                # 4. 清洗模块线圈复位逻辑
                if self.coils[PLCCoils.CLEAN_START] and \
                   self.holding_registers[PLCHoldingRegisters.CLEAN_STATE] == 2:
                    self.coils[PLCCoils.CLEAN_START] = False
                    print("PLC: Coil 9 (clean start) reset due to state 2")
                
                if self.coils[PLCCoils.CLEAN_FINISH] and \
                   self.holding_registers[PLCHoldingRegisters.CLEAN_STATE] == 1:
                    self.coils[PLCCoils.CLEAN_FINISH] = False
                    print("PLC: Coil 10 (clean finish) reset due to state 1")
                
                # === 第三步：将内部数组同步回 Modbus 上下文（写入处理后的数据）===
                # 这对应 C++ 代码中的另一个 memcpy 操作
                try:
                    self.context[1].setValues(1, 0, self.coils)
                    self.context[1].setValues(3, 0, self.holding_registers)
                except Exception as e:
                    print(f"PLC: Error writing to Modbus context: {e}")
            
            time.sleep(0.1)  # 100ms检查一次
        
        print("PLC: Auto-reset coils thread stopped")
    
    def set_coil(self, coil_idx, value):
        """设置PLC线圈值"""
        with self.mutex:
            if 0 <= coil_idx < PLCCoils.COIL_COUNT:
                # 先更新内部数组
                self.coils[coil_idx] = value
                # 更新前一个值，避免被检测为客户端修改
                self.prev_coils[coil_idx] = value
                # 再同步到 Modbus 上下文
                self.context[1].setValues(1, coil_idx, [value])
                print(f"📤 PLC本地写入: 线圈 {coil_idx} (Coil {coil_idx + 1}) 设置为 {value}")
    
    def get_holding_register(self, reg_idx):
        """获取保持寄存器值"""
        with self.mutex:
            if 0 <= reg_idx < PLCHoldingRegisters.HOLDING_REG_COUNT:
                # 先从 Modbus 上下文同步最新数据
                try:
                    modbus_regs = self.context[1].getValues(3, reg_idx, 1)
                    if modbus_regs and len(modbus_regs) > 0:
                        self.holding_registers[reg_idx] = modbus_regs[0]
                except Exception as e:
                    print(f"PLC: Error reading register {reg_idx}: {e}")
                    import traceback
                    traceback.print_exc()
                
                return self.holding_registers[reg_idx]
            return None
    
    def wait_for_state(self, reg_idx, target_state, timeout_seconds=0):
        """等待指定寄存器达到目标状态"""
        from constants import MODULE_NAMES
        
        if reg_idx < 0 or reg_idx >= PLCHoldingRegisters.HOLDING_REG_COUNT:
            print(f"Invalid register index: {reg_idx}")
            return False
            
        print(f"Waiting for {MODULE_NAMES[reg_idx]} to reach state {target_state}")
        
        start_time = time.time()
        
        while self.running:
            print(f"PLC: Waiting for {MODULE_NAMES[reg_idx]} : {reg_idx}")
            current_state = self.get_holding_register(reg_idx)
            if current_state == target_state:
                print(f"{MODULE_NAMES[reg_idx]} reached state {target_state}")
                return True
            
            # 检查超时
            if timeout_seconds > 0 and (time.time() - start_time) >= timeout_seconds:
                print(f"{MODULE_NAMES[reg_idx]} timeout waiting for state {target_state}")
                return False
            
            time.sleep(1)  # 500ms检查一次
        
        return False
    
    def stop(self):
        """停止PLC服务器"""
        self.running = False
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join()
        if self.auto_reset_thread and self.auto_reset_thread.is_alive():
            self.auto_reset_thread.join()
        print("PLC server stopped")
