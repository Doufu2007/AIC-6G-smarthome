import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import SubprocessPolicyClient  # noqa: E402


class ResourcePolicyCliTests(unittest.TestCase):
    def setUp(self):
        cli = os.path.join(HERE, "policy_cli.exe" if os.name == "nt" else "policy_cli")
        self.client = SubprocessPolicyClient(cli)

    def tearDown(self):
        self.client.close()

    def configure(self):
        return self.client._cmd("resource_cfg 300 30 5 300 8 256 100 35 -1 -1 15 3 0")

    def configure_ready_endpoints(self):
        self.assertEqual(self.configure(), "ok")
        self.assertEqual(self.client._cmd("endpoint local 1 0 2048 0"), "ok")
        self.assertEqual(self.client._cmd("endpoint cloud 1 0 16384 0"), "ok")
        self.assertEqual(self.client._cmd("observe lan 1 3 -1 -1"), "ok")
        self.assertEqual(self.client._cmd("resource_tick"), "ok")

    def test_dual_endpoint_protocol_accepts_unknown_transfer_costs(self):
        self.assertEqual(self.configure(), "ok")
        self.assertEqual(self.client._cmd("endpoint local 1 0 2048 0"), "ok")
        self.assertEqual(self.client._cmd("endpoint cloud 1 0 16384 0"), "ok")
        self.assertEqual(self.client._cmd("observe wan 1 25 3 0"), "ok")
        self.assertEqual(self.client._cmd("observe lan 1 3 1 0"), "ok")
        self.assertEqual(self.client._cmd("resource_tick"), "ok")

        backend, reason, log = self.client.decide("complex", None)

        self.assertEqual((backend, reason), ("cloud-model", "COST_CLOUD"))
        self.assertIn("transfer", log.lower())

    def test_unknown_model_readiness_is_not_treated_as_available(self):
        self.assertEqual(self.configure(), "ok")
        self.assertEqual(self.client._cmd("endpoint local -1 -1 -1 -1"), "ok")
        self.assertEqual(self.client._cmd("endpoint cloud -1 -1 -1 -1"), "ok")
        self.assertEqual(self.client._cmd("observe wan 1 25 3 0"), "ok")
        self.assertEqual(self.client._cmd("observe lan 1 3 1 0"), "ok")
        self.assertEqual(self.client._cmd("resource_tick"), "ok")

        self.assertEqual(self.client.decide("complex", None)[:2],
                         ("local-rules", "NO_MODEL_CAPACITY"))

    def test_resource_clear_invalidates_dual_endpoint_cache_and_keeps_link_state(self):
        self.configure_ready_endpoints()

        self.assertEqual(self.client._cmd("resource_clear 1"), "ok")
        _, _, log = self.client.decide("complex", None)

        self.assertNotIn("[resources]", log)

    def test_simple_and_safety_stay_local_under_dual_policy(self):
        self.assertEqual(self.configure(), "ok")
        self.assertEqual(self.client._cmd("endpoint local -1 -1 -1 -1"), "ok")
        self.assertEqual(self.client._cmd("endpoint cloud 1 0 16384 0"), "ok")
        self.assertEqual(self.client.decide("simple", None)[:2],
                         ("local-rules", "TASK_IS_SIMPLE"))
        self.assertEqual(self.client.decide("safety", None)[:2],
                         ("local-rules", "TASK_IS_SAFETY"))

    def test_observed_jitter_and_loss_thresholds_gate_cloud(self):
        cases = (
            ("observe wan 1 25 31 0", "WAN_JITTER_HIGH"),
            ("observe wan 1 25 0 6", "WAN_LOSS_HIGH"),
        )
        for command, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                self.client._cmd("reset")
                self.configure_ready_endpoints()
                self.assertEqual(self.client._cmd(command), "ok")

                self.assertEqual(self.client.decide("complex", None)[:2],
                                 ("local-model", expected_reason))

    def test_unreachable_wan_without_rtt_reports_unreachable(self):
        self.configure_ready_endpoints()
        self.assertEqual(self.client._cmd("observe wan 0 -1 -1 -1"), "ok")

        self.assertEqual(self.client.decide("complex", None)[:2],
                         ("local-model", "WAN_UNREACHABLE"))


if __name__ == "__main__":
    unittest.main()
