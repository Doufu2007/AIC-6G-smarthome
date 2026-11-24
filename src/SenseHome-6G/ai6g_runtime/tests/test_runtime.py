import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.runtime import TaskRuntime
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import ModelResult
from ai6g_runtime.router import RouteDecision


class StaticBackend:
    def __init__(self, payload, *, backend_name='cloud-model', available=True):
        self.payload = payload
        self.backend_name = backend_name
        self.available = available
        self.calls = 0

    def infer(self, task, timeout_s):
        self.calls += 1
        if not self.available:
            return ModelResult(ok=False, payload=None, error='endpoint_unavailable', model_real=False, latency_ms=0.0)
        return ModelResult(ok=True, payload=self.payload, error=None, model_real=True, latency_ms=1.0)


class FixedRouter:
    def __init__(self, backend_name):
        self.backend_name = backend_name
        self.calls = 0

    def choose(self, task, now=None):
        self.calls += 1
        return RouteDecision(backend=self.backend_name, reason='test_route', estimated_latency_ms=1.0,
                             quality_score=1.0, observation_age_ms=0.0, refresh_required=False)


class EligibilityRouter:
    def __init__(self):
        self.task = None

    def choose(self, task, now=None):
        self.task = task
        backend = 'local-rules' if task.rule_eligible else 'cloud-model'
        return RouteDecision(backend=backend, reason='eligibility_test', estimated_latency_ms=1.0,
                             quality_score=1.0, observation_age_ms=0.0, refresh_required=False)


class RuntimeTests(unittest.TestCase):
    def test_default_deadline_override_preserves_rule_eligibility(self):
        with tempfile.TemporaryDirectory() as td:
            router = EligibilityRouter()
            runtime = TaskRuntime(
                router=router,
                backends={
                    'local-rules': StaticBackend({'actions': [{'type': 'light_on', 'location': '客厅'}]}, backend_name='local-rules'),
                    'cloud-model': StaticBackend({'actions': [{'type': 'light_on', 'location': '客厅'}]}, backend_name='cloud-model'),
                },
                store=DeviceStore(), log_path=Path(td) / 'requests.jsonl', default_deadline_ms=1000)
            result = runtime.handle('打开客厅的灯', request_id='deadline-eligibility')
            self.assertTrue(result['ok'])
            self.assertTrue(router.task.rule_eligible)
            self.assertEqual(result['route']['backend'], 'local-rules')

    def test_fixed_cloud_route_cannot_bypass_strict_privacy(self):
        with tempfile.TemporaryDirectory() as td:
            backend = StaticBackend({'actions': [{'type': 'light_on', 'location': '客厅'}]}, backend_name='cloud-model')
            runtime = TaskRuntime(
                router=FixedRouter('cloud-model'),
                backends={'cloud-model': backend},
                store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            result = runtime.handle('不要上传任何信息，打开客厅的灯', request_id='privacy-fixed-route')
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_code'], 'privacy_violation')
            self.assertEqual(backend.calls, 0)

    def test_finish_never_rewrites_prior_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'requests.jsonl'
            runtime = TaskRuntime(router=FixedRouter('local-rules'),
                backends={'local-rules': StaticBackend({'actions': [{'type': 'light_on', 'location': '客厅'}]})},
                log_path=path)
            first = runtime.handle('打开客厅的灯', request_id='append-only-1')
            original = path.read_bytes()
            runtime.handle('打开客厅的灯', request_id='append-only-2')
            self.assertTrue(path.read_bytes().startswith(original))
            record = json.loads(original.decode('utf8').splitlines()[0])
            self.assertIn('service_ms', record['timing'])
            self.assertEqual(record['timing']['total_ms'], record['timing']['service_ms'])
            self.assertGreaterEqual(first['timing']['handler_total_ms'], first['timing']['service_ms'])
            self.assertEqual(first['timing']['scope'], 'entry_to_state_before_log')

    def test_timing_declares_service_and_handler_scopes(self):
        class SlowLogRuntime(TaskRuntime):
            def _append_log(self, record):
                import time
                time.sleep(0.01)
                super()._append_log(record)

        with tempfile.TemporaryDirectory() as td:
            runtime = SlowLogRuntime(
                router=FixedRouter('local-rules'),
                backends={'local-rules': StaticBackend({'actions': [{'type': 'light_on', 'location': '客厅'}]}, backend_name='local-rules')},
                store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            result = runtime.handle('打开客厅的灯', request_id='persist-latency')
            self.assertTrue(result['ok'])
            self.assertGreaterEqual(result['timing']['log_ms'], 9.0)
            self.assertEqual(result['timing']['scope'], 'entry_to_state_before_log')
            self.assertEqual(result['timing']['service_ms'], result['timing']['total_ms'])
            self.assertGreaterEqual(result['timing']['handler_total_ms'], result['timing']['service_ms'])

    def test_simple_task_changes_state_and_writes_complete_log(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / 'requests.jsonl'
            runtime = TaskRuntime(
                router=FixedRouter('local-rules'),
                backends={'local-rules': StaticBackend({'actions': [{'type': 'light_on', 'location': '\u5ba2\u5385', 'brightness': 80}]}, backend_name='local-rules')},
                store=DeviceStore(), log_path=log_path)
            result = runtime.handle('\u6253\u5f00\u5ba2\u5385\u7684\u706f', request_id='r1')
            self.assertTrue(result['ok'])
            self.assertTrue(result['execution']['changed'])
            self.assertEqual(result['route']['backend'], 'local-rules')
            self.assertTrue(result['timing']['total_ms'] >= 0)
            record = json.loads(log_path.read_text(encoding='utf-8').splitlines()[0])
            for key in ('request_id', 'route', 'observation', 'model_real', 'model_payload', 'model_error', 'state', 'timing', 'execution'):
                self.assertIn(key, record)
            self.assertIn('raw_response', record)
            self.assertEqual(record['model_payload'], {'actions': [{'type': 'light_on', 'location': '\u5ba2\u5385', 'brightness': 80}]})
            self.assertIsNone(record['model_error'])
            self.assertEqual(runtime.store.snapshot()['lights']['\u5ba2\u5385']['brightness'], 80)

    def test_failure_log_retains_model_payload_and_error(self):
        with tempfile.TemporaryDirectory() as td:
            payload = {'actions': [{'type': 'security_unlock', 'location': '\u4e0d\u5b58\u5728'}]}
            log_path = Path(td) / 'requests.jsonl'
            runtime = TaskRuntime(
                router=FixedRouter('cloud-model'),
                backends={'cloud-model': StaticBackend(payload)},
                store=DeviceStore(), log_path=log_path)
            result = runtime.handle('\u6253\u5f00\u4e0d\u5b58\u5728\u7684\u95e8', request_id='bad-payload')
            self.assertFalse(result['ok'])
            self.assertEqual(result['model_payload'], payload)
            self.assertIsNone(result['model_error'])
            record = json.loads(log_path.read_text(encoding='utf-8').splitlines()[0])
            self.assertEqual(record['model_payload'], payload)
            self.assertIn('state', record)

    def test_explicit_clarification_result_kind_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            payload = {'result_kind': 'clarification', 'actions': [], 'explanation': '\u9700\u8981\u77e5\u9053\u76ee\u6807\u4eae\u5ea6'}
            runtime = TaskRuntime(
                router=FixedRouter('cloud-model'),
                backends={'cloud-model': StaticBackend(payload)},
                store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            result = runtime.handle('\u628a\u706f\u8c03\u4eae\u4e00\u70b9', request_id='clarify')
            self.assertTrue(result['ok'])
            self.assertEqual(result['result_kind'], 'clarification')
            self.assertEqual(result['model_payload'], payload)

    def test_duplicate_request_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            backend = StaticBackend({'actions': [{'type': 'light_on', 'location': '\u5ba2\u5385', 'brightness': 40}]}, backend_name='local-rules')
            runtime = TaskRuntime(router=FixedRouter('local-rules'), backends={'local-rules': backend},
                                  store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            first = runtime.handle('\u6253\u5f00\u5ba2\u5385\u7684\u706f', request_id='same')
            second = runtime.handle('\u6253\u5f00\u5ba2\u5385\u7684\u706f', request_id='same')
            self.assertTrue(first['ok'])
            self.assertTrue(second['ok'])
            self.assertTrue(second['execution']['idempotent_replay'])
            self.assertEqual(backend.calls, 1)

    def test_invalid_model_output_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(router=FixedRouter('cloud-model'),
                                  backends={'cloud-model': StaticBackend({'actions': [{'type': 'security_unlock', 'location': '\u4e0d\u5b58\u5728'}]})},
                                  store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            result = runtime.handle('\u6253\u5f00\u4e0d\u5b58\u5728\u7684\u95e8', request_id='bad')
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_code'], 'invalid_model_output')
            self.assertEqual(runtime.store.snapshot()['lights']['\u5ba2\u5385']['is_on'], False)

    def test_model_outage_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(router=FixedRouter('cloud-model'),
                                  backends={'cloud-model': StaticBackend(None, available=False)},
                                  store=DeviceStore(), log_path=Path(td) / 'requests.jsonl')
            result = runtime.handle('\u628a\u5ba2\u5385\u8c03\u6697\u4e00\u70b9', request_id='down')
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_code'], 'model_unavailable')

    def test_deadline_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(router=FixedRouter('cloud-model'),
                                  backends={'cloud-model': StaticBackend({'actions': [{'type': 'light_on', 'location': '\u5ba2\u5385'}]})},
                                  store=DeviceStore(), log_path=Path(td) / 'requests.jsonl', default_deadline_ms=0)
            result = runtime.handle('\u6253\u5f00\u5ba2\u5385\u7684\u706f', request_id='late')
            self.assertFalse(result['ok'])
            self.assertEqual(result['error_code'], 'deadline_exceeded')


if __name__ == '__main__':
    unittest.main()
