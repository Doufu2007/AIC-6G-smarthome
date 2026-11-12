"""Behaviour tests for the fused real-endpoint runner (fixtures are not models)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def runner(test):
    path = HERE / "fused_eval.py"
    test.assertTrue(path.is_file(), "the fused endpoint runner must exist")
    spec = importlib.util.spec_from_file_location("fused_eval_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelFixture:
    """A real HTTP server with explicitly synthetic responses, used only here."""
    def __init__(self, content, status=200):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, body, code=200):
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                self.reply({"data": [{"id": "fixture-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                self.reply({"id": "fixture-completion", "model": "fixture-model",
                            "choices": [{"message": {"content": content}}],
                            "usage": {"completion_tokens": 12}}, status)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FusedEvalTests(unittest.TestCase):
    def test_schedule_rotates_arms_and_requires_three_repetitions(self):
        m = runner(self)
        jobs = m.make_schedule([{"id": "one"}], 4)
        self.assertEqual([job["arms"] for job in jobs], [
            ["fixed-local", "fixed-cloud", "network-only", "joint"],
            ["fixed-cloud", "network-only", "joint", "fixed-local"],
            ["network-only", "joint", "fixed-local", "fixed-cloud"],
            ["joint", "fixed-local", "fixed-cloud", "network-only"],
        ])
        with self.assertRaises(ValueError):
            m.make_schedule([{"id": "one"}], 2)

    def test_direct_adapter_retains_raw_unsafe_plan_before_guard_refusal(self):
        m = runner(self)
        unsafe = '{"answer":"done","actions":[{"name":"unlock_door","params":{"location":"客厅"}}]}'
        with ModelFixture(unsafe) as fixture:
            backend = m.DirectModelBackend(fixture.url, "fixture-model", "local", evidence_kind="test-fixture")
            result = backend.infer("打开客厅的灯并打开窗帘", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "model_refusal")
        self.assertEqual(backend.attempts[0]["raw_model_actions"][0]["name"], "unlock_door")
        self.assertEqual(backend.attempts[0]["upstream_exchanges"][0]["response"]["choices"][0]["message"]["content"], unsafe)
        self.assertEqual(backend.attempts[0]["upstream_exchanges"][0]["request"]["model"], "fixture-model")

    def test_health_probe_is_read_only_and_preserves_unknown_metrics(self):
        m = runner(self)
        with ModelFixture('{"answer":"","actions":[]}') as fixture:
            backend = m.DirectModelBackend(fixture.url, "fixture-model", "local", evidence_kind="test-fixture")
            health = backend.health()
            observation = m.make_observation(2, {"local": health}, m.default_profile())
        self.assertEqual([request[0] for request in fixture.requests], ["GET"])
        self.assertTrue(observation["compute"]["local"]["model_ready"])
        self.assertIsNone(observation["compute"]["local"]["free_mem_mb"])
        self.assertEqual(observation["compute"]["local"]["queue_ms"], 0)
        self.assertEqual(health['queue_scope'], 'adapter_serial_admission')
        self.assertIsNone(health['upstream_queue_ms'])
        self.assertIsNone(observation["network"]["wan"]["rtt_ms"])
        self.assertFalse(observation["network"]["wan"]["reachable"])

    def test_summary_cannot_pool_synthetic_and_real_evidence_or_count_failed_fallback(self):
        m = runner(self)
        common = {"arm": "joint", "status": "finished", "gateway_wall_ms": 8,
                  "action_correct": True, "state_outcome_correct": True,
                  "simulated_execution_success": True, "interface_ok": True,
                  "model_http_ok": True, "model_calls": 1, "fallback": False,
                  "error": None, "protection_refusal": False}
        rows = [{**common, "evidence_class": "real-model"},
                {**common, "evidence_class": "test-fixture", "gateway_wall_ms": 900},
                {**common, "evidence_class": "fallback", "error": "HTTP_503", "fallback": True},
                {**common, "evidence_class": "real-model", "status": "skipped", "model_calls": 0}]
        summary = m.summarize(rows)
        self.assertEqual(summary["by_arm_and_evidence"]["joint"]["real-model"]["n_rows"], 1)
        self.assertEqual(summary["by_arm_and_evidence"]["joint"]["real-model"]["gateway_median_ms"], 8)
        self.assertEqual(summary["by_arm_and_evidence"]["joint"]["fallback"]["n_completed"], 0)
        self.assertEqual(summary["n_skipped"], 1)

    def test_c_gateway_preserves_action_accuracy_execution_state_and_failed_raw_rows(self):
        m = runner(self)
        cli = ROOT / "SenseHome-6G/net_offload/policy_cli.exe"
        if not cli.exists():
            self.skipTest("existing Windows C policy binary unavailable")
        plan = '{"answer":"","actions":[{"name":"light_control","params":{"location":"客厅","brightness":60}}]}'
        case = {"id": "C01", "category": "explicit", "command": "把客厅灯光设为60%",
                "expected_actions": [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}],
                "human_reviewed": True, "label_source": "unit_test_fixture_not_frozen_dataset"}
        with ModelFixture(plan) as fixture, tempfile.TemporaryDirectory() as tmp:
            backend = m.DirectModelBackend(fixture.url, "fixture-model", "local", evidence_kind="test-fixture")
            report = m.evaluate([case], {"local": backend}, cli, tmp, repeat=3)
            disk_rows = [json.loads(line) for line in (Path(tmp) / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(disk_rows), 12)
        local = [r for r in disk_rows if r["arm"] == "fixed-local"]
        self.assertTrue(all(r["action_correct"] and r["simulated_execution_success"] for r in local))
        self.assertEqual(local[0]["state_after"]["lights"]["客厅"]["brightness"], 60)
        self.assertEqual(local[0]["gateway_event"]["policy"]["implementation"], "dual_endpoint_C_policy")
        self.assertEqual(len({r["input_hash"] for r in disk_rows}), 1)
        self.assertTrue(all(r["status"] == "skipped" for r in disk_rows if r["arm"] == "fixed-cloud"))
        self.assertEqual(report["summary"]["n_skipped"], 3)
        self.assertNotIn("real-model", report["summary"]["by_arm_and_evidence"]["fixed-local"])

    def test_new_case_collection_is_never_promoted_to_human_frozen_status(self):
        m = runner(self)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.jsonl"
            path.write_text(json.dumps({"id": "D01", "command": "有点暗", "expected_actions": [],
                                       "human_reviewed": False, "label_source": "agent_draft_unreviewed"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                m.read_dataset(path)
            data = m.read_dataset(path, allow_draft=True)
        self.assertEqual(data["label_status"], "draft_labels_not_human_verified")

    def test_report_endpoint_identity_redacts_non_loopback_addresses(self):
        m = runner(self)
        self.assertEqual(m.endpoint_identity("http://127.0.0.1:18091/v1"), "http://127.0.0.1:18091/v1")
        self.assertEqual(m.endpoint_identity("https://private-host.invalid:18091/v1"), "redacted_non_loopback_endpoint")

    def test_calibration_records_measured_calls_and_does_not_reuse_frozen_commands(self):
        m = runner(self)
        with ModelFixture('{"answer":"","actions":[]}') as fixture:
            backend = m.DirectModelBackend(fixture.url, "fixture-model", "local", evidence_kind="test-fixture")
            data = m.calibrate({"local": backend}, count=3)
        self.assertEqual(len(data["rows"]), 3)
        self.assertGreater(data["inference_cost_ms"]["local"], 0)
        self.assertEqual(data["scope"], "warmup_not_evaluation")
        frozen = {c["command"] for c in m.read_dataset(ROOT / "SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl")["cases"]}
        self.assertFalse(frozen & {r["command"] for r in data["rows"]})

    def test_programmatic_challenge_freezes_before_inference_and_cannot_be_called_human_reviewed(self):
        m = runner(self)
        with tempfile.TemporaryDirectory() as tmp:
            manifest = m.freeze_challenge(tmp)
            path = Path(tmp) / "challenge.programmatic.jsonl"
            data = m.read_dataset(path, allow_draft=True)
            self.assertEqual(manifest["dataset_sha256"], data["sha256"])
            self.assertEqual(len(data["cases"]), 12)
            self.assertTrue(all(c["human_reviewed"] is False for c in data["cases"]))
            self.assertEqual(data["label_status"], "draft_labels_not_human_verified")
            with self.assertRaises(ValueError):
                m.freeze_challenge(tmp)

    def test_summary_separates_raw_normalized_empty_and_task_scope(self):
        m = runner(self)
        common = {"arm": "joint", "status": "finished", "gateway_wall_ms": 8,
                  "evidence_class": "real-model", "action_correct": True,
                  "interface_ok": True, "model_calls": 1, "error": None,
                  "task_scope": "complex_planning", "raw_model_action_match": False,
                  "normalized_model_action_match": True, "expected_empty_plan": False}
        summary = m.summarize([common, {**common, "task_scope": "local_simple_or_safety",
                                       "evidence_class": "local-rules", "expected_empty_plan": True}])
        value = summary["by_task_scope"]["complex_planning"]["joint"]["real-model"]
        self.assertEqual(value["n_raw_model_action_match"], 0)
        self.assertEqual(value["n_normalized_model_action_match"], 1)
        self.assertEqual(value["n_empty_expected_structure_match"], 0)

    def test_matching_initial_state_never_proves_an_unexecuted_command_completed(self):
        m = runner(self)
        state = {"lights": {"客厅": {"is_on": True, "brightness": 100}},
                 "curtains": {"客厅": {"is_open": True, "position": 100}},
                 "temperature": {"客厅": {"temp": 25, "humidity": 60}}}
        expected = [{"name": "curtain_control", "params": {"location": "客厅", "action": "open"}}]
        event = {"interface_ok": True, "semantic_complete": False, "actions": [],
                 "source": "local-rules", "response": {}, "error": None}
        score = m.score_outcome(event, state, state, expected, [])
        self.assertTrue(score["state_outcome_correct"])
        self.assertFalse(score["task_completed"])
        self.assertFalse(score["simulated_execution_success"])

    def test_local_direct_state_mutation_counts_execution_without_remote_executor(self):
        m = runner(self)
        before = {"lights": {"客厅": {"is_on": False, "brightness": 0}},
                  "curtains": {}, "temperature": {}}
        after = {"lights": {"客厅": {"is_on": True, "brightness": 100}},
                 "curtains": {}, "temperature": {}}
        actions = [{"name": "light_control", "params": {"location": "客厅", "action": "on"}}]
        event = {"interface_ok": True, "semantic_complete": True, "actions": actions,
                 "source": "local-rules", "response": {}, "error": None}
        score = m.score_outcome(event, before, after, actions, [])
        self.assertTrue(score["simulated_execution_success"])
        self.assertTrue(score["task_completed"])

    def test_observation_override_keeps_original_health_and_explicit_injection_source(self):
        m = runner(self)
        original = m.make_observation(1, {"local": {"reachable": True, "ready": True, "latency_ms": 3}}, m.default_profile())
        changed = m.apply_condition(original, {"id": "local_not_ready", "override": {"compute": {"local": {"model_ready": False}}}})
        self.assertTrue(original["compute"]["local"]["model_ready"])
        self.assertFalse(changed["compute"]["local"]["model_ready"])
        self.assertEqual(changed["condition_source"], "injected_observation_not_measured_failure")
        self.assertEqual(changed["condition_id"], "local_not_ready")

    def test_rescoring_preserves_original_report_bytes_and_records_new_scorer_source(self):
        m = runner(self)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.json"
            raw.write_text(json.dumps({"repeat": 3, "status": "finished", "rows": []}), encoding="utf-8")
            before = raw.read_bytes()
            result = m.rescore_report(raw, Path(tmp) / "analysis")
            self.assertEqual(raw.read_bytes(), before)
            self.assertEqual(result["postprocessing"]["scope"], "rescored_existing_rows_no_new_model_inference")
            self.assertTrue((Path(tmp) / "analysis/summary.md").exists())

    def test_wrapper_declaring_stub_cannot_enter_real_model_aggregate(self):
        m = runner(self)
        backend = SimpleNamespace(evidence_kind="real-model", metadata={"protocol": "sensehome-wrapper"})
        event = {"selected_backend": "local-model", "source": "stub", "backend": "local-model"}
        source = m.classify_row(event, [{"response": {"source": "stub"}}], {"local": backend})
        self.assertEqual(source, "unverified-upstream")

    def test_missing_policy_binary_writes_blocked_evidence_without_model_calls(self):
        m = runner(self)
        with tempfile.TemporaryDirectory() as tmp:
            report = m.evaluate([{"id": "case", "command": "ignored", "expected_actions": []}], {},
                                Path(tmp) / "missing-policy.exe", Path(tmp) / "output", repeat=3)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["rows"], [])
            self.assertTrue((Path(tmp) / "output/report.json").exists())


if __name__ == "__main__":
    unittest.main()
