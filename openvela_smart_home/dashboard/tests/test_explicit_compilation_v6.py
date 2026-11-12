"""Explicit commands compile locally, then traverse durable execution rails."""
from contextlib import ExitStack
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / 'openvela_smart_home/dashboard'
sys.path[:0] = [str(DASH), str(ROOT / 'SenseHome-6G/net_offload')]
from durable_receipts import SQLiteReceiptExecutor
from mqtt_receipts import SoftwareEntityStore
from offload_gateway import BackendResult, OffloadGateway, RuleBackend


class ExplicitCompilationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        spec = importlib.util.spec_from_file_location('explicit_compilation_v6_bridge', DASH / 'bridge_server.py')
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'SENSEHOME_STATE_FILE': str(folder / 'state.json'),
            'SENSEHOME_EVENT_LOG': str(folder / 'events.jsonl'),
            'SENSEHOME_REQUEST_DB': str(folder / 'requests.sqlite3'),
            'SENSEHOME_POLICY_CLI': str(folder / 'absent-cli'),
            'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
        }):
            spec.loader.exec_module(self.bridge)
        self.stack.callback(self.bridge.close_gateway)
        self.bridge.save_state(self.bridge.default_state())
        entity = SoftwareEntityStore(folder / 'entity.sqlite3', 'explicit-home', self.bridge.load_state())

        class Transport:
            submissions = 0
            def submit(self, payload):
                self.submissions += 1
                return entity.submit(payload)
            def lookup(self, command_id):
                return entity.lookup(command_id)
            def snapshot(self):
                return entity.snapshot()
            def connection_state(self):
                return {'connected': True, 'subscribed': True, 'generation': 1}

        class Policy:
            def decide(self, *args):
                return 'cloud-model', 'MODEL_PREFERRED', ''

        class Planner:
            calls = 0
            def infer(self, *args):
                self.calls += 1
                return BackendResult(True, False, {}, [], source='cloud-model')

        self.transport, self.planner = Transport(), Planner()
        self.bridge.gateway_receipt_executor = SQLiteReceiptExecutor(
            folder / 'receipts.sqlite3', self.transport, platform_id='explicit-home')
        self.bridge.gateway = OffloadGateway(Policy(), {
            'local-rules': RuleBackend(self.bridge._gateway_local_handler), 'cloud-model': self.planner},
            receipt_executor=self.bridge._gateway_action_executor)

    def test_complete_two_room_plan_is_local_durable_and_idempotent(self):
        command = '把客厅灯光设为60%，再把卧室灯光设为30%'
        result = self.bridge.process_http_command(command, {'command_id': 'explicit-two-room'})
        self.assertTrue(result.get('task_complete'), result)
        self.assertEqual(result['backend'], 'local-rules')
        self.assertEqual(result['reason'], 'SERVER_EXPLICIT_GOALS')
        self.assertEqual([a['params']['brightness'] for a in result['actions']], [60, 30])
        self.assertEqual(self.planner.calls, 0)
        self.assertEqual(self.transport.submissions, 1)
        replay = self.bridge.process_http_command(command, {'command_id': 'explicit-two-room'})
        self.assertTrue(replay['task_complete'])
        self.assertEqual(self.transport.submissions, 1)
        state = self.transport.snapshot()['software_state']
        self.assertEqual(state['lights']['客厅']['brightness'], 60)
        self.assertEqual(state['lights']['卧室']['brightness'], 30)

    def test_equivalent_explicit_aliases_execute_without_model(self):
        for index, command in enumerate(('麻烦把客厅灯光设为60%', '把客厅的灯调成60%', '将客厅灯亮度设为百分之六十')):
            with self.subTest(command=command):
                result = self.bridge.process_http_command(command, {'command_id': f'alias-{index}'})
                self.assertTrue(result.get('task_complete'), result)
                self.assertEqual(result['actions'][0]['params']['brightness'], 60)
        self.assertEqual(self.planner.calls, 0)

    def test_unresolved_or_conditional_command_never_compiles_partial_goals(self):
        before = deepcopy(self.transport.snapshot()['software_state'])
        for index, command in enumerate(('如果客厅暗就把客厅灯设为60%', '把客厅灯设为60%，并打开阳台灯',
                                         '不要打开客厅灯', '把客厅灯设为101%', '打开客厅灯并关闭客厅灯')):
            with self.subTest(command=command):
                result = self.bridge.process_http_command(command, {'command_id': f'unresolved-{index}'})
                self.assertFalse(result.get('task_complete'), result)
        self.assertEqual(self.transport.submissions, 0)
        self.assertEqual(self.transport.snapshot()['software_state'], before)

    def test_expired_explicit_plan_does_not_submit(self):
        result = self.bridge.process_command('把客厅灯设为60%，再把卧室灯设为30%',
            internal={'_deadline_monotonic': time.monotonic() - 1})
        self.assertEqual(result['error'], 'DEADLINE_EXCEEDED')
        self.assertEqual(self.transport.submissions, 0)
        self.assertEqual(self.planner.calls, 0)


if __name__ == '__main__':
    unittest.main()
