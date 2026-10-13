"""通用 HTTP 完成判定及标准机器人节点的隔离测试。"""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from core.flow_engine import FlowContext, FlowNode
from core.http_nodes import build_http_handlers
from core.robot_nodes import build_robot_handlers


class TestCommonHttp(unittest.TestCase):
    def setUp(self):
        self.ctx = FlowContext({"cube": 2, "arm_url": "http://arm.local/task",
                                "arm_tasks": {"load": "load_to_opener"}})
        self.handler = build_http_handlers()["send_http"]
        self.params = {
            "url": "{{arm_url}}", "method": "POST",
            "body": {"task": "{{arm_tasks.load}}", "extra_params": {"cube": "{{cube}}"}},
            "success_field": "data.completed", "success_value": True,
            "result_var": "arm_response", "timeout": 4,
        }

    def run_node(self, response=None, **overrides):
        node = FlowNode("http", "send_http", params={**self.params, **overrides})
        mocked_response = Mock(status_code=200)
        mocked_response.json.return_value = response if response is not None else {"data": {"completed": True}}
        with patch("core.http_nodes.requests.request", return_value=mocked_response) as request:
            ok = self.handler(node, self.ctx)
        return ok, request

    def test_renders_nested_payload_and_records_full_response(self):
        body = {"data": {"completed": True, "cube": 2}}
        ok, request = self.run_node(body)
        self.assertTrue(ok)
        self.assertEqual(request.call_args.kwargs["json"], {
            "task": "load_to_opener", "extra_params": {"cube": 2},
        })
        self.assertEqual(request.call_args.kwargs["timeout"], 4)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.ctx.get("arm_response"), body)

    def test_json_text_retains_numeric_template_type(self):
        ok, request = self.run_node(body='{"task": "{{arm_tasks.load}}", "cube": "{{cube}}"}')
        self.assertTrue(ok)
        self.assertEqual(request.call_args.kwargs["json"]["cube"], 2)

    def test_full_json_variable_can_be_body(self):
        self.ctx.set("payload", {"task": "load"})
        ok, request = self.run_node(body="{{payload}}")
        self.assertTrue(ok)
        self.assertEqual(request.call_args.kwargs["json"], {"task": "load"})

    def test_completion_value_must_match(self):
        for value in (False, "true", 1, "accepted"):
            with self.subTest(value=value):
                ok, _ = self.run_node({"data": {"completed": value}})
                self.assertFalse(ok)
                self.assertFalse(self.ctx.get("last_http_success"))

    def test_missing_completion_field_fails(self):
        ok, _ = self.run_node({"success": True, "message": "accepted"})
        self.assertFalse(ok)

    def test_nested_array_field_supported(self):
        ok, _ = self.run_node({"items": [{"state": "done"}]},
                              success_field="items.0.state", success_value="done")
        self.assertTrue(ok)

    def test_non_json_response_fails(self):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("no json")
        with patch("core.http_nodes.requests.request", return_value=response):
            self.assertFalse(self.handler(FlowNode("http", "send_http", params=self.params), self.ctx))

    def test_acceptance_and_http_error_are_not_completion(self):
        for status in (202, 204, 302, 400, 500):
            with self.subTest(status=status):
                response = Mock(status_code=status)
                response.json.return_value = {"data": {"completed": True}}
                if status == 204:
                    response.json.side_effect = ValueError("empty")
                with patch("core.http_nodes.requests.request", return_value=response):
                    self.assertFalse(self.handler(FlowNode("http", "send_http", params=self.params), self.ctx))
                self.assertEqual(self.ctx.get("last_http_status"), status)

    def test_bad_configuration_never_sends(self):
        invalid = [
            {"url": ""}, {"url": "{{missing_url}}"}, {"url": "file:///tmp/arm"},
            {"body": {"task": "{{missing_task}}"}}, {"body": {"task": ""}},
            {"body": "invalid JSON"}, {"body": 2}, {"headers": []},
            {"headers": {"Authorization": None}}, {"method": "NOPE"},
            {"timeout": 0}, {"timeout": float("nan")},
            {"success_field": ""}, {"success_value": None},
            {"response_mode": "accepted"},
        ]
        for params in invalid:
            with self.subTest(params=params):
                ok, request = self.run_node(**params)
                self.assertFalse(ok)
                request.assert_not_called()

    def test_timeout_does_not_retry_or_retain_stale_result(self):
        self.ctx.set("arm_response", {"data": {"completed": True}})
        with patch("core.http_nodes.requests.request", side_effect=requests.Timeout()) as request:
            self.assertFalse(self.handler(FlowNode("http", "send_http", params=self.params), self.ctx))
        request.assert_called_once()
        self.assertIsNone(self.ctx.get("arm_response"))

    def test_stop_prevents_network(self):
        event = threading.Event()
        event.set()
        self.handler = build_http_handlers(event)["send_http"]
        ok, request = self.run_node()
        self.assertFalse(ok)
        request.assert_not_called()

    def test_stop_while_request_running_does_not_succeed(self):
        event = threading.Event()
        self.ctx.extra["stop_event"] = event

        def respond(*args, **kwargs):
            event.set()
            response = Mock(status_code=200)
            response.json.return_value = {"data": {"completed": True}}
            return response

        with patch("core.http_nodes.requests.request", side_effect=respond):
            self.assertFalse(self.handler(FlowNode("http", "send_http", params=self.params), self.ctx))

    def test_dryrun_skips_network_with_unconfigured_device(self):
        self.ctx.extra["dryrun"] = True
        self.ctx.set("arm_url", "")
        self.ctx.set("arm_tasks", {})
        ok, request = self.run_node()
        self.assertTrue(ok)
        request.assert_not_called()
        self.assertEqual(self.ctx.get("arm_response"), {"data": {"completed": True}})


class TestCommonRobotNodes(unittest.TestCase):
    def setUp(self):
        self.robot = Mock()
        self.robot.is_connected.return_value = True
        self.ctx = FlowContext({"robot_id": "robot_a", "cube": 2})
        self.state_machine = Mock()
        self.handlers = build_robot_handlers(
            {"robot_a": self.robot}, self.state_machine, {"detect": [(1, 2, 0)]},
        )

    def test_standard_action_preserves_cube(self):
        node = FlowNode("op", "send_operation", params={
            "task": "pure_water", "extra_params": '{"cube": "{{cube}}"}',
        })
        with patch("core.robot_nodes.send_task_action", return_value=True) as action:
            self.assertTrue(self.handlers["send_operation"](node, self.ctx))
        self.assertEqual(action.call_args.kwargs["task"], "pure_water")
        self.assertEqual(action.call_args.kwargs["extra_params"], {"cube": 2})

    def test_service_uses_standard_robot_task(self):
        node = FlowNode("op", "send_operation", params={
            "task": "pour_water", "call_type": "service", "service": "robot_task",
            "extra_params": {"cube": 1},
        })
        self.robot.send_service_request_task.return_value = True
        self.assertTrue(self.handlers["send_operation"](node, self.ctx))
        self.assertEqual(self.robot.send_service_request_task.call_args.args, ("/robot_task",))
        self.assertEqual(self.robot.send_service_request_task.call_args.kwargs["extra_params"], {"cube": 1})

    def test_invalid_operation_params_do_not_move_robot(self):
        for params in ({"extra_params": "broken"}, {"extra_params": []},
                       {"extra_params": 1}, {"task": "{{unknown_task}}"},
                       {"call_type": "other"}, {"timeout": 0}):
            with self.subTest(params=params):
                node = FlowNode("op", "send_operation", params={"task": "pure_water", **params})
                with patch("core.robot_nodes.send_task_action") as action:
                    self.assertFalse(self.handlers["send_operation"](node, self.ctx))
                action.assert_not_called()

    def test_navigation_uses_configured_pose(self):
        node = FlowNode("nav", "navigate", params={"pose": "detect", "skip_if_at_pose": False})
        with patch("core.robot_nodes.send_navigation_action", return_value=SimpleNamespace(succeeded=True)) as nav:
            self.assertTrue(self.handlers["navigate"](node, self.ctx))
        self.assertEqual(nav.call_args.args[1].waypoints[0].x, 1)
        self.assertEqual(self.ctx.get("last_nav_pose"), "detect")

    def test_navigation_failure_does_not_record_arrival(self):
        node = FlowNode("nav", "navigate", params={"pose": "detect", "skip_if_at_pose": False})
        with patch("core.robot_nodes.send_navigation_action", return_value=SimpleNamespace(succeeded=False)):
            self.assertFalse(self.handlers["navigate"](node, self.ctx))
        self.assertIsNone(self.ctx.get("last_nav_pose"))

    def test_navigation_accepts_pose_class(self):
        poses = SimpleNamespace(detect=[(3, 4, 0)])
        handlers = build_robot_handlers({"robot_a": self.robot}, None, poses)
        node = FlowNode("nav", "navigate", params={"pose": "detect", "skip_if_at_pose": False})
        with patch("core.robot_nodes.send_navigation_action", return_value=SimpleNamespace(succeeded=True)):
            self.assertTrue(handlers["navigate"](node, self.ctx))

    def test_update_step_uses_parallel_state_machine_signature(self):
        node = FlowNode("state", "update_step", params={"step": "检测", "message": "运行中"})
        self.assertTrue(self.handlers["update_step"](node, self.ctx))
        self.state_machine.update_step.assert_called_once_with("robot_a", "检测", "运行中")


if __name__ == "__main__":
    unittest.main()
