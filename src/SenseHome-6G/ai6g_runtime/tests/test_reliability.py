import concurrent.futures
import json
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import ModelResult, RuleBackend
from ai6g_runtime.protocol import Action, ActionPlan
from ai6g_runtime.router import RouteDecision
from ai6g_runtime.runtime import TaskRuntime


class Fixed:
    def choose(self, task, now=None):
        return RouteDecision('local-rules', 'fixture', 1, 1, 0, False)


class SlowBackend:
    def __init__(self):
        self.calls = 0

    def infer(self, task, timeout_s):
        self.calls += 1
        time.sleep(.04)
        return ModelResult(True, {'actions': [{'type': 'light_on', 'location': '客厅'}]})


class ReliabilityTests(unittest.TestCase):
    def test_batch_failure_rolls_back_prior_action(self):
        store = DeviceStore()
        before = store.snapshot()
        plan = ActionPlan((Action('light_on', '客厅'), Action('light_on', '不存在')))
        self.assertFalse(store.apply(plan, 'batch').ok)
        self.assertEqual(store.snapshot(), before)

    def test_failed_atomic_write_does_not_commit_or_poison_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(Path(tmp)/'state.json')
            before = store.snapshot()
            plan = ActionPlan((Action('light_on', '客厅'),))
            with patch('ai6g_runtime.executor.os.replace', side_effect=OSError('disk error')):
                result = store.apply(plan, 'disk')
            self.assertFalse(result.ok)
            self.assertEqual(store.snapshot(), before)
            self.assertTrue(store.apply(plan, 'disk').ok)

    def test_duplicate_survives_restart_without_overwriting_new_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            first = DeviceStore(path)
            on = ActionPlan((Action('light_on', '客厅'),))
            off = ActionPlan((Action('light_off', '客厅'),))
            first.apply(on, 'old')
            first.apply(off, 'new')
            restored = DeviceStore(path)
            replay = restored.apply(on, 'old')
            self.assertTrue(replay.idempotent_replay)
            self.assertFalse(restored.snapshot()['lights']['客厅']['is_on'])

    def test_same_id_different_plan_is_rejected(self):
        store = DeviceStore()
        store.apply(ActionPlan((Action('light_on', '客厅'),)), 'same')
        out = store.apply(ActionPlan((Action('light_off', '客厅'),)), 'same')
        self.assertFalse(out.ok)
        self.assertEqual(out.error, 'request_id_conflict')
        self.assertTrue(store.snapshot()['lights']['客厅']['is_on'])

    def test_corrupt_persistent_store_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            path.write_text('{broken', encoding='utf8')
            with self.assertRaises(ValueError):
                DeviceStore(path)

    def test_concurrent_duplicate_only_infers_once_and_logs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = SlowBackend()
            r = TaskRuntime(router=Fixed(), backends={'local-rules': b}, log_path=Path(tmp)/'log.jsonl')
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda _: r.handle('打开客厅的灯', request_id='one'), range(8)))
            self.assertTrue(all(x['ok'] for x in results))
            self.assertEqual(b.calls, 1)
            self.assertEqual(len(r.logs()), 1)
            self.assertEqual(sum(bool(x.get('replay')) for x in results), 7)

    def test_duplicate_waits_for_active_owner_after_commit_before_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, log = Path(tmp) / 'state.json', Path(tmp) / 'requests.jsonl'
            runtime = TaskRuntime(router=Fixed(), backends={'local-rules': RuleBackend()},
                                  store=DeviceStore(state), log_path=log, default_deadline_ms=2000)
            entered, release = threading.Event(), threading.Event()
            original = runtime._append_log

            def pause(record):
                entered.set()
                if not release.wait(2):
                    raise OSError('test did not release audit append')
                return original(record)

            with patch.object(runtime, '_append_log', side_effect=pause), concurrent.futures.ThreadPoolExecutor(2) as pool:
                owner = pool.submit(runtime.handle, '打开客厅的灯', request_id='audit-race')
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(runtime.state()['lights']['客厅']['is_on'])
                    self.assertEqual(runtime.store.receipt('audit-race')['audit_status'], 'pending')
                    # A recovered runtime with no live owner must retain audit uncertainty.
                    recovered = TaskRuntime(router=Fixed(), backends={'local-rules': RuleBackend()},
                                            store=DeviceStore(state), log_path=log)
                    recovered_result = recovered.handle('打开客厅的灯', request_id='audit-race')
                    self.assertFalse(recovered_result['ok'])
                    self.assertEqual(recovered_result['error_code'], 'audit_unknown')
                    duplicate = pool.submit(runtime.handle, '打开客厅的灯', request_id='audit-race')
                    with self.assertRaises(concurrent.futures.TimeoutError):
                        duplicate.result(timeout=.05)
                finally:
                    release.set()
                first, replay = owner.result(2), duplicate.result(2)
            self.assertTrue(first['ok'])
            self.assertTrue(replay['ok'])
            self.assertEqual(replay['audit_status'], 'persisted')
            self.assertTrue(replay['execution']['idempotent_replay'])
            self.assertEqual(len(runtime.logs()), 1)

    def test_late_commit_fact_survives_audit_write_errors(self):
        for failure in ('append', 'mark'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                runtime = TaskRuntime(router=Fixed(), backends={'local-rules': RuleBackend()},
                                      store=DeviceStore(Path(tmp) / 'state.json'),
                                      log_path=Path(tmp) / 'requests.jsonl', default_deadline_ms=20)
                clock, real_replace = [100.0], os.replace

                def cross_deadline(src, dst):
                    real_replace(src, dst)
                    clock[0] += .04

                audit_failure = (patch.object(runtime, '_append_log', side_effect=OSError('audit disk full'))
                                 if failure == 'append' else
                                 patch.object(runtime.store, 'mark_audit', side_effect=OSError('receipt disk full')))
                with patch('ai6g_runtime.executor.time.perf_counter', side_effect=lambda: clock[0]), \
                     patch('ai6g_runtime.executor.os.replace', side_effect=cross_deadline), audit_failure:
                    result = runtime.handle('打开客厅的灯', request_id='late-audit')
                self.assertFalse(result['ok'])
                self.assertEqual(result['error_code'], 'audit_persist_failed' if failure == 'append' else 'audit_unknown')
                self.assertFalse(result['execution']['ok'])
                self.assertTrue(result['execution']['late_commit'])
                self.assertTrue(result['execution']['commit_succeeded'])
                self.assertTrue(result['execution']['changed'])
                self.assertTrue(result['commit_succeeded'])
                self.assertTrue(result['state']['lights']['客厅']['is_on'])

    def test_runtime_id_is_bound_to_text(self):
        b = SlowBackend()
        r = TaskRuntime(router=Fixed(), backends={'local-rules': b})
        r.handle('打开客厅的灯', request_id='one')
        out = r.handle('关闭客厅的灯', request_id='one')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error_code'], 'request_id_conflict')
        self.assertEqual(b.calls, 1)

    def test_whitespace_normalization_keeps_duplicate_binding(self):
        b = SlowBackend()
        r = TaskRuntime(router=Fixed(), backends={'local-rules': b})
        r.handle('  打开客厅的灯  ', request_id='spaces')
        repeated = r.handle('  打开客厅的灯  ', request_id='spaces')
        self.assertTrue(repeated['ok'])
        out = r.handle('打开客厅的灯', request_id='spaces')
        self.assertTrue(out['ok'])
        self.assertTrue(out.get('replay'))
        self.assertEqual(b.calls, 1)

    def test_audit_failure_is_explicit_after_commit(self):
        b = SlowBackend()
        r = TaskRuntime(router=Fixed(), backends={'local-rules': b}, log_path='unused.jsonl')
        with patch.object(r, '_append_log', side_effect=OSError('audit disk full')):
            out = r.handle('打开客厅的灯', request_id='audit')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error_code'], 'audit_persist_failed')
        self.assertTrue(out['commit_succeeded'])
        self.assertTrue(out['state']['lights']['客厅']['is_on'])
        self.assertTrue(r.handle('打开客厅的灯', request_id='audit').get('replay'))
        self.assertEqual(b.calls, 1)

    def test_timeout_after_budget_is_deadline_error(self):
        class TimeoutBackend:
            def infer(self, task, timeout_s):
                time.sleep(.02)
                return ModelResult(False, error='model endpoint unavailable: timed out')
        r = TaskRuntime(router=Fixed(), backends={'local-rules': TimeoutBackend()}, default_deadline_ms=5)
        out = r.handle('打开客厅的灯', request_id='timeout')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error_code'], 'deadline_exceeded')

    def test_atomic_replace_crossing_deadline_is_explicit_late_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(Path(tmp)/'state.json')
            plan = ActionPlan((Action('light_on', '客厅'),))
            original = __import__('os').replace
            def slow_replace(src, dst):
                time.sleep(.05)
                return original(src, dst)
            with patch('ai6g_runtime.executor.os.replace', side_effect=slow_replace):
                result = store.apply(plan, 'late', deadline_at=time.perf_counter()+.02)
            self.assertFalse(result.ok)
            self.assertEqual(result.error, 'late_commit')
            self.assertTrue(result.late_commit)
            self.assertTrue(store.snapshot()['lights']['客厅']['is_on'])

    def test_audit_failure_survives_restart_as_unknown_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state.json'
            log = Path(tmp)/'requests.jsonl'
            backend = SlowBackend()
            runtime = TaskRuntime(router=Fixed(), backends={'local-rules': backend},
                                  store=DeviceStore(state), log_path=log)
            with patch.object(runtime, '_append_log', side_effect=OSError('audit disk full')):
                first = runtime.handle('打开客厅的灯', request_id='audit-restart')
            self.assertEqual(first['error_code'], 'audit_persist_failed')
            restored = TaskRuntime(router=Fixed(), backends={'local-rules': backend},
                                   store=DeviceStore(state), log_path=log)
            replay = restored.handle('打开客厅的灯', request_id='audit-restart')
            self.assertFalse(replay['ok'])
            self.assertEqual(replay['error_code'], 'audit_unknown')
            self.assertTrue(replay['commit_succeeded'])

    def test_structurally_invalid_v2_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            path.write_text(json.dumps({'schema': 'ai6g.device_store.v2', 'state': None, 'receipts': {}}), encoding='utf8')
            with self.assertRaises(ValueError):
                DeviceStore(path)

    def test_runtime_restart_replays_durable_receipt_without_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            b = SlowBackend()
            r = TaskRuntime(router=Fixed(), backends={'local-rules': b}, store=DeviceStore(path))
            r.handle('打开客厅的灯', request_id='one')
            r2 = TaskRuntime(router=Fixed(), backends={'local-rules': b}, store=DeviceStore(path))
            out = r2.handle('打开客厅的灯', request_id='one')
            self.assertTrue(out['ok'])
            self.assertTrue(out['execution']['idempotent_replay'])
            self.assertEqual(out['result_origin'], 'durable_receipt')
            self.assertEqual(b.calls, 1)


if __name__ == '__main__':
    unittest.main()
