"""Intrinsic F01/F02/F03/F09 regressions; all state stays in a private directory."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
import zipfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = tempfile.TemporaryDirectory(prefix='sensehome-language-import-')
sys.path.insert(0, str(ROOT / 'openvela_smart_home/dashboard'))
sys.path.insert(0, str(ROOT / 'SenseHome-6G/net_offload'))
with mock.patch.dict(os.environ, {
    'SENSEHOME_POLICY_CLI': str(Path(PRIVATE.name) / 'missing-cli'),
    'SENSEHOME_EVENT_LOG': str(Path(PRIVATE.name) / 'events.jsonl'),
    'SENSEHOME_STATE_FILE': str(Path(PRIVATE.name) / 'state.json'),
}):
    import bridge_server as bridge
from backends.openai_backend import BackendConfig, BackendError, InferenceService
from offload_gateway import BackendResult, OffloadGateway, RuleBackend


class Policy:
    def decide(self, task_class, link):
        return 'cloud-model', 'TEST', ''


class Planner:
    def __init__(self, actions):
        self.actions, self.calls = actions, []

    def infer(self, command, context=None):
        self.calls.append((command, context))
        return BackendResult(True, True, {'answer': 'model says done'}, self.actions)


class LanguageContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sensehome-language-')
        self.previous = (bridge.STATE_FILE, bridge.gateway, bridge.backend_monitor, bridge.gateway_receipt_executor)
        bridge.STATE_FILE = str(Path(self.tmp.name) / 'state.json')
        bridge.backend_monitor = bridge.gateway_receipt_executor = None
        bridge.save_state(bridge.default_state())

    def tearDown(self):
        bridge.STATE_FILE, bridge.gateway, bridge.backend_monitor, bridge.gateway_receipt_executor = self.previous
        self.tmp.cleanup()

    def gateway(self, actions):
        planner = Planner(actions)
        bridge.gateway = OffloadGateway(Policy(), {'cloud-model': planner,
            'local-rules': RuleBackend(bridge._gateway_local_handler)}, action_executor=bridge._gateway_action_executor)
        return planner

    def test_legal_wrong_plan_is_rejected_before_any_mutation(self):
        for actions in (
            [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 20}}],
            [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}],
            [{'name': 'light_control', 'params': {'location': '卧室', 'brightness': 20}},
             {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}],
        ):
            with self.subTest(actions=actions):
                before = deepcopy(bridge.load_state())
                self.gateway(actions)
                # Directly exercise the model boundary; the public bridge now
                # compiles this complete explicit request without a model.
                result = bridge.gateway.handle('把客厅灯光设为60%，再把卧室灯光设为20%')
                self.assertEqual(bridge.load_state(), before)
                self.assertFalse(result.get('task_complete', result['semantic_complete']))
                self.assertFalse(result['fallback_attempted'])
                self.assertEqual(result['error'], 'REQUEST_GOAL_PLAN_MISMATCH')

    def test_adapter_rejects_missing_goal_even_when_actions_are_legal(self):
        service = InferenceService(BackendConfig('http://127.0.0.1:1/v1', 'fixture'))
        plan = {'answer': '', 'actions': [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 20}}]}
        upstream = {'choices': [{'message': {'content': json.dumps(plan)}}]}
        with mock.patch.object(service, '_upstream', return_value=(upstream, 1)):
            with self.assertRaisesRegex(BackendError, 'REQUEST_GOAL_PLAN_MISMATCH'):
                service.infer({'command': '把客厅灯光设为60%，再把卧室灯光设为20%'})

    def test_empty_model_plan_cannot_announce_task_success(self):
        service = InferenceService(BackendConfig('http://127.0.0.1:1/v1', 'fixture'))
        plan = {'answer': '我已经把所有灯调整完成。', 'actions': []}
        upstream = {'choices': [{'message': {'content': json.dumps(plan)}}]}
        with mock.patch.object(service, '_upstream', return_value=(upstream, 1)):
            result = service.infer({'command': '把客厅灯光设为60%'})
        self.assertNotIn('已经', result['answer'])
        self.assertFalse(result['semantic_complete'])

    def test_adapter_starts_directly_and_from_offline_bundle(self):
        import importlib.util
        source = ROOT / 'docs/deployment/prepare_autodl_recovery.py'
        spec = importlib.util.spec_from_file_location('private_adapter_packager', source)
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        bundle = Path(self.tmp.name) / 'adapter.zip'
        packager.build(bundle)
        extracted = Path(self.tmp.name) / 'adapter'
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extracted)
        for script in (ROOT / 'SenseHome-6G/net_offload/backends/openai_backend.py',
                       extracted / 'backends/openai_backend.py'):
            process = subprocess.run([sys.executable, str(script), '--help'], cwd=self.tmp.name,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_correct_plan_requires_current_state_acceptance(self):
        self.gateway([{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}},
                      {'name': 'light_control', 'params': {'location': '卧室', 'brightness': 20}}])
        result = bridge.process_command('把客厅灯光设为60%，再把卧室灯光设为20%')
        self.assertTrue(result.get('plan_valid'))
        self.assertTrue(result.get('execution_complete'))
        self.assertTrue(result.get('task_complete'))

    def test_rule_unresolved_can_reach_model(self):
        planner = self.gateway([])
        result = bridge.process_command('把卧室灯弄亮些')
        self.assertEqual(len(planner.calls), 1)
        self.assertFalse(result['semantic_complete'])

    def test_clarification_slots_continue_in_same_session(self):
        self.gateway([])
        first = bridge.process_command('有点暗', {'session_id': 'clarify-complete'})
        second = bridge.process_command('卧室', {'session_id': 'clarify-complete'})
        third = bridge.process_command('调到60%', {'session_id': 'clarify-complete'})
        self.assertTrue(first.get('clarification_required'))
        self.assertTrue(second.get('clarification_required'))
        self.assertFalse(second['semantic_complete'])
        self.assertTrue(third.get('task_complete'), third)
        self.assertEqual(bridge.load_state()['lights']['卧室']['brightness'], 60)

    def test_sessions_cancel_and_expiry_do_not_restore_old_intent(self):
        self.gateway([])
        bridge.process_command('有点暗', {'session_id': 'clarify-a'})
        bridge.process_command('卧室', {'session_id': 'clarify-b'})
        self.assertFalse(bridge.process_command('60%', {'session_id': 'clarify-b'})['semantic_complete'])
        bridge.process_command('取消', {'session_id': 'clarify-a'})
        self.assertFalse(bridge.process_command('卧室60%', {'session_id': 'clarify-a'})['semantic_complete'])
        bridge.process_command('有点暗', {'session_id': 'clarify-expire'})
        with mock.patch('clarification_sessions._monotonic', return_value=time.monotonic() + 1000):
            result = bridge.process_command('卧室60%', {'session_id': 'clarify-expire'})
        self.assertFalse(result['semantic_complete'])

    def test_new_relative_intent_does_not_inherit_prior_value(self):
        self.gateway([])
        bridge.process_command('有点暗', {'session_id': 'replace-relative'})
        bridge.process_command('60%', {'session_id': 'replace-relative'})
        result = bridge.process_command('客厅有点亮', {'session_id': 'replace-relative'})
        self.assertTrue(result['clarification_required'])
        self.assertEqual(result['missing_slots'], ['value'])
        self.assertEqual(bridge.load_state()['lights']['客厅']['brightness'], 0)

    def test_external_internal_fields_never_reach_gateway(self):
        planner = self.gateway([])
        bridge.process_command('把卧室灯弄亮些', {'_deadline_monotonic': 0, '_trusted_goal': 'invented'})
        self.assertTrue(planner.calls)
        self.assertFalse(any(str(k).startswith('_') for k in planner.calls[-1][1]))

    def test_expired_internal_deadline_prevents_local_submission(self):
        self.gateway([])
        before = deepcopy(bridge.load_state())
        result = bridge.process_command('打开客厅灯', internal={'_deadline_monotonic': time.monotonic() - 1})
        self.assertEqual(bridge.load_state(), before)
        self.assertFalse(result['semantic_complete'])
        self.assertEqual(result['error'], 'DEADLINE_EXCEEDED')

    def test_expired_remote_plan_is_terminal_without_rule_fallback(self):
        self.gateway([{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}},
                      {'name': 'curtain_control', 'params': {'location': '卧室', 'action': 'open'}}])
        before = deepcopy(bridge.load_state())
        result = bridge.process_command('把客厅灯光设为60%，再打开卧室窗帘',
                                       internal={'_deadline_monotonic': time.monotonic() - 1})
        self.assertEqual(bridge.load_state(), before)
        self.assertEqual(result['error'], 'DEADLINE_EXCEEDED')
        self.assertFalse(result['fallback_attempted'])

    def test_deadline_expiring_during_validation_cannot_submit(self):
        original = bridge.execute_device_actions
        def delayed_validation(state, actions):
            result = original(state, actions)
            time.sleep(0.02)
            return result
        before = deepcopy(bridge.load_state())
        with mock.patch.object(bridge, 'execute_device_actions', side_effect=delayed_validation):
            result = bridge._gateway_action_executor(
                [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}],
                {'_receipt_command': '把客厅灯光设为60%', '_deadline_monotonic': time.monotonic() + 0.01})
        self.assertEqual(bridge.load_state(), before)
        self.assertEqual(result['error'], 'DEADLINE_EXCEEDED')

    def test_mqtt_receipt_without_fresh_snapshot_is_not_task_complete(self):
        class Receipt:
            platform_id = 'test-home'
            def __call__(self, actions, context):
                return {'interface_ok': True, 'semantic_complete': True, 'execution_state': 'applied', 'actions': actions}
            def snapshot(self):
                return None
        bridge.gateway_receipt_executor = Receipt()
        result = bridge._gateway_action_executor([{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}],
                                                {'_receipt_command': '把客厅灯光设为60%'})
        self.assertTrue(result.get('execution_complete'))
        self.assertFalse(result.get('task_complete', result['semantic_complete']))

    def test_snapshot_older_than_applied_receipt_cannot_accept_goal(self):
        current = bridge.load_state()
        current['lights']['客厅'] = {'brightness': 60, 'is_on': True}
        class Receipt:
            platform_id = 'test-home'
            def __call__(self, actions, context):
                return {'interface_ok': True, 'execution_state': 'applied', 'actions': actions,
                        'receipt': {'state_version_after': 3}}
            def snapshot(self):
                return {'schema': 'sensehome.software-state.v1', 'platform_id': self.platform_id,
                        'evidence_kind': 'mqtt-software-entity', 'state_version': 2, 'software_state': current}
        bridge.gateway_receipt_executor = Receipt()
        result = bridge._gateway_action_executor([{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}],
                                                {'_receipt_command': '把客厅灯光设为60%'})
        self.assertTrue(result['execution_complete'])
        self.assertFalse(result['task_complete'])

    def test_leave_home_submits_supported_batch_and_reports_unsupported_security(self):
        calls = []
        with mock.patch.object(bridge, '_gateway_action_executor', side_effect=lambda a, c=None: calls.append((a, c)) or
                               {'actions': a, 'execution_complete': True, 'task_complete': True, 'semantic_complete': True}):
            result = bridge._gateway_local_handler('离家模式', {'command_id': 'away-test'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][0]), 10)
        self.assertEqual(calls[0][1]['command_id'], 'away-test')
        self.assertFalse(result.get('task_complete', result['semantic_complete']))
        self.assertIn('security_arming', result.get('unsupported_capabilities', []))

    def receipt_fixture(self, partial_after=None):
        from durable_receipts import SQLiteReceiptExecutor
        from mqtt_receipts import SoftwareEntityStore
        initial = bridge.load_state()
        initial.pop('sensors', None)
        entity = SoftwareEntityStore(Path(self.tmp.name) / 'entity.sqlite', 'goal-home', initial,
                                     partial_after=partial_after)
        bridge.gateway_receipt_executor = SQLiteReceiptExecutor(Path(self.tmp.name) / 'receipts.sqlite', entity,
                                                                platform_id='goal-home')
        return entity

    def test_partial_mqtt_batch_cannot_complete_task_even_with_valid_receipt(self):
        self.receipt_fixture(partial_after=1)
        result = bridge._gateway_action_executor([
            {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}},
            {'name': 'light_control', 'params': {'location': '卧室', 'brightness': 20}},
        ], {'command_id': 'partial-goals', '_receipt_command': '把客厅灯光设为60%，再把卧室灯光设为20%'})
        self.assertEqual(result['execution_state'], 'partial')
        self.assertFalse(result['execution_complete'])
        self.assertFalse(result['task_complete'])

    def test_mqtt_patrol_uses_receipt_read_batch_without_inventing_sensor_controls(self):
        entity = self.receipt_fixture()
        result = bridge.run_environment_patrol({'command_id': 'patrol-goals'})
        self.assertTrue(result['task_complete'])
        self.assertEqual(result['command_id'], 'patrol-goals')
        receipt = entity.lookup('patrol-goals')
        self.assertEqual(len(receipt['requested_actions']), 5)
        self.assertTrue(all(a['name'] == 'read_temperature' for a in receipt['requested_actions']))
        self.assertEqual(result['auto_decisions'], 0)
        self.assertIn('illuminance', result['unavailable_sensors'])

    def test_mqtt_leave_scene_commits_allowlisted_actions_with_one_id(self):
        entity = self.receipt_fixture()
        result = bridge._gateway_local_handler('离家模式', {'command_id': 'leave-goals'})
        self.assertTrue(result['supported_scope_complete'])
        receipt = entity.lookup('leave-goals')
        self.assertEqual(len(receipt['requested_actions']), 10)
        self.assertTrue(all(a['name'] in {'light_control', 'curtain_control'} for a in receipt['requested_actions']))
        self.assertFalse(result['task_complete'])


if __name__ == '__main__':
    unittest.main()
