"""
XINXIANG 检测模块：本机作为 Modbus TCP **服务器**（与旧 plc_controller/plc_modbus.PLCServer 相同）。

检测模块是客户端：连进来读我们置位的线圈，再把 DETECT_STATE 写进保持寄存器。
握手在本地数据区完成：本机可先写状态寄存器（如 4003=1），再置位线圈，等对方改成完成值。
`plc.enabled=false` 或演练时不启服务器，节点直接视为成功。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from infrastructure.config_loader import load_config
from infrastructure.constants import PLCCoils, PLCHoldingRegisters
from infrastructure.error_logger import get_error_logger

from programs.XINXIANG.constants import DETECT_STEPS, XinxiangTimeout

logger = get_error_logger()
_LOG = "XINXIANG_PLC"

_FC_COIL = 1
_FC_HOLDING = 3


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def plc_config() -> Dict[str, Any]:
    cfg = (load_config() or {}).get("plc") or {}
    return cfg if isinstance(cfg, dict) else {}


def plc_enabled() -> bool:
    return bool(plc_config().get("enabled"))


_SERVER_LOCK = threading.Lock()
_SERVER: Optional["XinxiangPlcServer"] = None


class XinxiangPlcServer:
    """本机从站。检测模块作为主站连进来读写。"""

    def __init__(self, host: str = "0.0.0.0", port: int = 502, unit_id: int = 1):
        self.host = host or "0.0.0.0"
        self.port = int(port)
        self.unit_id = int(unit_id)
        self._lock = threading.Lock()
        self._running = False
        self._server = None
        self._server_thread: Optional[threading.Thread] = None
        self._reset_thread: Optional[threading.Thread] = None
        self._context = None

    def start(self, listen: bool = True) -> bool:
        if self._context is not None and (not listen or (self._server_thread and self._server_thread.is_alive())):
            self._running = True
            return True
        try:
            from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext  # noqa: PLC0415
            try:
                from pymodbus.datastore import ModbusSlaveContext as DeviceContext  # noqa: PLC0415
            except ImportError:
                from pymodbus.datastore import ModbusDeviceContext as DeviceContext  # noqa: PLC0415
        except ImportError:
            logger.error(_LOG, "pymodbus 未安装，无法启动 PLC 服务器")
            return False

        if self._context is None:
            coils = [0] * max(PLCCoils.COIL_COUNT, 32)
            # 图中 Static 到偏移 26（4014）；+8 给 pymodbus 1-based 寻址留余量
            regs = [0] * max(PLCHoldingRegisters.HOLDING_REG_COUNT + 8, 32)
            store = DeviceContext(
                di=ModbusSequentialDataBlock(0, [0] * 100),
                co=ModbusSequentialDataBlock(0, coils),
                hr=ModbusSequentialDataBlock(0, regs),
                ir=ModbusSequentialDataBlock(0, [0] * 100),
            )
            try:
                self._context = ModbusServerContext(slaves=store, single=True)
            except TypeError:
                self._context = ModbusServerContext(devices=store, single=True)

        if not listen:
            self._running = True
            if self._reset_thread is None or not self._reset_thread.is_alive():
                self._reset_thread = threading.Thread(
                    target=self._auto_reset_coils, daemon=True, name="XINXIANG-plc-reset",
                )
                self._reset_thread.start()
            return True

        identity = self._make_identity()
        self._running = True
        self._server_thread = threading.Thread(
            target=self._serve, args=(identity,), daemon=True, name="XINXIANG-modbus",
        )
        self._server_thread.start()
        time.sleep(0.2)
        if not self._server_thread.is_alive():
            logger.error(_LOG, f"Modbus 服务器未能监听 {self.host}:{self.port}（端口占用或需要 root 时换非特权端口）")
            self._running = False
            return False
        if self._reset_thread is None or not self._reset_thread.is_alive():
            self._reset_thread = threading.Thread(
                target=self._auto_reset_coils, daemon=True, name="XINXIANG-plc-reset",
            )
            self._reset_thread.start()
        logger.info(
            _LOG,
            f"本机已作为 Modbus TCP 服务器监听 {self.host}:{self.port}。"
            f"检测模块应作为客户端连本机（不是我们去连对方）",
        )
        return True

    @staticmethod
    def _make_identity():
        try:
            from pymodbus.device import ModbusDeviceIdentification  # noqa: PLC0415
        except ImportError:
            try:
                from pymodbus.pdu.device import ModbusDeviceIdentification  # noqa: PLC0415
            except ImportError:
                return None
        identity = ModbusDeviceIdentification()
        try:
            identity.VendorName = "XINXIANG"
            identity.ProductName = "Detect handshake server"
        except Exception:
            pass
        return identity

    def _serve(self, identity) -> None:
        address = (self.host, self.port)
        try:
            from pymodbus.server import StartTcpServer  # noqa: PLC0415
            kwargs = {"context": self._context, "address": address}
            if identity is not None:
                kwargs["identity"] = identity
            StartTcpServer(**kwargs)
        except Exception as e:  # noqa: BLE001
            if self._running:
                logger.error(_LOG, f"Modbus 服务器异常 {self.host}:{self.port}: {e}")
            self._running = False

    def _slave(self):
        if self._context is None:
            return None
        try:
            return self._context[self.unit_id]
        except Exception:
            return self._context[1]

    def close(self) -> None:
        self._running = False
        server = self._server
        self._server = None
        if server is not None:
            for meth in ("shutdown", "server_close", "close"):
                fn = getattr(server, meth, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass

    def read_register(self, addr: int) -> Optional[int]:
        slave = self._slave()
        if slave is None:
            return None
        with self._lock:
            try:
                values = slave.getValues(_FC_HOLDING, addr, 1)
            except Exception as e:  # noqa: BLE001
                logger.warning(_LOG, f"读寄存器 {addr} 异常: {e}")
                return None
        if not isinstance(values, (list, tuple)) or not values:
            return None
        return _as_int(values[0], 0)

    def write_coil(self, addr: int, value: bool = True) -> bool:
        slave = self._slave()
        if slave is None:
            logger.error(_LOG, "PLC 服务器未启动，无法写线圈")
            return False
        bit = 1 if value else 0
        with self._lock:
            try:
                slave.setValues(_FC_COIL, addr, [bit])
            except Exception as e:  # noqa: BLE001
                logger.error(_LOG, f"写线圈 {addr}={value} 异常: {e}")
                return False
        logger.info(_LOG, f"本地写线圈 {addr} (Coil {addr + 1}) = {bool(value)}")
        return True

    def write_register(self, addr: int, value: int) -> bool:
        slave = self._slave()
        if slave is None:
            logger.error(_LOG, "PLC 服务器未启动，无法写寄存器")
            return False
        with self._lock:
            try:
                slave.setValues(_FC_HOLDING, addr, [int(value)])
            except Exception as e:  # noqa: BLE001
                logger.error(_LOG, f"写寄存器 {addr}={value} 异常: {e}")
                return False
        logger.info(_LOG, f"本机写寄存器 {addr} (400{addr + 1}) = {int(value)}")
        return True

    def _auto_reset_coils(self) -> None:
        """上位机置位的线圈，在模块状态到达约定值后拉回 False。

        开盖：状态 1 发送、状态 2 复位；状态 3 发送、状态 0 复位。
        关盖：状态 1 发送、状态 2 复位；状态 3 发送、状态 1 复位。
        检测：状态 1→2、2→3、4→5、5→1。
        清洗：状态 1 发送、状态 2 复位；状态 3 发送、状态 1 复位。
        """
        rules = (
            (PLCCoils.OPEN_START, PLCHoldingRegisters.OPEN_LID_STATE, 2),
            (PLCCoils.OPEN_FINISH, PLCHoldingRegisters.OPEN_LID_STATE, 0),
            (PLCCoils.CLOSE_START, PLCHoldingRegisters.CLOSE_LID_STATE, 2),
            (PLCCoils.CLOSE_FINISH, PLCHoldingRegisters.CLOSE_LID_STATE, 1),
            (PLCCoils.DETECT_DISPENSE, PLCHoldingRegisters.DETECT_STATE, 2),
            (PLCCoils.DETECT_START, PLCHoldingRegisters.DETECT_STATE, 3),
            (PLCCoils.DETECT_PICK, PLCHoldingRegisters.DETECT_STATE, 5),
            (PLCCoils.DETECT_FINISH, PLCHoldingRegisters.DETECT_STATE, 1),
            (PLCCoils.CLEAN_START, PLCHoldingRegisters.CLEAN_STATE, 2),
            (PLCCoils.CLEAN_FINISH, PLCHoldingRegisters.CLEAN_STATE, 1),
        )
        while self._running:
            slave = self._slave()
            if slave is None:
                time.sleep(0.1)
                continue
            with self._lock:
                try:
                    for coil, reg, state in rules:
                        coils = slave.getValues(_FC_COIL, coil, 1)
                        regs = slave.getValues(_FC_HOLDING, reg, 1)
                        if not isinstance(coils, (list, tuple)) or not isinstance(regs, (list, tuple)):
                            continue
                        if coils and regs and bool(coils[0]) and _as_int(regs[0], -1) == state:
                            slave.setValues(_FC_COIL, coil, [0])
                            logger.info(_LOG, f"线圈 {coil} 因状态 {state} 自动复位")
                except Exception as e:  # noqa: BLE001
                    logger.warning(_LOG, f"线圈复位监视异常: {e}")
            time.sleep(0.1)

    def wait_register(
        self,
        addr: int,
        want: int,
        timeout: float,
        stop_event: Optional[threading.Event] = None,
        poll: float = XinxiangTimeout.PLC_POLL,
    ) -> bool:
        deadline = None if timeout in (None, "", 0, "0") else time.time() + float(timeout)
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            value = self.read_register(addr)
            if value == want:
                return True
            if deadline is not None and time.time() >= deadline:
                dump = None
                slave = self._slave()
                if slave is not None:
                    raw = slave.getValues(_FC_HOLDING, 0, PLCHoldingRegisters.HOLDING_REG_COUNT)
                    dump = list(raw) if isinstance(raw, (list, tuple)) else raw
                logger.error(
                    _LOG,
                    f"等待寄存器 {addr}={want} 超时，当前={value}；本机数据区 4001 起={dump}",
                )
                return False
            time.sleep(poll)

    def handshake(
        self,
        coil: int,
        register: int,
        wait_done: int,
        timeout: float,
        stop_event: Optional[threading.Event] = None,
        label: str = "",
        set_state: Optional[int] = None,
        wait_ready: Optional[int] = None,
    ) -> bool:
        if self._context is None and not self.start():
            return False
        tag = label or f"coil={coil}"
        remain = None if timeout in (None, "", 0, "0") else float(timeout)
        started = time.time()

        def _left() -> float:
            if remain is None:
                return 0
            return max(0.1, remain - (time.time() - started))

        if set_state is not None:
            logger.info(_LOG, f"{tag}：本机把寄存器 {register} 写成 {int(set_state)}，等对方改成 {wait_done}")
            if not self.write_register(register, int(set_state)):
                return False
        elif wait_ready is not None:
            logger.info(_LOG, f"{tag}：等待寄存器 {register} 已是 {wait_ready}")
            if not self.wait_register(register, int(wait_ready), _left(), stop_event):
                return False
        logger.info(_LOG, f"{tag}：置位线圈 {coil}")
        if not self.write_coil(coil, True):
            return False
        logger.info(_LOG, f"{tag}：等待对方把寄存器 {register} 改成 {wait_done}")
        return self.wait_register(register, int(wait_done), _left(), stop_event)


# 旧名字：曾经误做成客户端。调用方仍可用这个名字。
XinxiangPlcClient = XinxiangPlcServer


def server_from_config() -> Optional[XinxiangPlcServer]:
    global _SERVER
    cfg = plc_config()
    if not cfg.get("enabled"):
        return None
    host = str(cfg.get("host") or "0.0.0.0")
    if host in ("127.0.0.1", "localhost"):
        logger.warning(_LOG, f"plc.host={host} 只听本机，局域网会连接被拒绝，已改为 0.0.0.0")
        host = "0.0.0.0"
    port = _as_int(cfg.get("port"), 502)
    unit_id = _as_int(cfg.get("unit_id"), 1)
    with _SERVER_LOCK:
        if _SERVER is not None:
            _SERVER.host = host
            _SERVER.port = port
            _SERVER.unit_id = unit_id
            _SERVER.start()
            return _SERVER
        server = XinxiangPlcServer(host, port, unit_id)
        server.start()
        _SERVER = server
        return server


def client_from_config() -> Optional[XinxiangPlcServer]:
    return server_from_config()


def run_detect_step(
    step: str,
    timeout: Any,
    stop_event: Optional[threading.Event] = None,
    client: Optional[XinxiangPlcServer] = None,
    skip: bool = False,
) -> bool:
    spec = DETECT_STEPS.get(step)
    if spec is None:
        logger.error(_LOG, f"未知检测步骤 '{step}'")
        return False
    label = spec["label"]
    if skip or not plc_enabled():
        logger.info(_LOG, f"{label}：PLC 未启用或演练，跳过握手视为成功")
        return True
    plc = client
    if plc is None:
        plc = server_from_config()
    if plc is None:
        logger.info(_LOG, f"{label}：没有 PLC 服务器，跳过")
        return True
    timeout_f = XinxiangTimeout.PLC if timeout in (None, "", 0, "0") else float(timeout)
    return plc.handshake(
        coil=spec["coil"],
        register=spec["register"],
        set_state=spec.get("set_state"),
        wait_ready=spec.get("wait_ready"),
        wait_done=spec["wait_done"],
        timeout=timeout_f,
        stop_event=stop_event,
        label=label,
    )
