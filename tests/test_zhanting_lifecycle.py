"""展厅生命周期回归测试：所有设备调用都使用内存替身。"""

import copy
import importlib
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.task_state_machine import TaskStatus
from infrastructure.constants import ErrorCode


project = importlib.import_module("programs.ZHANTING.ZHANTING")


def _wait(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待流程状态超时")
        time.sleep(0.005)


def _graph(*nodes):
    return {
        "flow_id": "test_zhanting",
        "start": nodes[0]["id"],
        "nodes": list(nodes),
        "edges": [{"source": left["id"], "target": right["id"]} for left, right in zip(nodes, nodes[1:])],
    }


def _wait_graph():
    return _graph(
        {"id": "loaded", "type": "wait_for_command", "params": {"event_name": "ZHANTING_LOADED"}},
        {"id": "action", "type": "test_action"},
    )


class TestZhantingLifecycle(unittest.TestCase):
    def setUp(self):
        self.graph = _wait_graph()
        self.action = Mock(return_value=True)
        self.plc = SimpleNamespace(close=Mock())
        self.config_context = {"robot_id": "robot_a", "arm_url": "http://configured/arm"}
        self.prepare = self._patch("_prepare_run", return_value=(self.config_context, self.plc))
        self._patch("get_require_process_begins", return_value=True)
        self._patch("require_one_enabled", side_effect=lambda *_: ("test", copy.deepcopy(self.graph), None))
        self._patch("build_handler_registry", side_effect=lambda *args, **kwargs: {"test_action": self.action})
        self.handler = project.ZhantingHandler({"robot_a": object()})
        self.addCleanup(self.handler.shutdown)

    def _patch(self, name, **kwargs):
        patcher = patch.object(project, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _begin(self):
        result = self.handler.handle_process_begins({"cmd_id": "cycle"})
        self.assertTrue(result["success"], result)
        return result

    def _waiting(self):
        _wait(lambda: self.handler._engine.is_waiting_for("ZHANTING_LOADED"))

    def _finish(self):
        self.handler._engine_thread.join(timeout=2)
        self.assertFalse(self.handler._engine_thread.is_alive())

    def test_constructor_is_lazy_and_loaded_requires_current_waiter(self):
        self.prepare.assert_not_called()
        self.assertFalse(self.handler.handle_loaded({})["success"])
        self._begin()
        self._waiting()
        self.action.assert_not_called()
        self.assertTrue(self.handler.build_command_map()["ZHANTING_LOADED"]({})["success"])
        self._finish()
        self.action.assert_called_once()
        self.assertEqual(self.handler.task_state_machine.status, TaskStatus.COMPLETED)
        self.assertFalse(self.handler.handle_loaded({})["success"])

    def test_each_cycle_requires_fresh_loading_confirmation_and_uses_config_context(self):
        captured = []
        self.action.side_effect = lambda node, ctx: captured.append(ctx.get("arm_url")) or True
        self.graph["context_vars"] = {"arm_url": "http://old-value/arm"}
        self._begin()
        first_stop = self.handler._stop_event
        first_bus = self.handler._signal_bus
        self._waiting()
        self.handler.handle_loaded({})
        self._finish()
        first_bus.fire("ZHANTING_LOADED")
        self._begin()
        self._waiting()
        self.assertIsNot(first_stop, self.handler._stop_event)
        self.assertIsNot(first_bus, self.handler._signal_bus)
        self.assertEqual(len(captured), 1)
        self.handler.handle_loaded({})
        self._finish()
        self.assertEqual(captured, ["http://configured/arm"] * 2)

    def test_pause_resume_and_stop_at_wait(self):
        self._begin()
        self._waiting()
        self.assertTrue(self.handler.handle_process_paused({})["success"])
        self.assertFalse(self.handler._pause_event.is_set())
        self.assertTrue(self.handler.handle_process_resumed({})["success"])
        self.assertTrue(self.handler._pause_event.is_set())
        self.assertTrue(self.handler.handle_process_ended({})["success"])
        self._finish()
        self.assertEqual(self.handler.task_state_machine.status, TaskStatus.CANCELLED)
        self.action.assert_not_called()
        self.assertFalse(self.handler.handle_process_resumed({})["success"])
        self._begin()
        self._waiting()
        self.handler.handle_loaded({})
        self._finish()
        self.assertEqual(self.handler.task_state_machine.status, TaskStatus.COMPLETED)

    def test_concurrent_start_launches_exactly_one_worker(self):
        barrier = threading.Barrier(3)
        results = []

        def start():
            barrier.wait(timeout=2)
            results.append(self.handler.handle_process_begins({}))

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual([result["code"] for result in results if not result["success"]], [ErrorCode.ROBOT_BUSY])
        self.prepare.assert_called_once()

    def test_stopping_worker_blocks_restart_until_device_call_returns(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocking_action(node, ctx):
            entered.set()
            return release.wait(timeout=5)

        self.action.side_effect = blocking_action
        self.graph = _graph({"id": "action", "type": "test_action"})
        self._begin()
        self.assertTrue(entered.wait(timeout=2))
        original_stop = self.handler._stop_event
        self.handler.handle_process_ended({})
        result = self.handler.handle_process_begins({})
        self.assertEqual(result["code"], ErrorCode.ROBOT_BUSY)
        self.assertTrue(original_stop.is_set())
        self.plc.close.assert_not_called()
        release.set()
        self._finish()
        self.assertEqual(self.handler.task_state_machine.status, TaskStatus.CANCELLED)
        self.plc.close.assert_called_once()
        self._begin()
        self._finish()
        self.assertIsNot(self.handler._stop_event, original_stop)
        self.assertTrue(original_stop.is_set())

    def test_parallel_timeout_waits_for_inflight_call_before_restart(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.action.side_effect = lambda node, ctx: entered.set() or release.wait(timeout=5)
        self.graph = {
            "flow_id": "timeout-test", "start": "fork",
            "nodes": [
                {"id": "fork", "type": "parallel", "params": {"branches": ["work"], "timeout": 0.01}},
                {"id": "work", "type": "test_action"},
            ],
            "edges": [],
        }
        self._begin()
        self.assertTrue(entered.wait(timeout=2))
        _wait(self.handler._stop_event.is_set)
        self.assertEqual(self.handler.handle_process_begins({})["code"], ErrorCode.ROBOT_BUSY)
        self.plc.close.assert_not_called()
        release.set()
        self._finish()
        self.plc.close.assert_called_once()

    def test_shutdown_reports_busy_until_worker_exits_and_rejects_future_start(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.action.side_effect = lambda node, ctx: entered.set() or release.wait(timeout=5)
        self.graph = _graph({"id": "work", "type": "test_action"})
        self._begin()
        self.assertTrue(entered.wait(timeout=2))
        self.assertFalse(self.handler.shutdown())
        release.set()
        self._finish()
        self.assertTrue(self.handler.shutdown())
        self.assertFalse(self.handler.handle_process_begins({})["success"])

    def test_preflight_error_does_not_start_or_dispatch_nodes(self):
        self.prepare.side_effect = project.PlcConfigurationError("missing mapping")
        response = self.handler.handle_process_begins({})
        self.assertFalse(response["success"])
        self.assertIn("missing mapping", response["message"])
        self.assertIsNone(self.handler._engine_thread)
        self.action.assert_not_called()


class TestZhantingPreflight(unittest.TestCase):
    def setUp(self):
        self.config = {
            "zhanting": {
                "arm_url": "http://arm.test/run", "arm_response_mode": "completed",
                "arm_success_field": "success", "arm_success_value": True,
                "arm_tasks": {key: key for key in project.ARM_TASK_KEYS},
            },
            "plc": {"protocol": "s7"},
        }

    def test_missing_http_and_task_configuration_rejected_before_plc_factory(self):
        for field, value in (("arm_url", ""), ("arm_tasks", {}), ("arm_response_mode", "accepted")):
            config = copy.deepcopy(self.config)
            config["zhanting"][field] = value
            with self.subTest(field=field), patch.object(project, "load_config", return_value=config), patch.object(project, "client_from_config") as factory:
                with self.assertRaises(ValueError):
                    project._prepare_run()
                factory.assert_not_called()

    def test_config_is_copied_and_plc_mapping_validation_is_not_bypassed(self):
        with patch.object(project, "load_config", return_value=self.config), patch.object(project, "client_from_config") as factory:
            context, client = project._prepare_run()
            self.assertIs(client, factory.return_value)
            factory.assert_called_once_with(self.config["plc"])
            context["arm_tasks"]["load_to_opener"] = "modified"
            self.assertEqual(self.config["zhanting"]["arm_tasks"]["load_to_opener"], "load_to_opener")
            factory.side_effect = project.PlcConfigurationError("DB offset required")
            with self.assertRaisesRegex(project.PlcConfigurationError, "DB offset required"):
                project._prepare_run()


if __name__ == "__main__":
    unittest.main()
