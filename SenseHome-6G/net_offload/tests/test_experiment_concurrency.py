import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from experiment_concurrency import QueuedStub, run_cell
from experiment_inputs import load_json, load_profiles


class ConcurrencyTests(unittest.TestCase):
    def test_shared_queue_bounded_and_drained(self):
        backend = QueuedStub("test", slots=2, service_ms=2, max_waiters=32)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: backend.infer("test"), range(32)))
        self.assertTrue(all(result.ok for result in results))
        self.assertLessEqual(backend.peak, 2)
        self.assertEqual(backend.snapshot()["inflight"], 0)
        self.assertIsNone(backend.snapshot()["free_mem_mb"])
        self.assertTrue(any(result.response["measured_queue_ms"] > 0 for result in results))
        self.assertEqual(backend.admission_order, sorted(backend.admission_order))

    def test_live_gateway_keeps_safety_local_under_concurrency(self):
        cli = str(HERE / ("policy_cli.exe" if os.name == "nt" else "policy_cli"))
        rows, info = run_cell(cli, load_json(HERE / "experiments/policy_config.json"),
                              load_profiles()["profiles"], "joint", 8, 0, 32, "healthy")
        self.assertEqual(len(rows), 32)
        self.assertEqual(info["final_active"], 0)
        self.assertEqual(info["final_waiting"], 0)
        self.assertTrue(info["fifo_verified"])
        for row in rows:
            self.assertGreaterEqual(row["total_user_ms"], row["event"]["latency"]["total_user_ms"] - .1)
            self.assertEqual(row["observation"]["compute_source"], "live_shared_bounded_stub_queues")
            if row["task_class"] in {"safety", "simple"}:
                self.assertEqual(row["backend"], "local-rules")
                self.assertTrue(row["contract_complete"])


if __name__ == "__main__":
    unittest.main()
