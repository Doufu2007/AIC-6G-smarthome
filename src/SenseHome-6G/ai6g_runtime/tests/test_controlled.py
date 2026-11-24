import importlib
import json
import sys
import tempfile
import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class ControlledEvaluationTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('ai6g_runtime.evaluation.controlled'),
                             'controlled evaluator must exist')
        return importlib.import_module('ai6g_runtime.evaluation.controlled')

    def fixture(self, folder):
        tasks = Path(folder) / 'tasks.jsonl'
        rows = [
            {'id': 'a', 'text': '打开客厅的灯', 'family': 'lighting', 'deadline_ms': 1000,
             'gold': {'expected_action': [{'type': 'light_on', 'location': '客厅'}],
                      'expected_state': {'lights': {'客厅': {'is_on': True, 'brightness': 100}}}}},
            {'id': 'b', 'text': '请关闭卧室灯', 'family': 'lighting', 'deadline_ms': 1000,
             'gold': {'expected_action': [{'type': 'light_off', 'location': '卧室'}],
                      'expected_state': {'lights': {'卧室': {'is_on': False, 'brightness': 0}}}}},
        ]
        tasks.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in rows)+'\n', encoding='utf-8')
        calibration = Path(folder) / 'calibration.json'
        calibration.write_text(json.dumps({'backends': {
            name: {'quality_score': 1.0, 'source_sha256': 'a'*64}
            for name in ('local-model', 'cloud-model')}}), encoding='utf-8')
        return tasks, calibration

    def test_completed_rows_resume_without_repeating_calls_and_freeze_inputs(self):
        # Repeating calls after resume or accepting mutated gold must break this.
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            output = Path(td)/'report.json'
            cfg = m.ExperimentConfig(strategies=('fixed_ttl', 'adaptive'), repeats=2,
                                     shared_rule_fastpath=False, bootstrap_samples=40)
            m.evaluate(tasks, output, calibration_path=calibration, config=cfg, stop_after=3)
            resumed = m.evaluate(tasks, output, calibration_path=calibration, config=cfg, resume=True)
            fresh = m.evaluate(tasks, Path(td)/'fresh.json', calibration_path=calibration, config=cfg)
            self.assertTrue(resumed['complete'])
            self.assertEqual(len(resumed['rows']), 8)
            self.assertEqual(len({row['row_id'] for row in resumed['rows']}), 8)
            self.assertEqual([r['call_count'] for r in resumed['rows']], [r['call_count'] for r in fresh['rows']])
            self.assertEqual(resumed['strategy_order'][0], ['fixed_ttl', 'adaptive'])
            self.assertEqual(resumed['strategy_order'][1], ['adaptive', 'fixed_ttl'])
            tasks.write_text(tasks.read_text(encoding='utf-8').replace('1000', '999'), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'fingerprint|frozen'):
                m.evaluate(tasks, output, calibration_path=calibration, config=cfg, resume=True)

    def test_gold_remains_offline_and_initial_state_resets_for_every_arm(self):
        m = self.module()
        from ai6g_runtime.models import ModelResult
        class Backend:
            name = 'fixture'
            def infer(self, task, timeout_s):
                if hasattr(task, 'gold') or hasattr(task, 'expected_action'):
                    raise AssertionError('gold leaked into backend')
                # Constant independently specified response, unrelated to gold.
                return ModelResult(True, {'actions': [{'type': 'light_on', 'location': '客厅'}]},
                                   backend=self.name, raw_response={'usage': {'total_tokens': 7}})
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            cfg = m.ExperimentConfig(strategies=('fixed_local', 'fixed_cloud'), repeats=1,
                                     shared_rule_fastpath=False, bootstrap_samples=40)
            result = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration, config=cfg,
                                backends={'local-model': Backend(), 'cloud-model': Backend()})
            self.assertTrue(all(not row['initial_state']['lights']['客厅']['is_on'] for row in result['rows']))
            self.assertEqual([r['semantic_success'] for r in result['rows']], [True, True, False, False])
            self.assertEqual([r['wrong_side_effect'] for r in result['rows']], [False, False, True, True])
            self.assertEqual(result['source_kind'], 'contract_simulator')
            self.assertEqual(result['strategies']['fixed_local']['model_real_rows'], 0)

    def test_probe_cost_is_in_deadline_and_late_response_cannot_execute(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            trace = Path(td)/'trace.jsonl'
            trace.write_text(json.dumps({'step': 0, 'backend': '*', 'rtt_ms': 600})+'\n', encoding='utf-8')
            cfg = m.ExperimentConfig(strategies=('adaptive',), repeats=1, shared_rule_fastpath=False,
                                     bootstrap_samples=40)
            report = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration,
                                trace_path=trace, config=cfg)
            row = report['rows'][0]
            self.assertFalse(row['deadline_correct'])
            self.assertEqual(row['error_code'], 'deadline_exceeded')
            self.assertEqual(row['state'], row['initial_state'])
            self.assertGreaterEqual(row['total_elapsed_ms'], 1000)
            self.assertGreater(row['cost']['probe_requests'], 0)
            self.assertGreater(row['cost']['total_tokens'], 0)
            self.assertEqual(row['cost']['total_tokens'], sum(c['total_tokens'] for c in row['calls']))

    def test_trace_disconnect_recovers_without_exposing_future_to_policy(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            trace = Path(td)/'trace.jsonl'
            trace.write_text('\n'.join(json.dumps(x) for x in [
                {'step': 0, 'backend': 'local-model', 'reachable': False, 'rtt_ms': 5},
                {'step': 1, 'backend': 'local-model', 'reachable': True, 'rtt_ms': 9, 'queue_ms': 2},
            ])+'\n', encoding='utf-8')
            cfg = m.ExperimentConfig(strategies=('fixed_local',), repeats=1,
                                     shared_rule_fastpath=False, bootstrap_samples=40)
            r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration, trace_path=trace, config=cfg)
            self.assertFalse(r['rows'][0]['engineering_ok'])
            self.assertTrue(r['rows'][1]['engineering_ok'])
            self.assertFalse(r['rows'][0]['calls'][0]['trace']['reachable'])
            self.assertEqual(r['rows'][1]['calls'][0]['trace']['rtt_ms'], 9)
            self.assertGreaterEqual(r['rows'][1]['total_elapsed_ms'], 11)

    def test_paired_bootstrap_uses_task_clusters_and_requires_complete_pairs(self):
        m = self.module()
        rows = [{'task_id': task, 'repeat': rep, 'strategy': arm, 'deadline_correct': good}
                for task in ('a','b') for rep in range(3)
                for arm, good in [('adaptive', True), ('fixed_local', False)]]
        summary = m.paired_bootstrap(rows, 'adaptive', 'fixed_local', samples=100, seed=4)
        self.assertEqual(summary['clusters'], 2)
        self.assertEqual(summary['estimate'], 1.0)
        self.assertEqual(summary['ci95'], [1.0, 1.0])
        with self.assertRaisesRegex(ValueError, 'pair'):
            m.paired_bootstrap(rows[:-1], 'adaptive', 'fixed_local', samples=100, seed=4)

    def test_task_freeze_rejects_duplicate_ids_and_online_requires_real_config(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            first = tasks.read_text(encoding='utf-8').splitlines()[0]
            tasks.write_text(first+'\n'+first+'\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                m.load_frozen_tasks(tasks)

    def test_all_arms_share_rule_fastpath_and_never_send_health_probes(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration,
                           config=m.ExperimentConfig(bootstrap_samples=20))
            self.assertEqual(len(r['rows']), 18)
            self.assertTrue(all(row['semantic_success'] for row in r['rows']))
            self.assertTrue(all(row['cost']['model_requests'] == 0 for row in r['rows']))
            self.assertTrue(all(row['cost']['probe_requests'] == 0 for row in r['rows']))

    def test_cluster_id_keeps_paraphrases_together(self):
        m = self.module()
        rows = [{'task_id': task, 'cluster_id': 'one-template', 'repeat': rep,
                 'strategy': arm, 'deadline_correct': observed}
                for task, success in [('a', True), ('b', False)] for rep in range(3)
                for arm, observed in [('adaptive', success), ('fixed_local', False)]]
        r = m.paired_bootstrap(rows, 'adaptive', 'fixed_local', samples=20)
        self.assertEqual(r['clusters'], 1)
        self.assertEqual(r['estimate'], .5)
        self.assertEqual(r['ci95'], [.5, .5])

    def test_openai_http_boundary_preserves_usage_and_gold_is_not_transmitted(self):
        m = self.module()
        from ai6g_runtime.models import OpenAICompatibleBackend
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                payload = {'actions': [{'type': 'light_on', 'location': '客厅'}]}
                raw = json.dumps({'choices': [{'message': {'content': json.dumps(payload)}}],
                                  'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                tasks, calibration = self.fixture(td)
                first = json.loads(tasks.read_text(encoding='utf-8').splitlines()[0])
                first['gold']['marker'] = 'GOLD_SECRET_123'
                tasks.write_text(json.dumps(first, ensure_ascii=False)+'\n', encoding='utf-8')
                endpoint = f'http://127.0.0.1:{server.server_port}/v1/chat/completions'
                backend = OpenAICompatibleBackend('local-model', endpoint, 'fixture')
                cfg = m.ExperimentConfig(strategies=('fixed_local',), online=True,
                    shared_rule_fastpath=False, bootstrap_samples=20,
                    input_usd_per_million=2, output_usd_per_million=3)
                r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration,
                    config=cfg, backends={'local-model': backend})
                self.assertTrue(r['rows'][0]['semantic_success'])
                self.assertTrue(r['rows'][0]['model_real'])
                self.assertEqual(r['rows'][0]['cost']['provider_tokens'], 5)
                self.assertAlmostEqual(r['rows'][0]['cost']['usd'], .000013)
                self.assertNotIn('GOLD_SECRET_123', json.dumps(requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_resource_cost_has_cluster_paired_intervals(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            cfg = m.ExperimentConfig(strategies=('fixed_local', 'adaptive'),
                                     shared_rule_fastpath=False, bootstrap_samples=20)
            r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration, config=cfg)
            metrics = {c['metric'] for c in r['comparisons']}
            self.assertIn('total_tokens', metrics)
            self.assertIn('model_requests', metrics)

    def test_resume_repairs_a_complete_crash_tail_without_newline(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            output = Path(td)/'out.json'
            cfg = m.ExperimentConfig(strategies=('fixed_local',), shared_rule_fastpath=False,
                                     bootstrap_samples=20)
            m.evaluate(tasks, output, calibration_path=calibration, config=cfg, stop_after=1)
            journal = output.with_suffix('.rows.jsonl')
            journal.write_bytes(journal.read_bytes().rstrip(b'\r\n'))
            m.evaluate(tasks, output, calibration_path=calibration, config=cfg, resume=True)
            lines = journal.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)['task_id'] for line in lines], ['a', 'b'])

    def test_disconnected_proxy_attempt_is_not_a_dispatched_model_request(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            trace = Path(td)/'trace.jsonl'
            trace.write_text(json.dumps({'step': 0, 'backend': '*', 'reachable': False})+'\n', encoding='utf-8')
            cfg = m.ExperimentConfig(strategies=('fixed_local',), shared_rule_fastpath=False,
                                     bootstrap_samples=20)
            r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration,
                           trace_path=trace, config=cfg)
            self.assertEqual(r['rows'][0]['cost']['model_requests'], 0)
            self.assertEqual(r['rows'][0]['cost']['model_attempts'], 1)

    def test_predeclared_primary_pairs_include_legal_cloud_and_fixed_ttl(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as td:
            tasks, calibration = self.fixture(td)
            cfg = m.ExperimentConfig(strategies=('fixed_local', 'legal_cloud', 'fixed_ttl', 'adaptive'),
                                     bootstrap_samples=20)
            r = m.evaluate(tasks, Path(td)/'out.json', calibration_path=calibration, config=cfg)
            primary = {(c['arm'], c['reference'], c['metric']) for c in r['comparisons']
                       if c.get('comparison_role') == 'primary'}
            self.assertEqual(primary, {('adaptive', 'legal_cloud', 'deadline_correct'),
                                       ('adaptive', 'fixed_ttl', 'deadline_correct')})


if __name__ == '__main__':
    unittest.main()
