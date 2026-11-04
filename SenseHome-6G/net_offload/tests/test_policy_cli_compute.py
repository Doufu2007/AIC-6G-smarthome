import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import LinkSnapshot, SubprocessPolicyClient  # noqa: E402


class ComputeSnapshot:
    def __init__(self, model_ready, queue_ms, free_mem_mb, inflight):
        self.model_ready = model_ready
        self.queue_ms = queue_ms
        self.free_mem_mb = free_mem_mb
        self.inflight = inflight


class PolicyCliComputeTests(unittest.TestCase):
    def test_partial_health_ready_and_inflight_drive_real_c_policy(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        client = SubprocessPolicyClient(cli)
        try:
            link = LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3)
            client.update_links(link)
            self.assertEqual(client.update_compute({"model_ready": False, "inflight": 0}), "ok")
            self.assertEqual(client.decide("complex", link)[:2], ("local-model", "MODEL_UNAVAILABLE"))
            self.assertEqual(client.update_compute({"model_ready": True, "inflight": 9}), "ok")
            self.assertEqual(client.decide("complex", link)[:2], ("local-model", "COMPUTE_OVERLOADED"))
            self.assertEqual(client.update_compute({"model_ready": True, "inflight": 0}), "ok")
            self.assertEqual(client.decide("complex", link)[:2], ("cloud-model", "WAN_OK"))
            self.assertEqual(client.update_compute({"inflight": 0}), "ok")
            self.assertEqual(client.decide("complex", link)[:2], ("cloud-model", "WAN_OK"))
        finally:
            client.close()

    def test_partial_cli_rejects_invalid_sentinel_and_unknown_clears_previous_health(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        client = SubprocessPolicyClient(cli)
        try:
            link = LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3)
            client.update_links(link)
            self.assertEqual(client._cmd("compute_partial -2 -1 -1 0"), "err")
            self.assertEqual(client._cmd("compute_partial 2 -1 -1 0"), "err")
            self.assertEqual(client._cmd("compute_partial 0 -1 -1 0"), "ok")
            self.assertEqual(client.decide("complex", link)[1], "MODEL_UNAVAILABLE")
            self.assertEqual(client._cmd("compute_partial -1 -1 -1 -1"), "ok")
            self.assertEqual(client.decide("complex", link)[1], "WAN_OK")
        finally:
            client.close()

    def test_compute_log_explains_actual_selection_and_unknown_sentinels(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        client = SubprocessPolicyClient(cli)
        try:
            link = LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3)
            client.update_links(link)
            client.update_compute({"model_ready": False, "inflight": 0})
            _, _, log = client.decide("complex", link)
            self.assertIn("-> local-model because MODEL_UNAVAILABLE", log)
            self.assertIn("free_mem_mb=-1", log)
            self.assertIn("unknown=-1", log)
        finally:
            client.close()

    def test_cli_compute_observation_changes_decision(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        self.assertTrue(os.path.isfile(cli), "compile policy_cli before running this integration test")
        client = SubprocessPolicyClient(cli)
        try:
            client.update_links(LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3))
            client.update_compute(ComputeSnapshot(model_ready=False, queue_ms=0, free_mem_mb=4096, inflight=0))
            backend, reason, _ = client.decide("complex", LinkSnapshot(wan_ok=True, lan_ok=True))
        finally:
            client.close()

        self.assertEqual(backend, "local-model")
        self.assertEqual(reason, "MODEL_UNAVAILABLE")

    def test_invalid_compute_is_rejected_and_reset_restores_legacy_policy(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        client = SubprocessPolicyClient(cli)
        try:
            self.assertEqual(client._cmd("compute 2 0 4096 0"), "err")
            client.update_links(LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3))
            client.update_compute(ComputeSnapshot(model_ready=False, queue_ms=0, free_mem_mb=4096, inflight=0))
            self.assertEqual(client.decide("complex", LinkSnapshot(wan_ok=True, lan_ok=True))[1], "MODEL_UNAVAILABLE")
            self.assertEqual(client._cmd("reset"), "ok")
            client.update_links(LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3))
            self.assertEqual(client.decide("complex", LinkSnapshot(wan_ok=True, lan_ok=True))[1], "WAN_OK")
        finally:
            client.close()

    def test_concurrent_cli_requests_are_serialized(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        client = SubprocessPolicyClient(cli)
        try:
            # Directly exercise the line protocol from many callers. Every
            # response must remain a complete decision line; a write/read
            # race would commonly produce a truncated or mismatched line.
            with ThreadPoolExecutor(max_workers=8) as pool:
                outputs = list(pool.map(lambda _: client._cmd("decide simple"), range(32)))
        finally:
            client.close()

        self.assertEqual(len(outputs), 32)
        self.assertTrue(all(line.startswith("decision ") for line in outputs))
        self.assertTrue(all(len(line.split(" ", 3)) >= 3 for line in outputs))


if __name__ == "__main__":
    unittest.main()
