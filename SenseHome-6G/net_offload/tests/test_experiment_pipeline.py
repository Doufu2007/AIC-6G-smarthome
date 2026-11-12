import copy
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from experiment_inputs import (ARMS, SCENARIOS, canonical_hash, load_json, load_profiles,
                               make_trace, transfer_ms, validate_config, validate_trace, write_json)
from audit_experiments import audit
from experiment_replay import modeled_result, replay, seal_artifacts, source_manifest, stable_projection
from experiment_stats import aggregate, quantile, summary, wilson
from experiment_http_gateway import default_gateway, http_call, run_http
from experiment_http_gateway import summarize_http
from resource_policy import ResourcePolicyClient
from run_experiment_suite import compiler_invocation

CLI = str(HERE / ("policy_cli.exe" if os.name == "nt" else "policy_cli"))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.profiles = load_profiles()["profiles"]
        self.trace = make_trace(1234, 5)

    def test_generated_trace_valid_and_reproducible(self):
        self.assertEqual(self.trace, make_trace(1234, 5))
        self.assertNotEqual(self.trace["trace_id"], make_trace(1235, 5)["trace_id"])
        validate_trace(self.trace, self.profiles)
        self.assertEqual(len(self.trace["samples"]), len(SCENARIOS) * 5)

    def test_tamper_rejected(self):
        self.trace["samples"][0]["network"]["wan"]["rtt_ms"] = 99
        with self.assertRaisesRegex(ValueError, "TRACE_ID"):
            validate_trace(self.trace, self.profiles)

    def test_invalid_trace_values_rejected_even_with_rehashed_payload(self):
        for field, value in (("bandwidth_kbps", 0), ("rtt_ms", math.nan),
                             ("loss_pct", -1), ("reachable", 1)):
            trace = copy.deepcopy(self.trace)
            trace["samples"][0]["network"]["wan"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                trace["trace_id"] = canonical_hash({k: v for k, v in trace.items() if k != "trace_id"})
                validate_trace(trace, self.profiles)

    def test_bandwidth_units(self):
        profile = {"request_bytes": 1000, "response_bytes": 1000}
        self.assertEqual(transfer_ms(profile, {"bandwidth_kbps": 1000}), 16)

    def test_invalid_policy_config_rejected(self):
        config = load_json(HERE / "experiments/policy_config.json")
        config["queue_max_ms"] = -1
        with self.assertRaisesRegex(ValueError, "INVALID_POLICY"):
            validate_config(config)

    def test_independent_roles_have_frozen_distinct_outcomes(self):
        sample = self.trace["samples"][0]
        self.assertNotEqual(sample["outcomes"]["wan-probe"], sample["outcomes"]["wan-infer"])
        self.assertEqual(set(sample["outcomes"]), {
            "wan-probe", "wan-infer", "lan-probe", "lan-infer"})

    def test_zig_compiler_invocation_uses_c_frontend(self):
        self.assertEqual(compiler_invocation("C:/tools/zig.exe"), ["C:/tools/zig.exe", "cc"])
        self.assertEqual(compiler_invocation("C:/tools/gcc.exe"), ["C:/tools/gcc.exe"])

    def test_source_manifest_includes_all_policy_and_gateway_tests(self):
        manifest = source_manifest(CLI)

        self.assertIn("tests/test_policy_cli_resources.py", manifest)
        self.assertIn("tests/test_resource_policy_adapter.py", manifest)
        self.assertIn("tests/test_offload_gateway.py", manifest)


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_json(HERE / "experiments/policy_config.json")
        self.profiles = load_profiles()["profiles"]
        self.profile = self.profiles["interactive_inference"]
        self.sample = make_trace(17, 5)["samples"][0]
        self.client = ResourcePolicyClient(CLI, self.config)

    def tearDown(self):
        self.client.close()

    def select(self):
        return self.client.select(self.sample, self.profile)[:2]

    def test_healthy_cost_uses_cloud(self):
        self.assertEqual(self.select(), ("cloud-model", "COST_CLOUD"))

    def test_cloud_down_uses_local(self):
        self.sample["compute"]["cloud"]["model_ready"] = False
        self.assertEqual(self.select(), ("local-model", "MODEL_UNAVAILABLE"))

    def test_local_down_good_network_uses_cloud(self):
        self.sample["compute"]["local"]["model_ready"] = False
        self.assertEqual(self.select(), ("cloud-model", "LOCAL_MODEL_UNAVAILABLE"))

    def test_both_down_rules(self):
        for state in self.sample["compute"].values():
            state["model_ready"] = False
        self.assertEqual(self.select(), ("local-rules", "NO_MODEL_CAPACITY"))

    def test_cloud_queue_uses_local(self):
        self.sample["compute"]["cloud"]["queue_ms"] = 301
        self.assertEqual(self.select(), ("local-model", "COMPUTE_OVERLOADED"))

    def test_local_overload_can_trigger_cloud(self):
        self.sample["compute"]["local"]["inflight"] = 9
        self.assertEqual(self.select(), ("cloud-model", "LOCAL_COMPUTE_OVERLOADED"))

    def test_wan_degradation_and_local_idle_uses_local(self):
        self.sample["network"]["wan"]["rtt_ms"] = 400
        self.assertEqual(self.select(), ("local-model", "WAN_RTT_HIGH"))

    def test_lan_blackout_rules(self):
        self.sample["network"]["lan"]["reachable"] = False
        self.assertEqual(self.select(), ("local-rules", "LAN_DOWN"))

    def test_bandwidth_cost_changes_route_without_rtt_degradation(self):
        self.sample["network"]["wan"]["bandwidth_kbps"] = 64
        self.assertEqual(self.select(), ("local-model", "COST_LOCAL"))

    def test_stale_observations_fail_closed(self):
        self.sample["observed_age_ms"] = 2001
        self.assertEqual(self.select(), ("local-rules", "LAN_DOWN"))

    def test_recovery_requires_three_complex_observations(self):
        self.sample["network"]["wan"]["reachable"] = False
        self.assertEqual(self.select()[0], "local-model")
        self.sample["network"]["wan"]["reachable"] = True
        self.sample["index"] += 1
        self.assertEqual(self.select(), ("local-model", "RECOVERY_HOLD"))
        # A busy gateway reading the same cached sample must not recover early.
        for _ in range(8):
            self.assertEqual(self.select()[1], "RECOVERY_HOLD")
        # Local-only tasks don't consume a recovery observation.
        self.client.select(self.sample, self.profiles["safety_control"])
        self.sample["index"] += 1
        self.assertEqual(self.select()[1], "RECOVERY_HOLD")
        self.sample["index"] += 1
        self.assertEqual(self.select()[0], "cloud-model")


    def test_unknown_memory_not_low_memory(self):
        for state in self.sample["compute"].values():
            state["free_mem_mb"] = None
        self.assertEqual(self.select()[0], "cloud-model")

    def test_out_of_order_cache_cannot_manufacture_recovery(self):
        self.sample["observation_id"] = 1
        self.sample["network"]["wan"]["reachable"] = False
        self.select()
        self.sample["network"]["wan"]["reachable"] = True
        for index in (2, 3):
            self.sample["observation_id"] = index
            self.assertEqual(self.select()[1], "RECOVERY_HOLD")
        self.sample["observation_id"] = 2
        self.assertEqual(self.select(), ("local-rules", "LAN_DOWN"))
        self.sample["observation_id"] = 4
        self.assertEqual(self.select()[1], "RECOVERY_HOLD")

    def test_all_arms_keep_safety_and_simple_local(self):
        for arm in ARMS:
            client = ResourcePolicyClient(CLI, self.config, arm)
            try:
                for profile in (self.profiles["safety_control"], self.profiles["sensor_batch"]):
                    self.assertEqual(client.select(self.sample, profile)[0], "local-rules")
            finally:
                client.close()

    def test_cli_rejects_invalid_resource_observations(self):
        for command in ("endpoint bogus 1 0 256 0", "endpoint local 2 0 256 0",
                        "observe wan 1 -2 0 0", "observe lan 1 3 0 101",
                        "resource_cfg 180 30 5 -1 8 256 100 35 0 0 15 3 0"):
            self.assertEqual(self.client._cmd(command), "err")

    def test_network_only_ignores_compute_readiness(self):
        self.client.close()
        self.client = ResourcePolicyClient(CLI, self.config, "network-only")
        self.sample["compute"]["cloud"]["model_ready"] = False
        self.assertEqual(self.select()[0], "cloud-model")

    def test_margin_prevents_boundary_oscillation(self):
        outputs = []
        for rtt in (80, 60, 80, 60, 80, 60):
            self.sample["network"]["wan"]["rtt_ms"] = rtt
            outputs.append(self.select()[0])
        self.assertEqual(len(set(outputs)), 1)

    def test_compute_only_ignores_transport_cost(self):
        self.client.close()
        self.client = ResourcePolicyClient(CLI, self.config, "compute-only")
        self.sample["network"]["wan"].update(reachable=False, bandwidth_kbps=1)
        self.assertEqual(self.select()[0], "cloud-model")


class HttpGatewayFixtureTests(unittest.TestCase):
    def test_default_gateway_serves_health_through_dashboard_handler(self):
        config = load_json(HERE / "experiments/policy_config.json")
        with tempfile.TemporaryDirectory() as folder:
            with default_gateway(Path(folder), CLI, config, "joint") as (port, _):
                status, response, _ = http_call(port, "GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertIsInstance(response, dict)

    def test_http_audit_reruns_local_events_without_network_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "http"
            run_http(out, CLI, per_phase=5, requests=5, repetitions=1, levels=(1,),
                     seeds=(20261116,), arms=("joint",))
            stress_trace = load_json(out / "trace_stress_20261119.json")
            http_summary = load_json(out / "summary.json")
            self.assertEqual(stress_trace, make_trace(20261119, 5))
            self.assertEqual(http_summary["date"], datetime.now(timezone.utc).date().isoformat())
            result = audit(out, CLI, rerun=True)

        self.assertTrue(result["passed"])
        self.assertGreater(result["persisted_events"], 0)

    def test_http_audit_rejects_stress_input_detached_from_seeded_trace(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "http"
            run_http(out, CLI, per_phase=5, requests=5, repetitions=1, levels=(1,),
                     seeds=(20261116,), arms=("joint",))
            raw_path = out / "raw_requests.jsonl"
            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            row = next(item for item in rows if item["workload"] == "stress")
            row["input"]["network"]["wan"]["rtt_ms"] += 1
            row["configured_input_hash"] = canonical_hash(row["input"])
            raw_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
                                encoding="utf-8")
            seal_artifacts(out)

            with self.assertRaisesRegex(ValueError, "HTTP_STRESS_TRACE_DIVERGENCE"):
                audit(out, CLI, rerun=False)

    def test_http_audit_rejects_unknown_workload_label(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "http"
            run_http(out, CLI, per_phase=5, requests=5, repetitions=1, levels=(1,),
                     seeds=(20261116,), arms=("joint",))
            raw_path = out / "raw_requests.jsonl"
            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["workload"] = "unexpected"
            raw_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n",
                                encoding="utf-8")
            summary_path = out / "summary.json"
            http_summary = load_json(summary_path)
            http_summary["cells"] = summarize_http(rows)
            write_json(summary_path, http_summary)
            seal_artifacts(out)

            with self.assertRaisesRegex(ValueError, "HTTP_UNKNOWN_WORKLOAD"):
                audit(out, CLI, rerun=False)


class ReplayStatsTests(unittest.TestCase):
    def test_quantiles_known_values_and_empty(self):
        self.assertEqual(quantile([0, 10], .95), 9.5)
        self.assertEqual(quantile([1], .99), 1)
        self.assertIsNone(quantile([], .5))
        low, high = wilson(0, 100)
        self.assertAlmostEqual(low, 0)
        self.assertLess(high, .04)

    def test_failure_cannot_satisfy_deadline(self):
        row = {"total_user_ms": 1, "deadline_ms": 100, "contract_complete": False,
               "backend": "local-rules", "reason": "NO_MODEL_CAPACITY", "error": "NO_NLU"}
        result = summary([row])
        self.assertEqual(result["deadline_met"], 0)
        self.assertEqual(result["contract_completion_rate"], 0)
        self.assertEqual(result["p99_ms"], 1)

    def test_replay_exact_determinism_and_paired_input_hashes(self):
        trace = make_trace(11, 5)
        profiles = load_profiles()["profiles"]
        config = load_json(HERE / "experiments/policy_config.json")
        first, switches = replay(trace, profiles, config, CLI)
        second, _ = replay(trace, profiles, config, CLI)
        self.assertEqual([stable_projection(row) for row in first],
                         [stable_projection(row) for row in second])
        self.assertTrue(switches)
        for index in range(len(trace["samples"])):
            rows = [row for row in first if row["trace_index"] == index]
            self.assertEqual(len(rows), len(ARMS))
            self.assertEqual(len({row["input_hash"] for row in rows}), 1)
            for row in rows:
                self.assertAlmostEqual(row["total_user_ms"], sum(row["latency_components_ms"].values()))
        self.assertEqual(len(aggregate(first)["cells"]), len(SCENARIOS) * 3 * len(ARMS))

    def test_timeout_bounded_but_includes_fallback(self):
        sample = make_trace(19, 5)["samples"][0]
        profile = load_profiles()["profiles"]["interactive_inference"]
        config = load_json(HERE / "experiments/policy_config.json")
        sample["compute"]["cloud"]["queue_ms"] = 5000
        result = modeled_result(sample, profile, "cloud-model", config)
        self.assertEqual(result["error"], "MODEL_TIMEOUT")
        self.assertTrue(result["fallback_attempted"])
        self.assertFalse(result["contract_complete"])
        self.assertLessEqual(result["total_user_ms"], config["timeout_ms"] + 2)


if __name__ == "__main__":
    unittest.main()
