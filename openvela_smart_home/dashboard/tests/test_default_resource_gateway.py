"""Default initializer tests use declared model fixtures, never real model evidence."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_server as bridge
from offload_gateway import BackendResult


class FixtureBackend:
    instances = {}

    def __init__(self, url, name, **kwargs):
        self.name = name
        self.state = dict(ready=True, reachable=True, latency_ms=2, queue_ms=0,
                          inflight=0, free_mem_mb=None)
        self.calls = 0
        self.instances[name] = self

    def health(self):
        return dict(self.state)

    def infer(self, command, context=None):
        self.calls += 1
        return BackendResult(True, False, {"answer": "fixed fixture"}, [
            {"name": "light_control", "params": {"location": "客厅", "brightness": 75}},
            {'name': 'light_control', 'params': {'location': '卧室', 'brightness': 20}},
        ], source="contract-fixture")


class DefaultResourceGatewayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.original_file = bridge.STATE_FILE
        bridge.STATE_FILE = str(Path(self.folder.name) / "state.json")
        bridge.save_state(bridge.default_state())
        self.initial_state = copy.deepcopy(bridge.load_state())
        self.environment = patch.dict(os.environ, {
            "SENSEHOME_CLOUD_URL": "http://127.0.0.1:1",
            "SENSEHOME_LOCAL_URL": "http://127.0.0.1:2",
            "SENSEHOME_POLICY_ARM": "joint",
            "SENSEHOME_EVENT_LOG": str(Path(self.folder.name) / "events.jsonl"),
        })
        self.environment.start()
        self.backend = patch.object(bridge, "HttpJsonBackend", FixtureBackend)
        self.backend.start()
        bridge._init_gateway(start_monitor=False)
        self.cloud = FixtureBackend.instances["cloud-model"]
        self.local = FixtureBackend.instances["local-model"]
        bridge.backend_monitor.refresh_once()

    def tearDown(self):
        bridge.close_gateway()
        self.backend.stop()
        self.environment.stop()
        bridge.STATE_FILE = self.original_file
        self.folder.cleanup()
        bridge._init_gateway()

    def command(self, context=None):
        return bridge.process_command('把客厅灯弄亮些，再把卧室灯弄亮些', context)

    def assert_model_guess_rejected(self, result):
        self.assertFalse(result['semantic_complete'])
        self.assertFalse(result['task_complete'])
        self.assertEqual(result['error'], 'REQUEST_GOAL_UNRESOLVED')
        self.assertEqual(result['gateway_event']['actions'], [])
        self.assertFalse(result['fallback_attempted'])
        self.assertEqual(bridge.load_state(), self.initial_state)

    def test_default_entry_routes_unresolved_goals_and_rejects_model_guessed_targets(self):
        result = self.command()
        self.assert_model_guess_rejected(result)
        event = result["gateway_event"]
        self.assertEqual(event["policy"]["implementation"], "dual_endpoint_C_policy")
        self.assertEqual(event["backend"], "cloud-model")
        self.assertEqual(self.cloud.calls, 1)
        self.assertEqual(self.local.calls, 0)
        self.assertEqual(set(event["observation"]["compute"]), {"local", "cloud"})
        self.assertIsNone(event["observation"]["network"]["wan"]["bandwidth_kbps"])
        self.assertIsNone(event["observation"]["compute"]["local"]["free_mem_mb"])
        rows = Path(self.folder.name, "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[0])["event_id"], event["event_id"])

    def test_complete_explicit_goals_use_local_rules_without_model_calls(self):
        result = bridge.process_command('把客厅灯光设为75%，再把卧室灯光设为20%')
        self.assertEqual(result['backend'], 'local-rules')
        self.assertEqual(result['reason'], 'SERVER_EXPLICIT_GOALS')
        self.assertTrue(result['plan_valid'])
        self.assertTrue(result['execution_complete'])
        self.assertTrue(result['task_complete'])
        self.assertTrue(result['semantic_complete'])
        self.assertEqual(self.cloud.calls + self.local.calls, 0)
        state = bridge.load_state()
        self.assertEqual(state['lights']['客厅'], {'is_on': True, 'brightness': 75})
        self.assertEqual(state['lights']['卧室'], {'is_on': True, 'brightness': 20})

    def test_cloud_unavailable_routes_local(self):
        self.cloud.state["ready"] = False
        bridge.backend_monitor.refresh_once()
        event = self.command()
        self.assert_model_guess_rejected(event)
        self.assertEqual(event["backend"], "local-model")
        self.assertEqual(event["reason"], "MODEL_UNAVAILABLE")
        self.assertEqual(self.cloud.calls, 0)

    def test_local_capacity_and_both_models_unavailable(self):
        self.local.state["queue_ms"] = 650
        bridge.backend_monitor.refresh_once()
        overloaded = self.command()
        self.assertEqual(overloaded["reason"], "LOCAL_COMPUTE_OVERLOADED")
        self.assert_model_guess_rejected(overloaded)
        self.cloud.state["ready"] = self.local.state["ready"] = False
        bridge.backend_monitor.refresh_once()
        event = self.command()
        self.assertEqual(event["backend"], "local-rules")
        self.assertFalse(event["semantic_complete"])
        self.assertEqual(event["reason"], "NO_MODEL_CAPACITY")
        self.assertEqual(bridge.load_state(), self.initial_state)

    def test_untrusted_http_context_cannot_override_decision(self):
        self.cloud.state["ready"] = False
        bridge.backend_monitor.refresh_once()
        event = self.command({
            "compute": {"model_ready": True}, "observation": {"fake": True},
            "policy": {"arm": "static-cloud"}, "observation_age_ms": 0,
            "experiment_request_key": "http-gateway-audit-1",
        })
        self.assertEqual(event["backend"], "local-model")
        self.assertEqual(event["selected_backend"], "local-model")
        self.assertEqual(event["decision_reason"], "MODEL_UNAVAILABLE")
        self.assertIsInstance(event["decision_sequence"], int)
        self.assertEqual(event["request_key"], "http-gateway-audit-1")
        self.assertEqual(event["policy"]["arm"], "joint")
        self.assertFalse(event["resource_observation"]["compute"]["cloud"]["model_ready"])
        self.assert_model_guess_rejected(event)

    def test_stale_cache_blocks_complex_but_not_local_safety(self):
        snapshot = bridge.backend_monitor.snapshot()
        snapshot["age_ms"] = 3000
        snapshot["endpoint_age_ms"] = {"local-model": 3000, "cloud-model": 3000}
        with patch.object(bridge.backend_monitor, "snapshot", return_value=snapshot):
            self.assertEqual(self.command()["backend"], "local-rules")
            self.assertFalse(self.command()["semantic_complete"])
            safety = bridge.process_command("查询安防状态")
            self.assertEqual(safety["backend"], "local-rules")
            self.assertTrue(safety["semantic_complete"])
        self.assertEqual(self.cloud.calls + self.local.calls, 0)
        self.assertEqual(bridge.load_state(), self.initial_state)

    def test_missing_gateway_never_uses_legacy_permissive_path(self):
        actual = bridge.gateway
        try:
            bridge.gateway = None
            with patch.object(bridge, "_legacy_process_command") as legacy:
                event = bridge.process_command("打开客厅灯")
            legacy.assert_not_called()
            self.assertFalse(event["interface_ok"])
        finally:
            bridge.gateway = actual

    def test_missing_policy_binary_is_unavailable_and_does_not_actuate(self):
        before = bridge.load_state()
        with patch.dict(os.environ, {"SENSEHOME_POLICY_CLI": str(Path(self.folder.name) / "absent.exe")}):
            bridge._init_gateway(start_monitor=False)
        result = bridge.process_command("打开客厅灯")
        self.assertFalse(result["interface_ok"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(bridge.gateway_init_error, "policy_cli_unavailable")
        self.assertEqual(bridge.load_state(), before)

    def test_monotonic_age_survives_wall_clock_jump(self):
        before = bridge.backend_monitor.snapshot()["age_ms"]
        with patch("backend_monitor.time.time", return_value=-1e9):
            after = bridge.backend_monitor.snapshot()["age_ms"]
        self.assertGreaterEqual(after, before)
        self.assertLess(after - before, 1000)


if __name__ == "__main__":
    unittest.main()
