"""CONST 装/拆表：点对点 MQTT 超时后用 live 确认，呼叫人工保留真实原因。"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.flow_engine import FlowContext, FlowNode
from network.mqtt_client import payload_request_id
from programs.CONST_FLOW.constants import HumanReason, StationStatus
from programs.CONST_FLOW.calsys_ops import conclude_station_action
from programs.CONST_FLOW.node_handlers import build_handler_registry


class FakeAdapter:
    def __init__(self, status=""):
        self._status = status
        self.human_reason = ""
        self.need_human = False
        self._human_cleared = threading.Event()
        self._human_cleared.set()

    def reply_code(self, reply):
        if not reply:
            return None
        data = reply.get("data") if "data" in reply else reply
        if not isinstance(data, dict) or "code" not in data:
            return None
        try:
            return int(data.get("code"))
        except (TypeError, ValueError):
            return None

    def call_human(self, reason):
        self.need_human = True
        self.human_reason = reason
        self._human_cleared.clear()

    def wait_human(self, stop_event=None):
        while not self._human_cleared.is_set():
            if stop_event is not None and stop_event.is_set():
                return False
            self._human_cleared.wait(timeout=0.05)
        return True

    def station_status(self, seq):  # noqa: ARG002
        return self._status

    def wait_station_status(self, seq, wanted, timeout=None, stop_event=None):  # noqa: ARG002
        if isinstance(wanted, str):
            wanted = {wanted}
        return self._status in set(wanted)


def test_uninstall_close_ok_when_live_already_wait_install():
    adapter = FakeAdapter(status=StationStatus.WAIT_INSTALL)
    ctx = FlowContext()
    outcome = conclude_station_action(adapter, 1, "uninstall", 1, None, ctx)
    assert outcome == "ok"
    assert not adapter.need_human


def test_uninstall_close_human_when_live_still_uninstalling():
    adapter = FakeAdapter(status=StationStatus.UNINSTALLING)
    ctx = FlowContext()
    outcome = conclude_station_action(adapter, 1, "uninstall", 1, None, ctx)
    assert outcome == "human"
    assert adapter.human_reason == HumanReason.MQTT_TIMEOUT
    assert ctx.get("human_reason") == HumanReason.MQTT_TIMEOUT


def test_explicit_error_code_does_not_use_live():
    adapter = FakeAdapter(status=StationStatus.WAIT_INSTALL)
    ctx = FlowContext()
    reply = {"requestId": "abc", "data": {"code": 1}}
    outcome = conclude_station_action(adapter, 1, "uninstall", 1, reply, ctx)
    assert outcome == "human"
    assert adapter.human_reason == HumanReason.CALSYS_ERROR


def test_install_close_ok_on_installed_live():
    adapter = FakeAdapter(status=StationStatus.INSTALLED)
    ctx = FlowContext()
    outcome = conclude_station_action(adapter, 1, "install", 1, None, ctx)
    assert outcome == "ok"


def test_payload_request_id_nested_in_data():
    assert payload_request_id({"requestId": "aa"}) == "aa"
    assert payload_request_id({"data": {"requestId": "bb"}}) == "bb"
    assert payload_request_id({"request_id": "cc"}) == "cc"


def test_call_human_keeps_mqtt_timeout_not_gauge_dropped():
    adapter = FakeAdapter()
    adapter.call_human(HumanReason.MQTT_TIMEOUT)
    stop = threading.Event()
    stop.set()
    handlers = build_handler_registry(
        {}, None, mqtt=adapter, get_robot=lambda _x: None, stop_event=stop,
    )
    node = FlowNode(
        id="human_wait", type="const_call_human", label="呼叫人工",
        params={"reason": "gauge_dropped"},
    )
    ctx = FlowContext(extra={"const_mqtt": adapter, "stop_event": stop})
    assert handlers["const_call_human"](node, ctx) is True
    assert adapter.human_reason == HumanReason.MQTT_TIMEOUT
    assert ctx.get("human_reason") == HumanReason.MQTT_TIMEOUT
