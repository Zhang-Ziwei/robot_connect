"""
ATC 传送带 Modbus TCP 客户端

本模块封装 ATC 项目专用的传送带 PLC 控制逻辑，作为 Modbus TCP **客户端**
连接真实 PLC（或支持 Modbus TCP 的传送带控制器）。

与 plc_controller/plc_modbus.py 的关系
--------------------------------------
plc_controller/plc_modbus.py  ── Modbus TCP **服务器**，用于模拟 PLC 端（旧项目）。
programs/ATC/plc_modbus.py    ── Modbus TCP **客户端**，连接真实传送带 PLC（本模块）。

两者角色相反，不能互相替代，但底层都依赖 pymodbus 库。
本模块直接使用 pymodbus.client.ModbusTcpClient，无需继承或导入旧项目的 PLCServer。

寄存器协议（由 ATCConveyorRegisters 定义）
------------------------------------------
地址 0  控制寄存器：写入
    0 = 停止   （运动结束后必须写停止，PLC 才会归空闲）
    1 = 正转
    2 = 反转
地址 3  状态寄存器：只读
    0 = 空闲就绪
    1 = 运行中
    2 = 运行完成

使用示例
--------
    from programs.ATC.plc_modbus import ConveyorController

    conveyor = ConveyorController(host="192.168.1.10", port=502)
    conveyor.forward()
    if conveyor.wait_done(timeout=60):
        print("正转完成")
    # wait_done 内部自动写停止归零
"""

import threading
import time
from typing import Optional

from infrastructure.error_logger import get_error_logger
from programs.ATC.constants import ATCConveyorConfig, ATCConveyorRegisters, ATCTimeout

logger = get_error_logger()


class ConveyorController:
    """
    ATC 传送带 PLC 控制器（Modbus TCP 客户端）。

    每次操作独立建立连接（短连接模式），用完即关，避免长连接断线问题。
    所有写操作加互斥锁，支持多线程并发调用安全。

    如果 pymodbus 未安装或 PLC 连接失败，则降级为仅记录日志，不影响主流程。
    """

    def __init__(
        self,
        host: str = ATCConveyorConfig.HOST,
        port: int = ATCConveyorConfig.PORT,
    ):
        self._host = host
        self._port = port
        self._lock = threading.Lock()

    # ── 底层连接 ──────────────────────────────────────────────────────────────

    def _get_client(self) -> Optional[object]:
        """创建并连接 Modbus TCP 客户端，失败返回 None"""
        try:
            from pymodbus.client import ModbusTcpClient  # noqa: PLC0415
            client = ModbusTcpClient(self._host, port=self._port)
            if not client.connect():
                logger.error("ConveyorController", f"无法连接 PLC {self._host}:{self._port}")
                return None
            return client
        except ImportError:
            logger.error("ConveyorController", "pymodbus 未安装，跳过传送带操作")
            return None
        except Exception as e:
            logger.error("ConveyorController", f"连接 PLC 异常: {e}")
            return None

    # ── 寄存器读写 ────────────────────────────────────────────────────────────

    def _write_ctrl(self, value: int) -> bool:
        """向控制寄存器（地址 CTRL）写入指令值，线程安全"""
        with self._lock:
            client = self._get_client()
            if client is None:
                return False
            try:
                resp = client.write_register(ATCConveyorRegisters.CTRL, value)
                if resp and not resp.isError():
                    logger.info("ConveyorController", f"控制寄存器写入: {value}")
                    return True
                logger.error("ConveyorController", f"写控制寄存器失败: {resp}")
                return False
            except Exception as e:
                logger.error("ConveyorController", f"写控制寄存器异常: {e}")
                return False
            finally:
                client.close()

    def _read_state(self) -> int:
        """读取状态寄存器（地址 STATE），失败返回 -1，线程安全"""
        with self._lock:
            client = self._get_client()
            if client is None:
                return -1
            try:
                resp = client.read_holding_registers(ATCConveyorRegisters.STATE, count=1)
                if resp and not resp.isError() and resp.registers:
                    return resp.registers[0]
                return -1
            except Exception as e:
                logger.error("ConveyorController", f"读状态寄存器异常: {e}")
                return -1
            finally:
                client.close()

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def forward(self) -> bool:
        """
        写正转指令（控制寄存器 = CTRL_FORWARD = 1）。
        返回 True 表示指令发送成功，不等待运动完成（需配合 wait_done 使用）。
        """
        logger.info("ConveyorController", "传送带正转指令")
        return self._write_ctrl(ATCConveyorRegisters.CTRL_FORWARD)

    def reverse(self) -> bool:
        """
        写反转指令（控制寄存器 = CTRL_REVERSE = 2）。
        返回 True 表示指令发送成功，不等待运动完成（需配合 wait_done 使用）。
        """
        logger.info("ConveyorController", "传送带反转指令")
        return self._write_ctrl(ATCConveyorRegisters.CTRL_REVERSE)

    def stop(self) -> bool:
        """
        写停止指令（控制寄存器 = CTRL_STOP = 0）。
        PLC 收到停止后，状态寄存器会从"运行完成"归回"空闲"，供下次指令使用。
        通常由 wait_done 在运动完成后自动调用，外部一般不需要手动调用。
        """
        logger.info("ConveyorController", "传送带停止指令")
        return self._write_ctrl(0)

    def wait_done(self, timeout: float = ATCTimeout.CONVEYOR) -> bool:
        """
        阻塞轮询状态寄存器，直到运动完成（STATE_DONE = 2）或超时。

        完成后自动写停止（CTRL_STOP），使 PLC 归回空闲，供下次指令使用。

        参数:
            timeout: 最长等待秒数，默认取 ATCTimeout.CONVEYOR

        返回:
            True  — 运动正常完成（已自动写停止）
            False — 超时或读取失败（也会尝试写停止）
        """
        deadline = time.time() + timeout
        # 给 PLC 0.5s 启动时间，避免刚写完指令就读到"空闲"误判为完成
        time.sleep(0.5)

        while time.time() < deadline:
            state = self._read_state()
            if state == ATCConveyorRegisters.STATE_DONE:
                logger.info("ConveyorController", "传送带运动完成，写停止归零")
                time.sleep(0.5)
                self.stop()
                return True
            if state == -1:
                logger.warning("ConveyorController", "状态寄存器读取失败，继续等待...")
            time.sleep(0.5)

        logger.error("ConveyorController", f"传送带等待超时（{timeout}s），强制写停止")
        self.stop()
        return False
