import os
import sys
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import offload_bench  # noqa: E402
from network_trace import NetworkTrace  # noqa: E402


class FakeDriver:
    def probe(self, link, ok, rtt_ms):
        return "ok"

    def decide(self, task):
        if task in {"simple", "safety"}:
            return "local-rules", "TASK_IS_" + task.upper(), "test"
        return "cloud-model", "WAN_OK", "test"


class BenchLatencyTests(unittest.TestCase):
    def make_bench(self):
        bench = offload_bench.Bench.__new__(offload_bench.Bench)
        bench.args = types.SimpleNamespace(wan_port=1, lan_port=2)
        bench.timeout = 1.0
        return bench

    def test_infer_once_reports_probe_policy_backend_and_total_user_latency(self):
        bench = self.make_bench()

        def fake_probe(drv, do_lan=True):
            time.sleep(0.001)
            return {"wan": {"ok": True, "rtt_ms": 4.0}, "lan": {"ok": True, "rtt_ms": 1.0}}

        bench.probe_links = fake_probe
        with mock.patch.object(offload_bench, "http_post", return_value=(True, 0.002, {"stub_compute_ms": 1.0, "handler_ms": 0.001})):
            row = bench.infer_once(FakeDriver(), "复杂任务", "adaptive")

        self.assertIn("probe_ms", row)
        self.assertIn("policy_ms", row)
        self.assertIn("backend_ms", row)
        self.assertIn("total_user_ms", row)
        self.assertGreater(row["probe_ms"], 0)
        self.assertGreaterEqual(row["policy_ms"], 0)
        self.assertGreaterEqual(row["backend_ms"], 0)
        self.assertAlmostEqual(
            row["total_user_ms"],
            row["probe_ms"] + row["policy_ms"] + row["backend_ms"],
            places=6,
        )

    def test_infer_once_carries_shared_trace_identity(self):
        bench = self.make_bench()
        trace = NetworkTrace.generate(seed=9, steps=[30], probes_per_step=1)
        bench.probe_links = lambda drv, do_lan=True: {
            "wan": {"ok": True, "rtt_ms": 4.0},
            "lan": {"ok": True, "rtt_ms": 1.0},
        }
        with mock.patch.object(offload_bench, "http_post", return_value=(True, 0.002, {"stub_compute_ms": 1.0, "handler_ms": 0.001})):
            row = bench.infer_once(
                FakeDriver(), "复杂任务", "adaptive",
                trace_id=trace.trace_id, trace_index=trace.samples[0].index,
            )

        self.assertEqual(row["trace_id"], trace.trace_id)
        self.assertEqual(row["trace_index"], 0)

    def test_summary_exposes_latency_components(self):
        args = types.SimpleNamespace(cloud_compute_ms=300.0)
        phase = {
            "phase": "P1-normal",
            "arm": "adaptive",
            "conditions": {},
            "rows": [
                {
                    "task": "complex", "backend": "cloud-model", "reason": "WAN_OK",
                    "ok": True, "resolved": True, "e2e_ms": 10.0,
                    "probe_ms": 2.0, "policy_ms": 1.0, "backend_ms": 7.0,
                    "net_ms": 5.0, "http_ms": 7.0,
                }
            ],
            "wan_refused": 0,
            "wan_accepted": 1,
        }
        summary = offload_bench.summarize(phase, args)

        self.assertEqual(summary["overall"]["probe_median_ms"], 2.0)
        self.assertEqual(summary["overall"]["policy_median_ms"], 1.0)
        self.assertEqual(summary["overall"]["backend_median_ms"], 7.0)
        self.assertEqual(summary["overall"]["total_user_median_ms"], 10.0)

    def test_local_tasks_skip_probe_and_report_zero_probe_latency(self):
        for task in ("simple", "safety"):
            with self.subTest(task=task):
                bench = self.make_bench()
                bench.probe_links = mock.Mock(side_effect=RuntimeError("probe must not run"))
                driver = FakeDriver()
                driver.decide = mock.Mock(wraps=driver.decide)
                with mock.patch.object(offload_bench, "http_post") as http_post:
                    row = bench.infer_once(driver, task, "adaptive")

                bench.probe_links.assert_not_called()
                driver.decide.assert_called_once_with(task)
                http_post.assert_not_called()
                self.assertEqual(row["backend"], "local-rules")
                self.assertEqual(row["probe_ms"], 0.0)
                self.assertIsNone(row["wan_probe_ok"])
                self.assertIsNone(row["wan_probe_ms"])
                self.assertAlmostEqual(row["total_user_ms"], row["policy_ms"] + row["backend_ms"])

    def test_probe_overhead_excludes_forty_local_tasks_that_did_not_probe(self):
        phase = {
            "conditions": {"wan": {"blackout": False, "loss_pct": 10.0}},
            "rows": ([{"probe_ms": 0.0, "wan_probe_ok": None}] * 40
                     + [{"probe_ms": float(n), "wan_probe_ok": True} for n in range(400, 420)]),
        }
        phase["rows"][-1]["wan_probe_ok"] = False

        overhead = offload_bench.probe_overhead(phase)

        self.assertEqual(overhead["n_probes"], 20)
        self.assertEqual(overhead["median_probe_ms"], 409.5)
        self.assertEqual(overhead["probe_failure_pct"], 5.0)


if __name__ == "__main__":
    unittest.main()
