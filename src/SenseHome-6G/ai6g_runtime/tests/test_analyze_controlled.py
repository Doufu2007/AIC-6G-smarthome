"""Analysis must not turn probe evidence or safe failure into model success."""
import copy
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class AnalyzeControlledTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('ai6g_runtime.evaluation.analyze_controlled'))
        return importlib.import_module('ai6g_runtime.evaluation.analyze_controlled')

    def fixture(self):
        arms = ['legal_cloud', 'fixed_ttl', 'adaptive']
        tasks = [
            {'id': 'a', 'text': '火灾发生时请锁上客厅门', 'gold': {'must_reject': True}},
            {'id': 'b', 'text': '不要上传任何信息，打开客厅灯', 'gold': {'expected_action': [{}]}},
            {'id': 'c', 'text': '请把客厅灯调整到合适亮度', 'gold': {'allow_clarification': True}},
        ]
        rows = []
        for index, task in enumerate(tasks):
            for arm in arms:
                good = index == 0
                probe = {'backend': 'cloud-model', 'phase': 'probe', 'dispatched': True,
                         'model_real': True, 'total_tokens': 9}
                rows.append({'row_id': f'0:{task["id"]}:{arm}', 'task_id': task['id'],
                    'task_text': task['text'], 'strategy': arm, 'repeat': 0, 'trace_step': index,
                    'cluster_id': task['id'], 'family': 'lighting', 'engineering_ok': good,
                    'semantic_success': good, 'deadline_correct': good, 'wrong_side_effect': False,
                    'result_kind': 'refusal' if good else 'failure', 'plan': None,
                    'state': {'x': 0}, 'initial_state': {'x': 0},
                    'error_code': None if good else ('privacy_violation' if index == 1 else 'route_unavailable'),
                    'route': {'backend': 'local-rules' if good else 'unavailable'},
                    'calls': [probe] if index == 2 else [], 'total_elapsed_ms': index + 1,
                    'cost': {'model_requests': int(index == 2), 'model_attempts': int(index == 2),
                        'probe_requests': int(index == 2), 'probe_attempts': int(index == 2),
                        'total_tokens': 9 if index == 2 else 0, 'provider_tokens': 9 if index == 2 else 0,
                        'probe_tokens': 9 if index == 2 else 0, 'unknown_usage_requests': 0,
                        'probe_elapsed_ms': 2 if index == 2 else 0, 'model_elapsed_ms': 2 if index == 2 else 0,
                        'network_bytes': 20 if index == 2 else 0, 'usd': None if index == 2 else 0}})
        report = {'schema': 'ai6g.controlled.v1', 'source_kind': 'contract_simulator',
            'complete': True, 'task_count': 3, 'repeats': 1, 'fingerprint': 'fixture',
            'manifest': {'config': {'strategies': arms, 'bootstrap_samples': 30, 'seed': 4,
                                    'shared_rule_fastpath': True, 'shared_safety_fastpath': True,
                                    'trusted_backends': []}}, 'rows': rows}
        return report, tasks

    def test_incomplete_and_duplicate_rows_are_rejected_before_reporting(self):
        m = self.module()
        report, tasks = self.fixture()
        report['complete'] = False
        with self.assertRaisesRegex(ValueError, 'complete|完整'):
            m.analyze_report(report, tasks=tasks)
        report['complete'] = True
        report['rows'][-1] = copy.deepcopy(report['rows'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate|重复|coverage|覆盖'):
            m.analyze_report(report, tasks=tasks)

    def test_probe_real_is_not_task_real_and_safe_failure_is_not_refusal(self):
        m = self.module()
        report, tasks = self.fixture()
        analysis = m.analyze_report(report, tasks=tasks,
            trace_events=[{'step': 0, 'backend': '*', 'reachable': True},
                          {'step': 2, 'backend': 'cloud-model', 'reachable': False}])
        s = analysis['strategies']['adaptive']
        self.assertEqual(s['task_real_rows'], 0)
        self.assertEqual(s['probe_real_rows'], 1)
        self.assertEqual(s['refusal_protocol_ok'], 1)
        self.assertEqual(s['clarification_protocol_ok'], 0)
        self.assertEqual(s['no_action_fault'], 2)
        self.assertEqual(s['privacy_blocked'], 1)
        self.assertEqual(s['cost']['total_tokens'], 9)
        self.assertEqual(s['cost']['probe_tokens'], 9)
        self.assertEqual(s['cost']['network_bytes'], 20)
        self.assertIsNone(s['cost']['usd'])
        self.assertEqual(s['cost']['unknown_money_rows'], 1)
        self.assertEqual(len(analysis['phases']), 2)
        self.assertTrue(analysis['simulator_only'])

    def test_two_primary_pairs_use_paired_clusters_and_report_both(self):
        m = self.module()
        report, tasks = self.fixture()
        a = m.analyze_report(report, tasks=tasks)
        self.assertEqual([(x['arm'], x['reference']) for x in a['primary_comparisons']],
                         [('adaptive', 'legal_cloud'), ('adaptive', 'fixed_ttl')])
        self.assertEqual([x['clusters'] for x in a['primary_comparisons']], [3, 3])
        self.assertEqual([x['estimate'] for x in a['primary_comparisons']], [0, 0])

    def test_calibration_separates_shared_fastpath_from_model_delegation(self):
        m = self.module()
        report, tasks = self.fixture()
        cal_tasks = [{'id': 'p', 'text': '别打开客厅的灯'},
                     {'id': 'd', 'text': '请把客厅灯调到合适亮度'}]
        cal = [{'backend': backend, 'task_id': identity, 'family': 'lighting',
                'task_source_sha256': 'x', 'semantic_success': identity == 'p', 'model_real': True}
               for backend in ('local-model', 'cloud-model') for identity in ('p', 'd')]
        a = m.analyze_report(report, cal, tasks=tasks, calibration_tasks=cal_tasks)
        g = a['calibration']['local-model']['lighting']
        self.assertEqual(g['all']['n'], 2)
        self.assertEqual(g['all']['successes'], 1)
        self.assertEqual(g['shared_fastpath']['n'], 1)
        self.assertEqual(g['delegated']['n'], 1)
        self.assertEqual(g['delegated']['rate'], 0)

    def test_classifier_version_mismatch_cannot_relabel_privacy_or_calibration(self):
        m = self.module()
        report, tasks = self.fixture()
        report['manifest']['sources'] = {'protocol.py': '0'*64}
        with self.assertRaisesRegex(ValueError, 'classifier|分类'):
            m.analyze_report(report, tasks=tasks)


if __name__ == '__main__':
    unittest.main()
