import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.evaluation import run_dual
from ai6g_runtime.router import HealthObservation, ObservationCache


class _FakeProbeBackend:
    def __init__(self, delay_s=0.0):
        self.delay_s = delay_s
        self.calls = 0

    def infer(self, task, timeout_s):
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        return type("Result", (), {
            "ok": True,
            "model_real": True,
            "latency_ms": self.delay_s * 1000.0,
            "raw_response": {"usage": {"total_tokens": 7}},
            "error": None,
        })()


class RunDualTests(unittest.TestCase):
    def test_parse_ttl_rejects_non_finite_values(self):
        for value in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                run_dual.parse_ttl(value)
        self.assertEqual(run_dual.parse_ttl("0"), 0.0)
        self.assertEqual(run_dual.parse_ttl("2.5"), 2.5)

    def test_ensure_observations_returns_probe_indices_and_refresh_time(self):
        backend = _FakeProbeBackend(delay_s=0.01)
        cache = ObservationCache(ttl_s=5.0)
        records = []
        with tempfile.TemporaryDirectory() as td:
            metadata = run_dual._ensure_observations(
                cache=cache,
                backends={"local-model": backend},
                calibration={"local-model": {"quality_score": 0.8, "source_sha256": "a" * 64}},
                probe_records=records,
                probe_log=Path(td) / "probes.jsonl",
                ttl_s=5.0,
            )
        self.assertEqual(metadata["probe_record_indices"], [0])
        self.assertGreaterEqual(metadata["probe_refresh_ms"], 5.0)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(len(records), 1)

        metadata2 = run_dual._ensure_observations(
            cache=cache,
            backends={"local-model": backend},
            calibration={"local-model": {"quality_score": 0.8, "source_sha256": "a" * 64}},
            probe_records=records,
            probe_log=Path(td) / "probes.jsonl",
            ttl_s=5.0,
        )
        self.assertEqual(metadata2["probe_record_indices"], [])
        self.assertEqual(metadata2["probe_refresh_ms"], 0.0)
        self.assertEqual(backend.calls, 1)

    def test_source_manifest_contains_core_and_calibration_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            calibration = Path(td) / "calibration.json"
            calibration.write_text('{"x": 1}\n', encoding="utf-8")
            expected_sha = run_dual._sha256(calibration)
            manifest = run_dual.source_manifest(calibration_path=calibration)
        self.assertIn("evaluation/run_dual.py", manifest["files"])
        self.assertIn("runtime.py", manifest["files"])
        self.assertEqual(manifest["calibration_file_sha256"], expected_sha)
        self.assertEqual(len(manifest["sha256"]), 64)

    def test_evaluate_includes_sync_probe_in_elapsed_and_deadline(self):
        tasks = [{
            "id": "probe-cost", "text": "打开客厅的灯",
            "gold": {"deadline_ms": 10.0},
        }]

        def refresh(**kwargs):
            time.sleep(0.02)
            index = len(kwargs["probe_records"])
            kwargs["probe_records"].append({
                "external_elapsed_ms": 20.0, "latency_ms": 20.0, "usage": {"total_tokens": 7},
            })
            return {"probe_refresh_ms": 20.0, "probe_record_indices": [index]}

        def handle(runtime, text, request_id):
            return {"ok": True, "external_elapsed_ms": 1.0, "request_only_elapsed_ms": 1.0,
                    "timing": {"total_ms": 1.0}, "model_real": False}

        with tempfile.TemporaryDirectory() as td:
            task_path = Path(td) / "tasks.jsonl"
            task_path.write_text("{}\n", encoding="utf-8")
            calibration_path = Path(td) / "calibration.json"
            calibration_path.write_text("{}\n", encoding="utf-8")
            runtime = type("Runtime", (), {"state": lambda self: {}})()
            with patch.object(run_dual, "load_tasks", return_value=tasks), \
                 patch.object(run_dual, "load_calibration", return_value={"local-model": {}, "cloud-model": {}}), \
                 patch.object(run_dual, "_model_specs", return_value={}), \
                 patch.object(run_dual, "_backends", return_value={}), \
                 patch.object(run_dual, "_deployment_disclosure", return_value={}), \
                 patch.object(run_dual, "_new_runtime", return_value=runtime), \
                 patch.object(run_dual, "_ensure_observations", side_effect=refresh), \
                 patch.object(run_dual, "_handle", side_effect=handle), \
                 patch.object(run_dual, "semantic_score_row", return_value={"engineering_ok": True, "semantic_success": True, "status": "semantic_success"}), \
                 patch.object(run_dual, "STRATEGIES", ("adaptive",)), \
                 patch.object(run_dual, "REPEATS", 1):
                report = run_dual.evaluate(task_path, calibration_path, Path(td) / "result.json")
        row = report["rows"][0]
        self.assertGreaterEqual(row["external_elapsed_ms"], 15.0)
        self.assertEqual(row["request_only_elapsed_ms"], 1.0)
        self.assertEqual(row["probe_refresh_ms"], 20.0)
        self.assertEqual(row["probe_record_indices"], [0])
        self.assertEqual(report["strategies"]["adaptive"]["deadline_correct"], 0)


if __name__ == "__main__":
    unittest.main()
