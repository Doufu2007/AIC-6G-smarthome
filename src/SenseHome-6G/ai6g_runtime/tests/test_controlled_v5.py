"""Behavioral tests for the version-isolated resource experiment."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class ControlledV5Tests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('ai6g_runtime.evaluation.controlled_v5'),
                             'the independently versioned v5 runner is missing')
        return importlib.import_module('ai6g_runtime.evaluation.controlled_v5')

    def test_persistent_router_hysteresis_and_resume_keep_same_decision(self):
        m = self.module()
        from ai6g_runtime.protocol import classify_task
        from ai6g_runtime.router import HealthObservation
        task = replace(classify_task('打开客厅灯并关闭卧室灯'), quality_floor=.5)
        session = m.PolicySession('full_resource', m.ExperimentConfig())
        identity = id(session.router)
        for name, latency in [('local-model', 100), ('cloud-model', 102)]:
            session.publish(HealthObservation(name, True, latency, 1, 10))
        self.assertEqual(session.router.choose(task, now=10).backend, 'local-model')
        session.publish(HealthObservation('cloud-model', True, 98, 1, 10.1))
        held = session.router.choose(task, now=10.1)
        self.assertEqual(held.backend, 'local-model')
        self.assertIn('HYSTERESIS', held.reason)
        self.assertEqual(id(session.router), identity)
        restored = m.PolicySession('full_resource', m.ExperimentConfig(), session.snapshot())
        again = restored.router.choose(task, now=10.1)
        self.assertEqual(again.backend, 'local-model')
        self.assertIn('HYSTERESIS', again.reason)
        no_hyst = m.PolicySession('no_hysteresis', m.ExperimentConfig())
        for obs in [session.cache.get('local-model'), session.cache.get('cloud-model')]:
            no_hyst.publish(obs)
        self.assertEqual(no_hyst.router.choose(task, now=10.1).backend, 'cloud-model')

    def test_resource_ablation_changes_only_resource_gate_and_stale_is_ignored(self):
        m = self.module()
        from ai6g_runtime.protocol import classify_task
        from ai6g_runtime.router import HealthObservation, ResourceObservation
        task = replace(classify_task('打开客厅灯并关闭卧室灯'), quality_floor=.5)
        full = m.PolicySession('full_resource', m.ExperimentConfig())
        ablated = m.PolicySession('no_resource', m.ExperimentConfig())
        obs = HealthObservation('local-model', True, 100, 1, 10,
            resources=ResourceObservation(queue_ms=300, source='injected_fixture', observed_at=10))
        cloud = HealthObservation('cloud-model', True, 120, 1, 10)
        for session in (full, ablated):
            session.publish(obs)
            session.publish(cloud)
        self.assertEqual(full.router.choose(task, now=10).backend, 'cloud-model')
        self.assertEqual(ablated.router.choose(task, now=10).backend, 'local-model')
        self.assertIsNone(ablated.cache.get('local-model').resources)
        full.publish(replace(obs, resources=replace(obs.resources, observed_at=1)))
        self.assertEqual(full.router.choose(task, now=10).backend, 'local-model')

    def fixtures(self, folder):
        tasks = Path(folder)/'tasks.jsonl'
        rows = [{'id': str(i), 'text': '打开客厅灯', 'cluster_id': str(i),
                 'family': 'lighting', 'deadline_ms': 1000,
                 'gold': {'expected_action': [{'type': 'light_on', 'location': '客厅'}],
                          'expected_state': {'lights': {'客厅': {'is_on': True, 'brightness': 100}}}}}
                for i in range(2)]
        tasks.write_text(''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in rows), encoding='utf-8')
        calibration = Path(folder)/'calibration.json'
        calibration.write_text(json.dumps({'backends': {n: {'quality_score': 1., 'source_sha256': 'a'*64}
            for n in m_names()}}), encoding='utf-8')
        return tasks, calibration

    def test_run_resume_has_frozen_sources_no_gold_and_all_costs(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, cal = self.fixtures(td)
            cfg = m.ExperimentConfig(strategies=('full_resource', 'no_resource'),
                shared_rule_fastpath=False, bootstrap_samples=30)
            out = Path(td)/'result.json'
            partial = m.evaluate(tasks, out, calibration_path=cal, config=cfg, stop_after=1)
            self.assertFalse(partial['complete'])
            result = m.evaluate(tasks, out, calibration_path=cal, config=cfg, resume=True)
            self.assertTrue(result['complete'])
            self.assertEqual(len(result['rows']), 4)
            self.assertEqual(result['source_kind'], 'contract_simulator')
            self.assertIn('evaluation/controlled_v5.py', result['manifest']['sources'])
            self.assertFalse(result['manifest']['gold_online'])
            for row in result['rows']:
                self.assertEqual(row['cost']['total_tokens'], sum(c['total_tokens'] for c in row['calls']
                    if c['dispatched'] and c['backend'] != 'local-rules'))
                self.assertFalse(row['model_real'])
            tasks.write_text(tasks.read_text(encoding='utf-8').replace('1000', '1100'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                m.evaluate(tasks, out, calibration_path=cal, config=cfg, resume=True)

    def test_resource_trace_does_not_leak_future_and_rejects_bad_timestamps(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'trace.jsonl'
            p.write_text(json.dumps({'step': 2, 'backend': 'local-model',
                'resources': {'queue_ms': 900, 'age_ms': 0, 'source': 'injected_fixture'}})+'\n', encoding='utf-8')
            trace = m.ResourceTrace(p)
            self.assertIsNone(trace.resources_at(1, 'local-model', now_ms=1000))
            self.assertEqual(trace.resources_at(2, 'local-model', now_ms=2000).queue_ms, 900)
            p.write_text(json.dumps({'step': 0, 'backend': '*',
                'resources': {'queue_ms': -1, 'source': 'fixture'}})+'\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                m.ResourceTrace(p)

    def test_capture_then_replay_never_calls_model_and_accounts_captured_time(self):
        m = self.module()
        from ai6g_runtime.models import ModelResult
        from ai6g_runtime.health_monitor import ProbeTask
        class CaptureFixture:
            calls = 0
            model = 'fixture-not-a-real-model'
            endpoint = 'http://fixture.invalid'
            system_prompt = 'fixture'
            max_tokens = 384
            def infer(self, task, timeout_s):
                self.calls += 1
                self.assert_no_gold(task)
                return ModelResult(True, {'result_kind': 'clarification', 'actions': [], 'explanation': 'fixture'}
                    if isinstance(task, ProbeTask) else {'actions': [{'type': 'light_on', 'location': '客厅'}]},
                    raw_response={'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
            def assert_no_gold(self, task):
                if hasattr(task, 'gold'):
                    raise AssertionError('gold leaked')
        with tempfile.TemporaryDirectory() as td:
            tasks, cal = self.fixtures(td)
            capture = Path(td)/'capture.jsonl'
            models = {name: CaptureFixture() for name in m_names()}
            m.capture_outputs(tasks, capture, backends=models, trusted_backends=m_names())
            self.assertEqual(sum(b.calls for b in models.values()), 6)
            m.capture_outputs(tasks, capture, backends=models, trusted_backends=m_names(), resume=True)
            self.assertEqual(sum(b.calls for b in models.values()), 6)
            # Increase captured latency, not fixture wall latency: the replay must
            # consume the exact stored duration and prevent late device effects.
            captured = [json.loads(line) for line in capture.read_text(encoding='utf-8').splitlines()]
            for row in captured:
                row['elapsed_ms'] = 1500
            capture.write_text(''.join(json.dumps(x)+'\n' for x in captured), encoding='utf-8')
            cfg = m.ExperimentConfig(strategies=('fixed_local',), shared_rule_fastpath=False,
                trusted_backends=m_names(), bootstrap_samples=30)
            result = m.evaluate(tasks, Path(td)/'replay.json', config=cfg,
                calibration_path=cal, replay_path=capture)
            self.assertEqual(result['source_kind'], 'real_output_controlled_replay')
            self.assertEqual(sum(b.calls for b in models.values()), 6)
            self.assertTrue(all(row['state'] == row['initial_state'] for row in result['rows']))
            self.assertTrue(all(not row['deadline_correct'] for row in result['rows']))
            self.assertTrue(all(row['total_elapsed_ms'] >= 1000 for row in result['rows']))
            self.assertEqual(result['strategies']['fixed_local']['actual_endpoint_dispatches'], 0)
            self.assertEqual(result['capture_workload']['actual_endpoint_dispatches'], 6)
            self.assertEqual(result['capture_workload']['probe_dispatches'], 2)
            self.assertTrue(all(row['cost']['usd'] is None for row in result['rows']))
            self.assertTrue(all(row['cost']['unknown_usage_requests'] == 1 for row in result['rows']))
            for row in result['rows']:
                call = row['calls'][0]
                self.assertAlmostEqual(call['inference_wall_budget_ms']+call['injected_delay_ms'], call['elapsed_ms'])

    def test_capture_does_not_dispatch_strict_tasks_to_untrusted_aliases(self):
        m = self.module()
        from ai6g_runtime.models import ModelResult
        class Backend:
            calls = 0
            def infer(self, task, timeout_s):
                self.calls += 1
                return ModelResult(False, error='fixture')
        with tempfile.TemporaryDirectory() as td:
            tasks, _ = self.fixtures(td)
            text = tasks.read_text(encoding='utf-8').replace('打开客厅灯', '不要联网，打开客厅灯')
            tasks.write_text(text, encoding='utf-8')
            models = {name: Backend() for name in m_names()}
            rows = m.capture_outputs(tasks, Path(td)/'capture.jsonl', backends=models,
                trusted_backends=('local-model',), include_probes=False)
            self.assertEqual(models['cloud-model'].calls, 0)
            self.assertEqual(models['local-model'].calls, 2)
            self.assertEqual(sum(row['skipped'] for row in rows), 2)

    def test_runner_uses_current_telemetry_and_stale_age_from_first_step(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, cal = self.fixtures(td)
            trace = Path(td)/'resource.jsonl'
            trace.write_text(json.dumps({'step': 0, 'backend': 'local-model',
                'resources': {'queue_ms': 900, 'age_ms': 9000, 'source': 'injected_fixture'}})+'\n', encoding='utf-8')
            cfg = m.ExperimentConfig(strategies=('full_resource',), shared_rule_fastpath=False,
                bootstrap_samples=30)
            report = m.evaluate(tasks, Path(td)/'stale.json', config=cfg,
                calibration_path=cal, trace_path=trace)
            route = report['rows'][0]['route']
            self.assertNotIn('RESOURCE_CAPACITY', route['reason'])

    def test_incomplete_capture_is_rejected_before_arm_execution(self):
        m = self.module()
        from ai6g_runtime.models import ModelResult
        class Backend:
            def infer(self, task, timeout_s):
                return ModelResult(False, error='fixture')
        with tempfile.TemporaryDirectory() as td:
            tasks, cal = self.fixtures(td)
            capture = Path(td)/'partial.jsonl'
            m.capture_outputs(tasks, capture, backends={name: Backend() for name in m_names()}, stop_after=2)
            with self.assertRaisesRegex(ValueError, 'incomplete capture'):
                m.evaluate(tasks, Path(td)/'result.json', calibration_path=cal, replay_path=capture)

    def test_changed_weight_identity_rejects_capture_resume(self):
        m = self.module()
        from ai6g_runtime.models import ModelResult
        class Backend:
            def infer(self, task, timeout_s):
                return ModelResult(False, error='fixture')
        with tempfile.TemporaryDirectory() as td:
            tasks, _ = self.fixtures(td)
            identity = Path(td)/'identity.json'
            identity.write_text(json.dumps({'weights_sha256': 'a'*64, 'server_sha256': 'b'*64}), encoding='utf-8')
            output = Path(td)/'capture.jsonl'
            models = {name: Backend() for name in m_names()}
            m.capture_outputs(tasks, output, backends=models, identity_path=identity, stop_after=1)
            identity.write_text(json.dumps({'weights_sha256': 'c'*64, 'server_sha256': 'b'*64}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                m.capture_outputs(tasks, output, backends=models, identity_path=identity, resume=True)


def m_names():
    return ('local-model', 'cloud-model')


if __name__ == '__main__':
    unittest.main()
