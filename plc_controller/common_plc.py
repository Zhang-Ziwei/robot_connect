"""Named PLC points shared by flow projects.

S7 ``address`` values are documentation labels only: every point needs an
explicit DB/byte mapping (and a bit for BOOL). Modbus addresses are zero-based
wire offsets. Neither protocol guesses offsets from labels such as 40000.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import struct
import threading
import time
from typing import Any, Dict, Optional


# Modbus function codes used when reaching into the local server's data store.
_FC_COIL = 1
_FC_HOLDING = 3


class PlcError(RuntimeError):
    pass


class PlcConfigurationError(PlcError):
    pass


class PlcStopped(PlcError):
    pass


def _integer(value, field, minimum=0, maximum=None):
    if isinstance(value, bool) or value is None or value == "":
        raise PlcConfigurationError(f"{field} 必须明确配置为整数")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlcConfigurationError(f"{field} 必须为整数") from exc
    if str(number) != str(value).strip():
        raise PlcConfigurationError(f"{field} 必须为整数")
    if number < minimum or (maximum is not None and number > maximum):
        raise PlcConfigurationError(f"{field} 超出范围")
    return number


def _seconds(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlcConfigurationError(f"{field} 必须是正数秒数") from exc
    if not math.isfinite(number) or number <= 0:
        raise PlcConfigurationError(f"{field} 必须是正数秒数")
    return number


def bool_value(value):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "0", "1"):
        return value.strip().lower() in ("true", "1")
    raise PlcConfigurationError(f"BOOL 值必须为 true/false 或 0/1，实际为 {value!r}")


def _value(spec, value):
    if spec["type"] == "BOOL":
        return bool_value(value)
    return _integer(value, "INT 值", -32768, 32767)


def validate_config(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Validate without importing protocol libraries or connecting to hardware."""
    if not isinstance(config_dict, dict):
        raise PlcConfigurationError("plc 配置必须为对象")
    cfg = copy.deepcopy(config_dict)
    if cfg.get("enabled") is not True:
        raise PlcConfigurationError("PLC 未启用；请配置连接，或显式使用演练模式")
    protocol = str(cfg.get("protocol") or "modbus_server").lower()
    if protocol not in ("s7", "modbus_server", "modbus_tcp", "memory"):
        raise PlcConfigurationError(f"不支持 PLC 协议 {protocol!r}")
    cfg["protocol"] = protocol
    if protocol != "memory":
        host = str(cfg.get("host") or "").strip()
        if not host:
            raise PlcConfigurationError("plc.host 尚未配置")
        cfg["host"] = host
        cfg["port"] = _integer(cfg.get("port", 102 if protocol == "s7" else 502), "plc.port", 1, 65535)
    cfg["io_timeout"] = _seconds(cfg.get("io_timeout", 3), "plc.io_timeout")
    if protocol == "s7":
        cfg["rack"] = _integer(cfg.get("rack", 0), "plc.rack", 0, 255)
        cfg["slot"] = _integer(cfg.get("slot", 1), "plc.slot", 0, 255)
    elif protocol.startswith("modbus"):
        cfg["unit_id"] = _integer(cfg.get("unit_id", 1), "plc.unit_id", 0, 255)
    points = cfg.get("points")
    if not isinstance(points, dict) or not points:
        raise PlcConfigurationError("plc.points 尚未配置")
    for name, spec in points.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise PlcConfigurationError("PLC 点名必须为非空字符串，点位配置必须为对象")
        prefix = f"plc.points.{name}"
        spec["type"] = str(spec.get("type") or "").upper()
        if spec["type"] not in ("INT", "BOOL"):
            raise PlcConfigurationError(f"{prefix}.type 只支持 INT/BOOL")
        if spec.get("access") not in ("read", "write", "readwrite"):
            raise PlcConfigurationError(f"{prefix}.access 必须明确为 read/write/readwrite")
        if protocol == "s7":
            if str(spec.get("area", "DB")).upper() != "DB":
                raise PlcConfigurationError(f"{prefix} 当前只支持显式 DB 区映射")
            spec["db"] = _integer(spec.get("db"), f"{prefix}.db", 1, 65535)
            spec["byte"] = _integer(spec.get("byte"), f"{prefix}.byte", 0, 65535)
            if spec["type"] == "BOOL":
                spec["bit"] = _integer(spec.get("bit"), f"{prefix}.bit", 0, 7)
        elif protocol.startswith("modbus"):
            spec["address"] = _integer(spec.get("address"), f"{prefix}.address", 0, 65535)
        if "initial" in spec:
            spec["initial"] = _value(spec, spec["initial"])
    return cfg


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise PlcStopped("PLC 操作已停止")


class PlcClient:
    def __init__(self, config):
        self.config = validate_config(config)
        self.points = self.config["points"]
        self._lock = threading.RLock()
        self._command_locks = {}

    def _point(self, name, access):
        if name not in self.points:
            raise PlcConfigurationError(f"未知 PLC 点位 {name!r}")
        spec = self.points[name]
        if spec["access"] not in (access, "readwrite"):
            raise PlcConfigurationError(f"PLC 点位 {name} 不允许 {access}")
        return spec

    def read(self, point):
        raise NotImplementedError

    def read_block(self, point, count):
        """从 point 起连续读 count 个 INT，返回列表。

        成组的数据（例如占 10 个连续字的二维码）必须一次读完：逐点读会在中途
        被 PLC 刷新，拼出新旧混合的值。
        """
        spec = self._point(point, "read")
        if spec["type"] != "INT":
            raise PlcConfigurationError(f"连续读取只支持 INT 点位，{point} 是 {spec['type']}")
        count = _integer(count, "连续读取点数", 1, 128)
        if count == 1:
            return [self.read(point)]
        return self._read_block(spec, count)

    def _read_block(self, spec, count):
        raise PlcConfigurationError(f"{self.config['protocol']} 协议不支持连续读取，请逐点读取")

    def write(self, point, value):
        raise NotImplementedError

    def wait(self, point, value, timeout=120, stop_event=None, poll=0.2):
        spec = self._point(point, "read")
        target = _value(spec, value)
        deadline = time.monotonic() + _seconds(timeout, "timeout")
        interval = _seconds(poll, "poll")
        while True:
            _check_stop(stop_event)
            current = self.read(point)
            _check_stop(stop_event)
            if current == target:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                raise PlcError(f"等待 PLC {point}={target} 超时，当前值={current}")
            if stop_event is not None:
                stop_event.wait(min(interval, left))
            else:
                time.sleep(min(interval, left))

    def handshake(self, command, state=None, ready_value=None, done_value=None,
                  timeout=120, reset_after=True, stop_event=None, poll=0.2,
                  pulse_seconds=0.2):
        command_spec = self._point(command, "write")
        if command_spec["type"] != "BOOL":
            raise PlcConfigurationError("握手 command 必须是 BOOL 输出点")
        if ready_value is not None or done_value is not None:
            state_spec = self._point(state, "read")
            if ready_value is not None:
                ready_value = _value(state_spec, ready_value)
            if done_value is not None:
                done_value = _value(state_spec, done_value)
        duration = _seconds(timeout, "timeout")
        pulse = _seconds(pulse_seconds, "pulse_seconds")
        reset = bool_value(reset_after)
        with self._lock:
            command_lock = self._command_locks.setdefault(command, threading.Lock())
        deadline = time.monotonic() + duration

        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise PlcError(f"PLC 握手 {command} 超时")
            return left

        # Serialize use of the same command, while independent devices remain parallel.
        while not command_lock.acquire(timeout=min(0.05, remaining())):
            _check_stop(stop_event)
        attempted = False
        succeeded = False
        try:
            _check_stop(stop_event)
            if ready_value is not None:
                self.wait(state, ready_value, remaining(), stop_event, poll)
            _check_stop(stop_event)
            attempted = True  # An I/O exception may occur after the PLC applied the write.
            if self.write(command, True) is False:
                raise PlcError(f"PLC 输出 {command} 置位失败")
            if done_value is not None:
                self.wait(state, done_value, remaining(), stop_event, poll)
            else:
                if remaining() < pulse:
                    raise PlcError(f"PLC 握手 {command} 的超时小于输出脉冲宽度")
                if stop_event is not None:
                    stop_event.wait(pulse)
                else:
                    time.sleep(pulse)
            _check_stop(stop_event)
            succeeded = True
            return True
        finally:
            try:
                if attempted and (reset or not succeeded):
                    if self.write(command, False) is False:
                        raise PlcError(f"PLC 输出 {command} 清零失败，需现场确认")
            except Exception as exc:
                raise PlcError(f"PLC 输出 {command} 清零失败，需现场确认: {exc}") from exc
            finally:
                command_lock.release()

    def close(self):
        pass


class S7PlcClient(PlcClient):
    def __init__(self, config, transport=None):
        super().__init__(config)
        self._transport = transport
        self._connected = False

    def _connect(self):
        if self._connected:
            return self._transport
        if self._transport is None:
            try:
                import snap7
            except ImportError as exc:
                raise PlcError("S7 需要安装 python-snap7") from exc
            self._transport = snap7.client.Client()
            # Bound individual socket operations so stop requests are not held indefinitely.
            try:
                from snap7.type import Parameter
            except ImportError:
                from snap7.types import Parameter
            timeout_ms = int(self.config["io_timeout"] * 1000)
            for param in (Parameter.PingTimeout, Parameter.SendTimeout, Parameter.RecvTimeout):
                self._transport.set_param(param, timeout_ms)
        self._transport.connect(self.config["host"], self.config["rack"], self.config["slot"],
                                tcp_port=self.config["port"])
        self._connected = True
        return self._transport

    def _bytes(self, transport, spec, size):
        data = bytearray(transport.db_read(spec["db"], spec["byte"], size))
        if len(data) != size:
            raise PlcError(f"S7 DB{spec['db']} 字节 {spec['byte']} 返回长度错误")
        return data

    def read(self, point):
        spec = self._point(point, "read")
        with self._lock:
            try:
                data = self._bytes(self._connect(), spec, 1 if spec["type"] == "BOOL" else 2)
                return bool(data[0] & (1 << spec["bit"])) if spec["type"] == "BOOL" else struct.unpack(">h", data)[0]
            except Exception as exc:
                raise PlcError(f"读取 S7 {point} 失败: {exc}") from exc

    def _read_block(self, spec, count):
        with self._lock:
            try:
                data = self._bytes(self._connect(), spec, count * 2)
                return [item[0] for item in struct.iter_unpack(">h", bytes(data))]
            except Exception as exc:
                raise PlcError(f"读取 S7 DB{spec['db']} 字节 {spec['byte']} 起 {count} 个字失败: {exc}") from exc

    def write(self, point, value):
        spec = self._point(point, "write")
        value = _value(spec, value)
        # One lock covers connect + read/modify/write, preserving adjacent output bits.
        with self._lock:
            try:
                transport = self._connect()
                if spec["type"] == "BOOL":
                    data = self._bytes(transport, spec, 1)
                    mask = 1 << spec["bit"]
                    data[0] = data[0] | mask if value else data[0] & ~mask
                else:
                    data = bytearray(struct.pack(">h", value))
                result = transport.db_write(spec["db"], spec["byte"], data)
                if result not in (None, 0):
                    raise PlcError(f"db_write 返回错误码 {result}")
                return True
            except Exception as exc:
                raise PlcError(f"写入 S7 {point} 失败: {exc}") from exc

    def close(self):
        with self._lock:
            if self._transport is not None and self._connected:
                self._transport.disconnect()
            self._connected = False


class ModbusServerPlcClient(PlcClient):
    """Adapt the existing XINXIANG local server, retaining its coil reset rules."""

    def __init__(self, config, server=None):
        super().__init__(config)
        self._server = server

    def _connect(self):
        if self._server is None:
            from programs.XINXIANG.plc_client import XinxiangPlcServer
            self._server = XinxiangPlcServer(self.config["host"], self.config["port"], self.config["unit_id"])
        if not self._server.start():
            raise PlcError("Modbus 本地服务器启动失败")
        return self._server

    def read(self, point):
        spec = self._point(point, "read")
        with self._lock:
            server = self._connect()
            if spec["type"] == "INT":
                value = server.read_register(spec["address"])
                if value is None:
                    raise PlcError(f"读取 Modbus {point} 失败")
                return value - 65536 if value > 32767 else value
            with server._lock:
                values = server._slave().getValues(_FC_COIL, spec["address"], 1)
            if not isinstance(values, (list, tuple)) or not values:
                raise PlcError(f"读取 Modbus {point} 失败")
            return bool(values[0])

    def _read_block(self, spec, count):
        with self._lock:
            server = self._connect()
            with server._lock:
                values = server._slave().getValues(_FC_HOLDING, spec["address"], count)
            if not isinstance(values, (list, tuple)) or len(values) != count:
                raise PlcError(f"读取 Modbus 寄存器 {spec['address']} 起 {count} 个失败")
            return [value - 65536 if value > 32767 else value for value in values]

    def write(self, point, value):
        spec = self._point(point, "write")
        value = _value(spec, value)
        with self._lock:
            server = self._connect()
            ok = server.write_coil(spec["address"], value) if spec["type"] == "BOOL" else server.write_register(
                spec["address"], value & 0xFFFF)
            if not ok:
                raise PlcError(f"写入 Modbus {point} 失败")
            return True

    def close(self):
        with self._lock:
            if self._server is not None:
                self._server.close()


class ModbusTcpPlcClient(PlcClient):
    def __init__(self, config, transport=None):
        super().__init__(config)
        self._transport = transport
        self._connected = False

    def _connect(self):
        if self._transport is None:
            try:
                from pymodbus.client import ModbusTcpClient
            except ImportError as exc:
                raise PlcError("Modbus TCP 需要安装 pymodbus") from exc
            self._transport = ModbusTcpClient(self.config["host"], port=self.config["port"],
                                             timeout=self.config["io_timeout"])
        if not self._connected:
            if not self._transport.connect():
                raise PlcError("连接 Modbus TCP PLC 失败")
            self._connected = True
        return self._transport

    def _call(self, method, address, **kwargs):
        fn = getattr(self._connect(), method)
        parameters = inspect.signature(fn).parameters
        # Select before sending: retrying a write after TypeError could trigger it twice.
        unit_key = next((key for key in ("device_id", "slave", "unit") if key in parameters), "slave")
        response = fn(address, **{unit_key: self.config["unit_id"]}, **kwargs)
        if response is None or response.isError():
            raise PlcError(f"Modbus {method} 地址 {address} 失败: {response}")
        return response

    def read(self, point):
        spec = self._point(point, "read")
        with self._lock:
            response = self._call("read_coils" if spec["type"] == "BOOL" else "read_holding_registers",
                                  spec["address"], count=1)
            values = response.bits if spec["type"] == "BOOL" else response.registers
            if not values:
                raise PlcError(f"读取 Modbus {point} 未返回值")
            value = values[0]
            return bool(value) if spec["type"] == "BOOL" else value - 65536 if value > 32767 else value

    def _read_block(self, spec, count):
        with self._lock:
            response = self._call("read_holding_registers", spec["address"], count=count)
            values = response.registers
            if len(values) != count:
                raise PlcError(f"读取 Modbus 寄存器 {spec['address']} 起 {count} 个未返回完整数据")
            return [value - 65536 if value > 32767 else value for value in values]

    def write(self, point, value):
        spec = self._point(point, "write")
        value = _value(spec, value)
        with self._lock:
            self._call("write_coil" if spec["type"] == "BOOL" else "write_register", spec["address"],
                       value=value if spec["type"] == "BOOL" else value & 0xFFFF)
            return True

    def close(self):
        with self._lock:
            if self._transport is not None:
                self._transport.close()
            self._connected = False


class MemoryPlcClient(PlcClient):
    """Explicit simulator; it never fabricates a successful state transition."""

    def __init__(self, config):
        super().__init__(config)
        self.values = {name: spec.get("initial", False if spec["type"] == "BOOL" else 0)
                       for name, spec in self.points.items()}

    def read(self, point):
        self._point(point, "read")
        with self._lock:
            return self.values[point]

    def _read_block(self, spec, count):
        # 内存模拟没有地址空间，按 plc.points 的声明顺序取紧随其后的 INT 点。
        names = list(self.points)
        start = next(i for i, name in enumerate(names) if self.points[name] is spec)
        block = names[start:start + count]
        if len(block) != count or any(self.points[name]["type"] != "INT" for name in block):
            raise PlcConfigurationError(
                f"内存模拟从 {names[start]} 起没有 {count} 个连续声明的 INT 点位")
        with self._lock:
            return [self.values[name] for name in block]

    def write(self, point, value):
        spec = self._point(point, "write")
        with self._lock:
            self.values[point] = _value(spec, value)
            return True


_CLIENTS = {}
_CLIENTS_LOCK = threading.Lock()


def client_from_config(config_dict=None) -> PlcClient:
    if config_dict is None:
        from infrastructure.config_loader import load_config
        config_dict = (load_config() or {}).get("plc") or {}
    cfg = validate_config(config_dict)
    key = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    with _CLIENTS_LOCK:
        if key not in _CLIENTS:
            cls = {"s7": S7PlcClient, "modbus_server": ModbusServerPlcClient,
                   "modbus_tcp": ModbusTcpPlcClient, "memory": MemoryPlcClient}[cfg["protocol"]]
            _CLIENTS[key] = cls(cfg)
        return _CLIENTS[key]
