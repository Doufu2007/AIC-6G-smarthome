import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.evaluator import evaluate_gold, semantic_score_row, task_set_sha256
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import RuleBackend
from ai6g_runtime.router import TaskRouter, ObservationCache
from ai6g_runtime.runtime import TaskRuntime


class GoldEvaluatorTests(unittest.TestCase):
    def _factory(self, td):
        def factory(run_index=0):
            return TaskRuntime(
                router=TaskRouter(ObservationCache()),
                backends={"local-rules": RuleBackend()},
                store=DeviceStore(),
                log_path=Path(td) / f"run-{run_index}.jsonl",
            )
        return factory

    def test_engineering_ok_with_wrong_action_is_not_semantically_correct(self):
        row = {"ok": True, "execution": {"ok": True}, "plan": {"actions": [
            {"type": "light_off", "location": "客厅"}]},
            "state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}}}
        gold = {"expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}],
                "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
                "allow_clarification": False, "must_reject": False}
        score = semantic_score_row(row, gold)
        self.assertEqual(score["status"], "incorrect")
        self.assertFalse(score["semantic_success"])

    def test_extra_action_fails_exact_action_match(self):
        row = {"ok": True, "execution": {"ok": True}, "plan": {"actions": [
            {"type": "light_on", "location": "客厅", "brightness": 100},
            {"type": "curtain_open", "location": "客厅"}]},
            "state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}}}
        gold = {"expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}],
                "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
                "allow_clarification": False, "must_reject": False}
        self.assertFalse(semantic_score_row(row, gold)["semantic_success"])

    def test_action_order_is_ignored_when_actions_and_state_match(self):
        row = {"ok": True, "execution": {"ok": True}, "plan": {"actions": [
            {"type": "security_lock", "location": "卧室"},
            {"type": "light_off", "location": "客厅"},
        ]}, "state": {"lights": {"客厅": {"is_on": False, "brightness": 0}},
                            "security": {"卧室": {"locked": True}}}}
        gold = {"expected_action": [
            {"type": "light_off", "location": "客厅"},
            {"type": "security_lock", "location": "卧室"},
        ], "expected_state": {"lights": {"客厅": {"is_on": False, "brightness": 0}},
                               "security": {"卧室": {"locked": True}}},
                "allow_clarification": False, "must_reject": False}
        self.assertTrue(semantic_score_row(row, gold)["semantic_success"])

    def test_light_on_missing_brightness_uses_protocol_default(self):
        row = {"ok": True, "execution": {"ok": True}, "plan": {"actions": [
            {"type": "light_on", "location": "客厅"}]},
            "state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}}}
        gold = {"expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}],
                "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
                "allow_clarification": False, "must_reject": False}
        self.assertTrue(semantic_score_row(row, gold)["semantic_success"])

    def test_illegal_state_side_effect_fails_even_when_required_state_matches(self):
        row = {"ok": True, "execution": {"ok": True}, "plan": {"actions": [
            {"type": "light_on", "location": "\u5ba2\u5385", "brightness": 100}]},
            "initial_state": {"lights": {"\u5ba2\u5385": {"is_on": False, "brightness": 0}, "\u5367\u5ba4": {"is_on": False, "brightness": 0}}},
            "state": {"lights": {"\u5ba2\u5385": {"is_on": True, "brightness": 100}, "\u5367\u5ba4": {"is_on": True, "brightness": 100}}}}
        gold = {"expected_action": [{"type": "light_on", "location": "\u5ba2\u5385", "brightness": 100}],
                "expected_state": {"lights": {"\u5ba2\u5385": {"is_on": True, "brightness": 100}}},
                "allow_clarification": False, "must_reject": False}
        score = semantic_score_row(row, gold)
        self.assertEqual(score["status"], "incorrect")
        self.assertEqual(score["reason"], "illegal_side_effect")

    def test_clarification_and_rejection_are_not_fabricated_as_success(self):
        for gold in (
            {"expected_action": [], "allow_clarification": True, "must_reject": False, "expected_state": {}},
            {"expected_action": [], "allow_clarification": False, "must_reject": True, "expected_state": {}},
        ):
            row = {"ok": False, "error_code": "model_failed", "execution": {"ok": False}, "plan": None}
            score = semantic_score_row(row, gold)
            self.assertIn(score["status"], {"failed", "not_evaluable"})
            self.assertFalse(score["semantic_success"])

    def test_explicit_refusal_with_state_side_effect_is_incorrect(self):
        row = {
            "ok": True,
            "execution": {"ok": True},
            "result_kind": "refusal",
            "plan": {"actions": []},
            "initial_state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}},
            "state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
        }
        gold = {"expected_action": [], "allow_clarification": False,
                "must_reject": True, "expected_state": {}}
        score = semantic_score_row(row, gold)
        self.assertEqual(score["reason"], "illegal_side_effect")
        self.assertFalse(score["semantic_success"])

    def test_protocol_refusal_is_semantically_correct_for_must_reject_gold(self):
        row = {
            "ok": True,
            "execution": {"ok": True},
            "result_kind": "refusal",
            "plan": {"actions": []},
            "initial_state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}},
            "state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}},
        }
        gold = {"expected_action": [], "allow_clarification": False,
                "must_reject": True, "expected_state": {}}
        score = semantic_score_row(row, gold)
        self.assertTrue(score["semantic_success"])
        self.assertEqual(score["status"], "semantic_success")

    def test_protocol_clarification_is_semantically_correct_when_allowed(self):
        row = {
            "ok": True,
            "execution": {"ok": True},
            "result_kind": "clarification",
            "plan": {"actions": []},
            "initial_state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}},
            "state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}},
        }
        gold = {"expected_action": [], "allow_clarification": True,
                "must_reject": False, "expected_state": {}}
        score = semantic_score_row(row, gold)
        self.assertTrue(score["semantic_success"])
        self.assertEqual(score["status"], "semantic_success")

    def test_missing_expected_refusal_or_clarification_is_incorrect(self):
        for gold in (
            {"expected_action": [], "allow_clarification": False, "must_reject": True, "expected_state": {}},
            {"expected_action": [], "allow_clarification": True, "must_reject": False, "expected_state": {}},
        ):
            row = {
                "ok": True,
                "execution": {"ok": True},
                "result_kind": "action",
                "plan": {"actions": [{"type": "light_on", "location": "客厅", "brightness": 100}]},
                "state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
            }
            score = semantic_score_row(row, gold)
            self.assertEqual(score["status"], "incorrect")
            self.assertFalse(score["semantic_success"])

    def test_deadline_correct_requires_semantic_success(self):
        row = {
            "ok": True,
            "execution": {"ok": True},
            "engineering_ok": True,
            "semantic_success": False,
            "semantic_status": "incorrect",
            "deadline_ms": 5000,
            "timing": {"total_ms": 100.0, "scope": "entry_to_state_before_log"},
        }
        from ai6g_runtime.evaluator import _deadline_correct
        self.assertFalse(_deadline_correct(row))

    def test_deadline_correct_prefers_external_elapsed(self):
        row = {
            "semantic_success": True,
            "error_code": None,
            "deadline_ms": 1000,
            "external_elapsed_ms": 1100,
            "timing": {"total_ms": 100.0, "scope": "entry_to_state_before_log"},
        }
        from ai6g_runtime.evaluator import _deadline_correct
        self.assertFalse(_deadline_correct(row))

    def test_gold_report_includes_deadline_correct_and_latency_scope(self):
        tasks = [{
            "id": "deadline-a",
            "text": "打开客厅的灯",
            "gold": {
                "expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}],
                "allow_clarification": False,
                "must_reject": False,
                "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}},
                "deadline_ms": 5000,
            },
        }]
        with tempfile.TemporaryDirectory() as td:
            report = evaluate_gold(tasks, self._factory(td), strategies=("rules",), repeats=1)
        summary = report["strategies"]["rules"]
        self.assertEqual(summary["deadline_correct"], 1)
        self.assertIn("p50_total_ms", summary)
        self.assertIn("p95_total_ms", summary)
        self.assertEqual(summary["latency_scope"], "runtime:entry_to_state_before_log")

    def test_gold_is_not_sent_and_each_task_gets_fresh_state(self):
        tasks = [
            {"id": "a", "text": "打开客厅的灯", "gold": {"expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}], "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}}, "allow_clarification": False, "must_reject": False}},
            {"id": "b", "text": "关闭客厅的灯", "gold": {"expected_action": [{"type": "light_off", "location": "客厅"}], "expected_state": {"lights": {"客厅": {"is_on": False, "brightness": 0}}}, "allow_clarification": False, "must_reject": False}},
        ]
        seen = []
        class SpyBackend(RuleBackend):
            def infer(self, task, timeout_s):
                seen.append(set(vars(task)))
                return super().infer(task, timeout_s)
        def factory(index=0):
            return TaskRuntime(router=TaskRouter(ObservationCache()), backends={"local-rules": SpyBackend()}, store=DeviceStore())
        report = evaluate_gold(tasks, factory, strategies=("rules",), repeats=1, task_source_sha256="abc")
        self.assertEqual(report["task_count"], 2)
        self.assertEqual(report["strategies"]["rules"]["semantic_successes"], 2)
        self.assertEqual(report["task_set_sha256"], task_set_sha256(tasks))
        self.assertEqual(report["source_sha256"], "abc")
        self.assertTrue(all("gold" not in fields for fields in seen))

    def test_hash_changes_when_gold_changes(self):
        tasks = [{"id": "a", "text": "x", "gold": {"expected_action": []}}]
        first = task_set_sha256(tasks)
        changed = copy.deepcopy(tasks)
        changed[0]["gold"]["must_reject"] = True
        self.assertNotEqual(first, task_set_sha256(changed))


if __name__ == "__main__":
    unittest.main()
