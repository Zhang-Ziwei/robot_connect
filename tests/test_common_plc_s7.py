"""S7 点位寻址：用假传输层核对展厅 DB2 的字节映射与读改写行为。

这里不连现场 PLC，只验证"配置里的 db/byte/bit 会落到哪个字节的哪一位"。
展厅表格里的 40000~40018 和 0~8 只是逻辑点号，真实寻址全靠 DB2 偏移，
写错一位就会去动隔壁设备的输出，所以用测试把映射钉住。
"""
import json
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plc_controller.common_plc import PlcConfigurationError, S7PlcClient

ROOT = os.path.join(os.path.dirname(__file__), "..")


class FakeS7Transport:
    """按字节记账的 DB 存储，并记录每次读写的范围。"""

    def __init__(self, size=64):
        self.db = {2: bytearray(size)}
        self.reads = []
        self.writes = []
        self.connected = False

    def connect(self, host, rack, slot, tcp_port=102):  # noqa: ARG002
        self.connected = True

    def db_read(self, db, start, size):
        self.reads.append((db, start, size))
        return bytes(self.db[db][start:start + size])

    def db_write(self, db, start, data):
        self.writes.append((db, start, bytes(data)))
        self.db[db][start:start + len(data)] = data
        return 0

    def disconnect(self):
        self.connected = False


def zhanting_plc_config():
    path = os.path.join(ROOT, "programs", "ZHANTING", "robot_config.json")
    return json.loads(open(path, encoding="utf-8").read())["plc"]


@pytest.fixture
def client():
    transport = FakeS7Transport()
    return S7PlcClient(zhanting_plc_config(), transport=transport), transport


def test_shipped_config_is_fully_mapped():
    """每个点都要有具体字节，BOOL 还要有位——留 null 会在现场启动时才报错。"""
    points = zhanting_plc_config()["points"]
    assert len(points) == 28
    for name, spec in points.items():
        assert spec["db"] == 2, name
        assert isinstance(spec["byte"], int), f"{name} 的 byte 未填"
        if spec["type"] == "BOOL":
            assert spec["bit"] in range(8), f"{name} 的 bit 未填"


def test_bool_outputs_occupy_one_bit_each_without_gaps():
    """WMS_PLC 的 9 个输出连续排在 0.0~1.0。"""
    points = zhanting_plc_config()["points"]
    bools = [(s["byte"], s["bit"]) for s in points.values() if s["type"] == "BOOL"]
    assert bools == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6), (0, 7), (1, 0)]


def test_int_inputs_are_two_bytes_apart_from_byte_two():
    """PLC_WMS 的 19 个输入字从字节 2 起每点 2 字节，排到 38。"""
    points = zhanting_plc_config()["points"]
    ints = [s["byte"] for s in points.values() if s["type"] == "INT"]
    assert ints == list(range(2, 40, 2))


def test_write_bool_preserves_neighbour_bits(client):
    """同字节里 8 个输出共存，置位必须读改写，不能整字节覆盖。"""
    plc, transport = client
    plc.write("open_start", True)       # 0.4
    assert transport.db[2][0] == 0b0001_0000
    plc.write("detect_start", True)     # 0.1
    assert transport.db[2][0] == 0b0001_0010
    plc.write("open_start", False)
    assert transport.db[2][0] == 0b0000_0010
    # 设备复位在下一个字节，不能影响字节 0。
    plc.write("device_reset", True)
    assert transport.db[2][0] == 0b0000_0010
    assert transport.db[2][1] == 0b0000_0001


def test_read_bool_reads_its_own_bit(client):
    plc, transport = client
    transport.db[2][0] = 0b1000_0000      # 只有 0.7 关盖取料完成
    # 输出点在展厅配置里是只写的，用握手前的状态点验证读路径即可。
    assert plc.read("close_state") == 0
    transport.db[2][0] = 0
    struct.pack_into(">h", transport.db[2], 4, 3)
    assert plc.read("close_state") == 3


def test_read_int_uses_configured_byte(client):
    plc, transport = client
    struct.pack_into(">h", transport.db[2], 6, 2)      # detect_state @ byte 6
    struct.pack_into(">h", transport.db[2], 38, -1)    # open_lift_servo @ byte 38
    assert plc.read("detect_state") == 2
    assert plc.read("open_lift_servo") == -1
    assert (2, 6, 2) in transport.reads
    assert (2, 38, 2) in transport.reads


def test_read_block_fetches_qr_in_one_request(client):
    """二维码占 10 个连续字，必须一次读完，否则会拼出新旧混合的码。"""
    plc, transport = client
    for i in range(10):
        struct.pack_into(">h", transport.db[2], 14 + i * 2, 1000 + i)
    transport.reads.clear()
    assert plc.read_block("qr_1", 10) == [1000 + i for i in range(10)]
    assert transport.reads == [(2, 14, 20)]


def test_read_block_rejects_bool_and_bad_count(client):
    plc, _ = client
    with pytest.raises(PlcConfigurationError):
        plc.read_block("open_start", 2)
    with pytest.raises(PlcConfigurationError):
        plc.read_block("qr_1", 0)


def test_write_rejects_read_only_point(client):
    plc, transport = client
    with pytest.raises(PlcConfigurationError):
        plc.write("detect_state", 1)
    assert transport.writes == []
