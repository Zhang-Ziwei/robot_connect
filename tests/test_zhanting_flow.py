"""Execute the shipped graph to check ordering, failure propagation and public schemas."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.flow_engine import FlowEngine, SignalBus

ROOT = Path(__file__).resolve().parents[1]


def graph():
    return json.loads((ROOT / 'programs/ZHANTING/flows/zhanting_main.json').read_text())


class TestZhantingFlow(unittest.TestCase):
    def run_graph(self, fail=None):
        events = []
        lock = threading.Lock()
        bus = SignalBus()
        bus.fire('PROCESS_BEGINS', data={})
        bus.fire('ZHANTING_LOADED', data={})

        def record(node, ctx):
            with lock:
                events.append(node.id)
            return node.id != fail

        handlers = {name: record for name in
                    ('send_http', 'send_operation', 'plc_handshake', 'plc_read', 'update_step')}
        engine = FlowEngine(graph(), handlers=handlers, signal_bus=bus)
        self.assertEqual(engine.validate(), [])
        return engine.run(), events

    def test_parallel_joins_and_robot_parameters(self):
        result, events = self.run_graph()
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(events), len(set(events)), events)
        before = lambda a, b: self.assertLess(events.index(a), events.index(b), events)
        for a, b in [('pure_1', 'pure_2'), ('pure_2', 'detect_start'), ('detect_dispense', 'detect_start'),
                     ('pure_2', 'arm_close_load'), ('detect_start', 'pour_1'), ('pour_1', 'detect_pick'),
                     ('detect_pick', 'pour_2'), ('pour_2', 'detect_finish'),
                     ('close_finish', 'end'), ('detect_finish', 'end')]:
            before(a, b)
        ops = [n['params'] for n in graph()['nodes'] if n['type'] == 'send_operation']
        self.assertEqual([(p['task'], p['extra_params']) for p in ops], [
            ('pure_water', {'cube': 1}), ('pure_water', {'cube': 2}),
            ('pour_water', {'cube': 1}), ('pour_water', {'cube': 2}),
        ])

    def test_branch_failure_never_reports_completion(self):
        for failure in ('pure_2', 'detect_dispense', 'close', 'pour_1', 'detect_pick', 'detect_finish'):
            with self.subTest(failure=failure):
                result, events = self.run_graph(failure)
                self.assertFalse(result.success)
                self.assertNotIn('end', events)
                if failure in ('pure_2', 'detect_dispense'):
                    self.assertNotIn('detect_start', events)

    def test_detect_states_and_complete_point_table(self):
        nodes = {n['id']: n for n in graph()['nodes']}
        self.assertEqual(nodes['detect_pick']['params']['done_value'], 6)
        cfg = json.loads((ROOT / 'programs/ZHANTING/robot_config.json').read_text())
        plc = cfg['plc']
        self.assertEqual(plc['host'], '192.168.217.40')
        inputs = [p for p in plc['points'].values() if p['type'] == 'INT']
        outputs = [p for p in plc['points'].values() if p['type'] == 'BOOL']
        self.assertEqual([p['address'] for p in inputs], list(range(40000, 40019)))
        self.assertEqual([p['address'] for p in outputs], list(range(9)))
        self.assertTrue(all(p['access'] == 'read' and p['db'] == 2 for p in inputs))
        self.assertTrue(all(p['access'] == 'write' and p['db'] == 2 for p in outputs))

    def test_editor_exposes_common_nodes_and_manual_confirmation(self):
        from network.flow_api_server import PROJECT_ADAPTERS, _node_types_with_dynamic_options, _validate_graph
        adapter = PROJECT_ADAPTERS['ZHANTING']()
        self.assertEqual(_validate_graph(graph(), adapter), [])
        schemas = _node_types_with_dynamic_options('ZHANTING', adapter)
        for name in ('send_operation', 'navigate', 'send_http', 'plc_read', 'plc_write', 'plc_wait', 'plc_handshake'):
            self.assertEqual(schemas[name]['scope'], 'common')
        self.assertIn('ZHANTING_LOADED', adapter['run_control']['allowed_commands'])

    def test_default_dryrun_skips_unconfigured_external_hardware(self):
        from programs.ZHANTING.node_handlers import build_handler_registry
        from programs.ZHANTING.constants import NavigationPose
        handlers = build_handler_registry({}, None)
        operations = []
        handlers['send_operation'] = lambda n, c: operations.append((n.params['task'], n.params['extra_params']['cube'])) or True
        bus = SignalBus()
        bus.fire('PROCESS_BEGINS', data={})
        bus.fire('ZHANTING_LOADED', data={})
        with patch('core.http_nodes.requests.request', side_effect=AssertionError('real HTTP')):
            result = FlowEngine(graph(), handlers=handlers, signal_bus=bus).run(extra={'dryrun': True})
        self.assertTrue(result.success, result.message)
        self.assertEqual(len(operations), 4)


if __name__ == '__main__':
    unittest.main()
