"""XINXIANG 线性流程：并行分液+放料位移，再放料/检测/取料。"""

import json
import os
import unittest

from core.flow_engine import FlowEngine
from infrastructure.constants import PLCCoils, PLCHoldingRegisters
from programs.XINXIANG.constants import DETECT_STEPS
from programs.XINXIANG.plc_client import run_detect_step


_FLOW = os.path.join(
    os.path.dirname(__file__), "..", "programs", "XINXIANG", "flows", "xinxiang_main.json",
)


class TestXinxiangFlow(unittest.TestCase):
    def test_detect_steps_match_old_protocol(self):
        self.assertEqual(DETECT_STEPS["dispense"]["coil"], PLCCoils.DETECT_DISPENSE)
        self.assertEqual(DETECT_STEPS["start"]["coil"], PLCCoils.DETECT_START)
        self.assertEqual(DETECT_STEPS["pick"]["coil"], PLCCoils.DETECT_PICK)
        self.assertEqual(DETECT_STEPS["finish"]["coil"], PLCCoils.DETECT_FINISH)
        for spec in DETECT_STEPS.values():
            self.assertEqual(spec["register"], PLCHoldingRegisters.DETECT_STATE)
        self.assertEqual(DETECT_STEPS["dispense"]["set_state"], 1)
        self.assertEqual(DETECT_STEPS["dispense"]["wait_done"], 2)
        self.assertEqual(DETECT_STEPS["start"]["wait_done"], 4)
        self.assertEqual(DETECT_STEPS["pick"]["wait_done"], 5)
        self.assertEqual(DETECT_STEPS["finish"]["wait_done"], 1)
        self.assertEqual(PLCHoldingRegisters.HOLDING_REG_COUNT, 14)
        self.assertEqual(PLCHoldingRegisters.DEVICE_STATUS, 4)      # 4005
        self.assertEqual(PLCHoldingRegisters.DETECT_FLOW, 6)        # 4007
        self.assertEqual(PLCHoldingRegisters.SERVO_CLEAN_ROTATE, 13)  # 4014

    def test_holding_registers_cover_twincat_static(self):
        from programs.XINXIANG.plc_client import XinxiangPlcServer
        server = XinxiangPlcServer("127.0.0.1", 15052)
        self.assertTrue(server.start(listen=False))
        slave = server._slave()
        for addr, value in ((2, 11), (4, 6), (6, 10), (13, 0x7002)):
            slave.setValues(3, addr, [value])
            self.assertEqual(server.read_register(addr), value)
        server.close()

    def test_operations_use_robot_task(self):
        with open(_FLOW, encoding="utf-8") as f:
            graph = json.load(f)
        ops = [n for n in graph["nodes"] if n["type"] == "send_operation"]
        self.assertTrue(ops)
        for node in ops:
            self.assertEqual(node["params"].get("service"), "robot_task")
            self.assertNotIn("halfbody", json.dumps(node["params"]))

    def test_detect_skip_when_disabled(self):
        self.assertTrue(run_detect_step("dispense", 1, skip=True))
        self.assertFalse(run_detect_step("unknown", 1, skip=True))

    def test_server_handshake_waits_for_client_register(self):
        import threading
        from programs.XINXIANG.plc_client import XinxiangPlcServer

        server = XinxiangPlcServer("127.0.0.1", 15052)
        self.assertTrue(server.start(listen=False))
        slave = server._slave()
        self.assertIsNotNone(slave)
        self.assertEqual(server.read_register(PLCHoldingRegisters.DETECT_STATE), 0)

        done = []

        def _run():
            done.append(server.handshake(
                coil=PLCCoils.DETECT_DISPENSE,
                register=PLCHoldingRegisters.DETECT_STATE,
                set_state=1,
                wait_done=2,
                timeout=2,
                label="test-dispense",
            ))

        t = threading.Thread(target=_run)
        t.start()
        t.join(0.4)
        self.assertTrue(t.is_alive(), "握手应卡在等 DETECT_STATE=2")
        self.assertEqual(server.read_register(PLCHoldingRegisters.DETECT_STATE), 1)
        self.assertTrue(bool((slave.getValues(1, PLCCoils.DETECT_DISPENSE, 1) or [0])[0]))
        slave.setValues(3, PLCHoldingRegisters.DETECT_STATE, [2])
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(done, [True])
        server.close()

    def test_bool_signals_reset_when_module_state_matches_table(self):
        import time
        from programs.XINXIANG.plc_client import XinxiangPlcServer

        # (线圈, 状态寄存器, 置位时状态, 复位时状态)
        cases = (
            (PLCCoils.OPEN_START, PLCHoldingRegisters.OPEN_LID_STATE, 1, 2),
            (PLCCoils.OPEN_FINISH, PLCHoldingRegisters.OPEN_LID_STATE, 3, 0),
            (PLCCoils.CLOSE_START, PLCHoldingRegisters.CLOSE_LID_STATE, 1, 2),
            (PLCCoils.CLOSE_FINISH, PLCHoldingRegisters.CLOSE_LID_STATE, 3, 1),
            (PLCCoils.DETECT_DISPENSE, PLCHoldingRegisters.DETECT_STATE, 1, 2),
            (PLCCoils.DETECT_START, PLCHoldingRegisters.DETECT_STATE, 2, 3),
            (PLCCoils.DETECT_PICK, PLCHoldingRegisters.DETECT_STATE, 4, 5),
            (PLCCoils.DETECT_FINISH, PLCHoldingRegisters.DETECT_STATE, 5, 1),
            (PLCCoils.CLEAN_START, PLCHoldingRegisters.CLEAN_STATE, 1, 2),
            (PLCCoils.CLEAN_FINISH, PLCHoldingRegisters.CLEAN_STATE, 3, 1),
        )
        server = XinxiangPlcServer("127.0.0.1", 15053)
        self.assertTrue(server.start(listen=False))
        slave = server._slave()
        try:
            for coil, reg, sent_at, reset_at in cases:
                slave.setValues(3, reg, [sent_at])
                self.assertTrue(server.write_coil(coil, True))
                time.sleep(0.25)
                self.assertTrue(
                    bool((slave.getValues(1, coil, 1) or [0])[0]),
                    f"线圈 {coil} 在状态 {sent_at} 时不应复位",
                )
                slave.setValues(3, reg, [reset_at])
                time.sleep(0.35)
                self.assertFalse(
                    bool((slave.getValues(1, coil, 1) or [0])[0]),
                    f"线圈 {coil} 在状态 {reset_at} 时应由上位机复位",
                )
        finally:
            server.close()

    def test_main_graph_order(self):
        with open(_FLOW, encoding="utf-8") as f:
            graph = json.load(f)
        log = []

        def record(node, ctx):  # noqa: ARG001
            log.append(node.id)
            return True

        from core.flow_engine import SignalBus
        bus = SignalBus()
        bus.fire("PROCESS_BEGINS")
        self.assertEqual(graph.get("start"), "n_wait_begin")
        engine = FlowEngine(graph, handlers={
            "update_step": record,
            "send_operation": record,
            "xinxiang_detect": record,
        }, signal_bus=bus)
        result = engine.run()
        self.assertTrue(result.success, result.message)
        self.assertEqual(log[0], "n_begin")
        self.assertEqual(log[-1], "n_done")
        self.assertLess(log.index("n_place"), log.index("n_dispense"))
        self.assertLess(log.index("n_place"), log.index("n_detect_dispense"))
        self.assertLess(log.index("n_dispense"), log.index("n_detect_start"))
        self.assertLess(log.index("n_detect_dispense"), log.index("n_detect_start"))
        self.assertEqual(
            [n for n in log if n.startswith("n_detect")],
            ["n_detect_dispense", "n_detect_start", "n_detect_pick", "n_detect_finish"],
        )

    def test_adapter_starts_with_process_begins(self):
        from network.flow_api_server import PROJECT_ADAPTERS
        adapter = PROJECT_ADAPTERS["XINXIANG"]()
        buttons = adapter["run_control"]["buttons"]
        self.assertTrue(any(b.get("cmd") == "PROCESS_BEGINS" for b in buttons))
        self.assertIn("START_WORKING", adapter["run_control"]["allowed_commands"])
        self.assertIn("PROCESS_BEGINS", adapter["run_control"]["allowed_commands"])
        labels = [t.get("label") for t in adapter["run_control"]["command_templates"]]
        self.assertIn("PROCESS_BEGINS", labels)
        self.assertTrue(adapter["exclusive_enabled_flow"])
        self.assertEqual(adapter["display_name"], "新乡")


if __name__ == "__main__":
    unittest.main()
