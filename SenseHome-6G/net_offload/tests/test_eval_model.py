import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from eval_model import actions_equal, evaluate_cases, load_cases  # noqa: E402


class ModelEvaluationTests(unittest.TestCase):
    def test_action_comparison_uses_effects_not_model_success_claim(self):
        expected = [{"name": "light_control", "params": {"location": "客厅", "action": "on"}}]
        equivalent = [{"name": "light_control", "params": {"location": "客厅", "action": "set", "brightness": 100}}]
        self.assertTrue(actions_equal(expected, equivalent))
        self.assertFalse(actions_equal(expected, [{"name": "unlock_door", "params": {"location": "客厅"}}]))

    def test_draft_labels_require_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps({"id": "x", "command": "有点暗", "expected_actions": [], "human_reviewed": False}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)
            self.assertEqual(len(load_cases(path, allow_draft_labels=True)), 1)

    def test_evaluation_does_not_trust_semantic_complete_field(self):
        case = {"id": "x", "command": "客厅有点暗", "expected_actions": [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}], "human_reviewed": True}
        with mock.patch("eval_model.request_backend", return_value=(200, {"actions": [], "semantic_complete": True}, 1.0)):
            report = evaluate_cases("http://127.0.0.1:1", [case])
        self.assertEqual(report["summary"]["n_action_correct"], 0)
        self.assertFalse(report["rows"][0]["action_correct"])
        self.assertEqual(report["label_status"], "human_reviewed")


if __name__ == "__main__":
    unittest.main()
