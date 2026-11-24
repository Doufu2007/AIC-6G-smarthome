import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.evaluator import _summarize, evaluate, report_to_markdown
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import RuleBackend
from ai6g_runtime.router import TaskRouter, ObservationCache
from ai6g_runtime.runtime import TaskRuntime


class EvaluatorTests(unittest.TestCase):
    def _factory(self, td):
        def factory(run_index=0):
            return TaskRuntime(
                router=TaskRouter(ObservationCache()),
                backends={"local-rules": RuleBackend()},
                store=DeviceStore(),
                log_path=Path(td) / f"run-{run_index}.jsonl",
            )
        return factory

    def test_evaluate_repeats_and_aggregates_p95_and_model_real(self):
        tasks = [{"id": "a", "text": "打开客厅的灯"}, {"id": "b", "text": "有点冷"}]
        with tempfile.TemporaryDirectory() as td:
            report = evaluate(tasks, self._factory(td), strategies=("rules",), repeats=2)
        summary = report["strategies"]["rules"]
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["requests"], 4)
        self.assertEqual(summary["successes"], 2)
        self.assertIn("p50_total_ms", summary)
        self.assertIn("p95_total_ms", summary)
        self.assertEqual(summary["model_real_rows"], 0)
        self.assertEqual(summary["errors"]["model_failed"], 2)
        self.assertEqual(report["evidence"]["real_model_rows"], 0)

    def test_markdown_discloses_rules_only_and_contains_strategy_table(self):
        report = {"strategies": {"rules": {"runs": 1, "requests": 1, "successes": 1,
            "success_rate": 1.0, "p50_total_ms": 1.0, "p95_total_ms": 1.0,
            "model_real_rows": 0, "errors": {}}},
            "evidence": {"real_model_rows": 0, "model_real": False}}
        text = report_to_markdown(report)
        self.assertIn("rules", text)
        self.assertIn("model_real=false", text)
        self.assertIn("P95", text)

    def test_rejects_zero_repeats(self):
        with self.assertRaises(ValueError):
            evaluate([], self._factory("."), repeats=0)

    def test_summary_includes_deadline_correct_and_latency_scope(self):
        tasks = [{"id": "a", "text": "打开客厅的灯", "deadline_ms": 5000}]
        with tempfile.TemporaryDirectory() as td:
            report = evaluate(tasks, self._factory(td), strategies=("rules",), repeats=1)
        summary = report["strategies"]["rules"]
        self.assertEqual(summary["deadline_correct"], 0)
        self.assertEqual(summary["deadline_evaluable"], 0)
        self.assertIn("p50_total_ms", summary)
        self.assertIn("p95_total_ms", summary)
        self.assertEqual(summary["latency_scope"], "runtime:entry_to_state_before_log")

    def test_summary_uses_external_elapsed_when_every_row_has_it(self):
        rows = [
            {"ok": True, "execution": {"ok": True}, "timing": {"total_ms": 9.0}, "external_elapsed_ms": 21.0},
            {"ok": True, "execution": {"ok": True}, "timing": {"total_ms": 11.0}, "external_elapsed_ms": 31.0},
        ]
        summary = _summarize(rows, runs=1)
        self.assertEqual(summary["latency_scope"], "external:elapsed_ms")
        self.assertEqual(summary["p50_total_ms"], 26.0)


if __name__ == "__main__":
    unittest.main()
