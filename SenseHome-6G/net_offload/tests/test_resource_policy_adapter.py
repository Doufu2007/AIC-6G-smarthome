import importlib
import importlib.util
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def resource_modules(test_case):
    for name in ("experiment_inputs", "resource_policy"):
        test_case.assertIsNotNone(importlib.util.find_spec(name), name + " module is missing")
    return importlib.import_module("experiment_inputs"), importlib.import_module("resource_policy")


def policy_config():
    return {
        "schema_version": "sensehome.policy.v1",
        "queue_max_ms": 300,
        "inflight_max": 8,
        "free_mem_min_mb": 256,
        "local_infer_ms": 100,
        "cloud_infer_ms": 35,
        "switch_margin_ms": 15,
        "recovery_samples": 3,
        "timeout_ms": 800,
        "observation_max_age_ms": 2000,
        "observation_lookup_ms": 0.2,
        "policy_model_ms": 0.2,
        "execution_model_ms": 0.1,
        "rule_model_ms": 1,
    }


def service_profile():
    return {
        "task_class": "complex",
        "deadline_ms": 250,
        "reliability_target": 0.99,
        "request_bytes": 4096,
        "response_bytes": 2048,
        "wan_rtt_max_ms": 180,
        "wan_jitter_max_ms": 30,
        "wan_loss_max_pct": 5,
    }


def resource_sample(age_ms=10, observation_id=1, source_out_of_order=False):
    return {
        "observation_id": observation_id,
        "observed_age_ms": age_ms,
        "source_out_of_order": source_out_of_order,
        "network": {
            "wan": {"reachable": True, "rtt_ms": 25, "jitter_ms": None,
                    "loss_pct": None, "bandwidth_kbps": None},
            "lan": {"reachable": True, "rtt_ms": 0, "jitter_ms": None,
                    "loss_pct": None, "bandwidth_kbps": None},
        },
        "compute": {
            "local": {"model_ready": True, "queue_ms": None,
                      "free_mem_mb": None, "inflight": None},
            "cloud": {"model_ready": True, "queue_ms": None,
                      "free_mem_mb": None, "inflight": None},
        },
        "endpoint_age_ms": {"local": age_ms, "cloud": age_ms},
    }


class ResourcePolicyAdapterTests(unittest.TestCase):
    def make_client(self):
        _, resource_module = resource_modules(self)
        client = resource_module.ResourcePolicyClient.__new__(resource_module.ResourcePolicyClient)
        client.config = policy_config()
        client.arm = "joint"
        client._lock = threading.RLock()
        client._last_observation = None
        client._observation_highwater = -1
        client._compute_updated = False
        client.last_decision_path = "legacy_single_endpoint"
        client.transfer_costs_known = False
        client.commands = []

        def command(line):
            client.commands.append(line)
            return "ok"

        client._cmd = command
        return client

    def test_missing_bandwidth_is_unknown_not_zero(self):
        inputs, _ = resource_modules(self)
        profile = service_profile()

        self.assertIsNone(inputs.transfer_ms(profile, {"bandwidth_kbps": None}))
        self.assertEqual(inputs.transfer_ms(profile, {"bandwidth_kbps": 1000}), 49.152)

    def test_description_uses_audit_implementation_identifier(self):
        client = self.make_client()

        description = client.describe()

        self.assertEqual(description["implementation"], "dual_endpoint_C_policy")
        self.assertEqual(description["active_decision_path"], "legacy_single_endpoint")

    def test_local_task_decision_description_is_not_stale(self):
        _, resource_module = resource_modules(self)
        client = self.make_client()

        client.decide("safety", resource_module.LinkSnapshot())

        self.assertEqual(client.describe()["active_decision_path"], "local_task_forced")

    def test_missing_complex_observation_clears_previous_dual_endpoint_state(self):
        _, resource_module = resource_modules(self)
        client = self.make_client()
        client.observe(resource_sample(), service_profile())
        client.commands.clear()

        def command(line):
            client.commands.append(line)
            return "decision local-model WAN_OK legacy" if line == "decide complex" else "ok"

        client._cmd = command
        client.clear_observation_state()
        client.decide("complex", resource_module.LinkSnapshot(wan_ok=True, wan_rtt_ms=25))

        self.assertEqual(client.commands[-2:], ["resource_clear 1", "decide complex"])
        self.assertEqual(client.describe()["active_decision_path"], "legacy_single_endpoint")

    def test_observe_keeps_unknown_link_and_compute_fields_as_unknown(self):
        client = self.make_client()

        client.observe(resource_sample(), service_profile())

        commands = "\n".join(client.commands)
        self.assertIn("resource_cfg 180 30 5 300 8 256 100 35 -1 -1 15 3 0", commands)
        self.assertIn("observe wan 1 25 -1 -1", commands)
        self.assertIn("observe lan 1 0 -1 -1", commands)
        self.assertNotIn("observe wan 1 25 0 0", commands)

    def test_out_of_order_or_stale_observation_fails_closed(self):
        client = self.make_client()

        client.observe(resource_sample(age_ms=None, source_out_of_order=True), service_profile())

        commands = "\n".join(client.commands)
        self.assertIn("endpoint local 0 -1 -1 -1", commands)
        self.assertIn("endpoint cloud 0 -1 -1 -1", commands)
        self.assertIn("observe wan 0 25 -1 -1", commands)
        self.assertIn("observe lan 0 0 -1 -1", commands)


if __name__ == "__main__":
    unittest.main()
