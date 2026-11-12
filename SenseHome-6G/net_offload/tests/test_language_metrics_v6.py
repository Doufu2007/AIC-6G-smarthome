"""V6 bounded-language and planner-quality regressions; no real endpoints."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
NET = ROOT / 'SenseHome-6G/net_offload'
sys.path.insert(0, str(NET))
from online_metrics import OnlineMetrics, task_key
from target_contract import request_goals, validate_goal_plan


class BoundedLanguageV6Tests(unittest.TestCase):
    def test_equivalent_explicit_expressions_have_identical_goals(self):
        expected = [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]
        for command in ('麻烦把客厅灯光设为60%', '把客厅的灯调成60%',
                        '将客厅灯亮度设为百分之六十'):
            with self.subTest(command=command):
                self.assertEqual(request_goals(command), expected)

    def test_chinese_percent_boundaries_and_order_match_whole_request(self):
        for text, number in (('零', 0), ('十', 10), ('十一', 11), ('二十', 20),
                             ('九十九', 99), ('一百', 100)):
            with self.subTest(text=text):
                plan = request_goals('将客厅灯亮度设为百分之' + text)
                self.assertEqual(plan[0]['params']['brightness'], number)
        command = '麻烦把客厅灯光设为60%，再把卧室的灯调成30%'
        plan = request_goals(command)
        self.assertEqual([a['params']['location'] for a in plan], ['客厅', '卧室'])
        self.assertEqual([a['params']['brightness'] for a in plan], [60, 30])
        self.assertIsNone(validate_goal_plan(command, plan))
        self.assertEqual(validate_goal_plan(command, plan[:1]), 'REQUEST_GOAL_PLAN_MISMATCH')

    def test_partial_risky_or_conflicting_requests_remain_unresolved(self):
        for command in ('如果客厅有人，把客厅灯调成60%', '不要把客厅灯调成60%',
                        '把客厅灯调成101%', '把客厅灯调成-1%', '把客厅灯调成60.5%',
                        '把客厅灯调成百分之一百零一', '把客厅灯调成百分之六零',
                        '把客厅灯调成百分之零十', '把客厅灯调成60%并打开阳台灯',
                        '把客厅灯调成60%或者30%', '打开客厅灯并关闭客厅灯',
                        '把客厅灯光设为60%，把客厅灯光设为30%'):
            with self.subTest(command=command):
                self.assertIsNone(request_goals(command))

    def test_explicit_temporal_steps_keep_repeated_device_order(self):
        plan = request_goals('打开客厅灯然后关闭客厅灯')
        self.assertEqual([a['params']['action'] for a in plan], ['on', 'off'])


class PlannerMetricsV6Tests(unittest.TestCase):
    def test_all_attempt_costs_and_valid_plans_have_separate_counters(self):
        now = [0.]
        metrics = OnlineMetrics(clock=lambda: now[0])
        command = '把客厅灯光设为60%'
        for success, valid in ((True, False), (False, False), (True, True)):
            token = metrics.waiting(command)
            metrics.admitted(token)
            now[0] += .1
            metrics.finished(token, success=success, valid_plan=valid)
        sample = metrics.snapshot()['cost_by_task'][task_key(command)]
        self.assertEqual(sample['samples'], 3)
        self.assertEqual(sample['planner_attempts'], 3)
        self.assertEqual(sample['planner_completed_attempts'], 3)
        self.assertEqual(sample['planner_response_ok'], 2)
        self.assertEqual(sample['valid_plans'], 1)
        self.assertAlmostEqual(sample['valid_plan_rate'], 1 / 3)
        self.assertEqual(sample['scope'], 'planner_call_only')
        self.assertFalse(sample['includes_device_execution'])
        self.assertEqual(sample['quality_inference'], 'descriptive_counts_only')

    def test_legacy_success_does_not_assert_plan_validity(self):
        metrics = OnlineMetrics()
        token = metrics.waiting('打开客厅灯')
        metrics.admitted(token)
        metrics.finished(token, success=True)
        sample = metrics.snapshot()['cost_by_task']['control_single']
        self.assertEqual(sample['planner_response_ok'], 1)
        self.assertEqual(sample['valid_plans'], 0)

    def test_direct_empty_plans_have_cost_samples_but_zero_valid_rate(self):
        with ExitStack() as stack:
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            with patch.dict(os.environ, {
                'SENSEHOME_POLICY_CLI': str(folder / 'absent-policy'),
                'SENSEHOME_STATE_FILE': str(folder / 'state.json'),
                'SENSEHOME_EVENT_LOG': str(folder / 'events.jsonl'),
                'SENSEHOME_REQUEST_DB': str(folder / 'requests.sqlite3'),
                'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
            }):
                spec = importlib.util.spec_from_file_location('v6_planner_metrics_fused', ROOT / 'docs/deployment/fused_eval.py')
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                stack.callback(sys.modules.pop, spec.name, None)
                spec.loader.exec_module(module)
            backend = module.DirectModelBackend('http://127.0.0.1:1/v1', 'fixture', 'local', evidence_kind='fixture')
            empty = {'choices': [{'message': {'content': json.dumps({'answer': '', 'actions': []})}}]}
            with patch.object(module.RecordingService, '_upstream', return_value=(empty, 1.)):
                for _ in range(2):
                    result = backend.infer('把客厅灯光设为60%')
                    self.assertTrue(result.ok)
                    self.assertEqual(result.actions, [])
            sample = backend.resource_snapshot()['cost_by_task']['control_single']
            self.assertEqual(sample['samples'], 2)
            self.assertEqual(sample['valid_plans'], 0)
            self.assertEqual(sample['valid_plan_rate'], 0)
            self.assertEqual(sample['planner_response_ok'], 2)
            wrong = {'choices': [{'message': {'content': json.dumps({'answer': '', 'actions': [
                {'name': 'light_control', 'params': {'location': '卧室', 'brightness': 60}}]})}}]}
            with patch.object(module.RecordingService, '_upstream', return_value=(wrong, 1.)):
                self.assertFalse(backend.infer('把客厅灯光设为60%').ok)
            complete = {'choices': [{'message': {'content': json.dumps({'answer': '', 'actions': [
                {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]})}}]}
            with patch.object(module.RecordingService, '_upstream', return_value=(complete, 1.)):
                planned = backend.infer('把客厅灯光设为60%')
                self.assertTrue(planned.ok)
                self.assertFalse(planned.semantic_complete)
            sample = backend.resource_snapshot()['cost_by_task']['control_single']
            self.assertEqual(sample['samples'], 4)
            self.assertEqual(sample['valid_plans'], 1)
            self.assertEqual(sample['valid_plan_rate'], .25)
            self.assertEqual(sample['planner_response_ok'], 3)

    def test_rule_corrected_read_plans_do_not_gain_raw_model_quality_credit(self):
        with ExitStack() as stack:
            folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            with patch.dict(os.environ, {
                'SENSEHOME_POLICY_CLI': str(folder / 'absent-policy'),
                'SENSEHOME_STATE_FILE': str(folder / 'state.json'),
                'SENSEHOME_EVENT_LOG': str(folder / 'events.jsonl'),
                'SENSEHOME_REQUEST_DB': str(folder / 'requests.sqlite3'),
                'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
            }):
                spec = importlib.util.spec_from_file_location('v6_corrected_read_metrics_fused', ROOT / 'docs/deployment/fused_eval.py')
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                stack.callback(sys.modules.pop, spec.name, None)
                spec.loader.exec_module(module)
            backend = module.DirectModelBackend('http://127.0.0.1:1/v1', 'fixture', 'local', evidence_kind='fixture')
            expected = [{'name': 'read_temperature', 'params': {'location': '客厅'}}]
            for raw in ([], [{'name': 'read_temperature', 'params': {'location': '卧室'}}]):
                with self.subTest(raw=raw):
                    upstream = {'choices': [{'message': {'content': json.dumps({'answer': '', 'actions': raw})}}]}
                    with patch.object(module.RecordingService, '_upstream', return_value=(upstream, 1.)):
                        result = backend.infer('查看客厅温度')
                    self.assertTrue(result.ok)
                    self.assertEqual(result.actions, expected)
                    self.assertEqual(backend.attempts[-1]['response']['raw_model_actions'], raw)
                    self.assertTrue(backend.attempts[-1]['response']['action_normalization']['changed'])
                    sample = backend.resource_snapshot()['cost_by_task']['read_single']
                    self.assertEqual(sample['valid_plans'], 0)
                    self.assertEqual(sample['valid_plan_rate'], 0)
            sample = backend.resource_snapshot()['cost_by_task']['read_single']
            self.assertEqual(sample['samples'], 2)
            self.assertEqual(sample['planner_response_ok'], 2)
            upstream = {'choices': [{'message': {'content': json.dumps({'answer': '', 'actions': expected})}}]}
            with patch.object(module.RecordingService, '_upstream', return_value=(upstream, 1.)):
                self.assertTrue(backend.infer('查看客厅温度').ok)
            sample = backend.resource_snapshot()['cost_by_task']['read_single']
            self.assertEqual(sample['samples'], 3)
            self.assertEqual(sample['valid_plans'], 1)
            self.assertAlmostEqual(sample['valid_plan_rate'], 1 / 3)


if __name__ == '__main__':
    unittest.main()
