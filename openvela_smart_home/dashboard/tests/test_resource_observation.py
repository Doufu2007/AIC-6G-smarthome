import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bridge_server  # noqa: E402
from offload_gateway import LinkSnapshot, OffloadGateway, RuleBackend  # noqa: E402


class ObservationPolicy:
    def decide_observation(self, task_class, observation):
        return "cloud-model", "JOINT_POLICY", "observation accepted"


class RecordingGateway:
    def __init__(self):
        self.policy = ObservationPolicy()
        self.calls = []

    def handle(self, command, context=None, observation=None):
        self.calls.append((command, context, observation))
        return {
            "event_id": "observation-event",
            "task_class": "complex",
            "backend": "cloud-model",
            "reason": "JOINT_POLICY",
            "semantic_complete": False,
            "interface_ok": True,
            "response": {"answer": "accepted", "actions": []},
            "actions": [],
            "error": None,
        }


class Monitor:
    def __init__(self):
        self.sampled_at = 10.0
        self.age_ms = 5.0

    def snapshot(self):
        return {
            "sampled_at": self.sampled_at,
            "age_ms": self.age_ms,
            "link": {
                "wan_ok": True,
                "wan_rtt_ms": 38.5,
                "lan_ok": True,
                "lan_rtt_ms": 0.0,
            },
            "link_basis": "HTTP endpoint RTT; devices simulated in gateway process",
            "backends": {
                "cloud-model": {
                    "ready": True,
                    "model_ready": True,
                    "queue_ms": 0,
                    "queue_scope": "wrapper_fail_fast_admission",
                    "upstream_queue_ms": None,
                    "free_mem_mb": None,
                    "inflight": 2,
                },
                "local-model": {
                    "ready": False,
                    "model_ready": False,
                    "queue_ms": None,
                    "free_mem_mb": None,
                    "inflight": 0,
                },
            },
        }


class ResourceObservationTests(unittest.TestCase):
    def setUp(self):
        self.old_gateway = bridge_server.gateway
        self.old_monitor = bridge_server.backend_monitor
        self.old_profiles = getattr(bridge_server, "gateway_service_profiles", None)
        bridge_server.gateway = RecordingGateway()
        bridge_server.backend_monitor = Monitor()
        bridge_server.gateway_service_profiles = {
            "interactive_inference": {
                "task_class": "complex",
                "deadline_ms": 1200,
                "request_bytes": 128,
                "response_bytes": 256,
                "wan_rtt_max_ms": 300,
                "wan_jitter_max_ms": 30,
                "wan_loss_max_pct": 5,
                "reliability_target": 0.99,
            }
        }

    def tearDown(self):
        bridge_server.gateway = self.old_gateway
        bridge_server.backend_monitor = self.old_monitor
        if self.old_profiles is None:
            del bridge_server.gateway_service_profiles
        else:
            bridge_server.gateway_service_profiles = self.old_profiles

    def test_server_owned_observation_preserves_unknowns_and_sources(self):
        result = bridge_server.process_command(
            "请把客厅调整为阅读模式",
            {"compute": {"model_ready": True, "queue_ms": 0},
             "resource_observation": {"network": {"wan": {"bandwidth_kbps": 999999}}}},
        )
        command, context, observation = bridge_server.gateway.calls[-1]

        self.assertEqual(command, "请把客厅调整为阅读模式")
        self.assertEqual(context["compute"]["inflight"], 2)
        self.assertIsNone(context["compute"]["queue_ms"])
        self.assertEqual(observation["compute"]["cloud"]["inflight"], 2)
        self.assertIsNone(observation["compute"]["cloud"]["queue_ms"])
        self.assertIsNone(observation["compute"]["cloud"]["free_mem_mb"])
        self.assertEqual(observation["network"]["wan"]["rtt_ms"], 38.5)
        self.assertIsNone(observation["network"]["wan"]["bandwidth_kbps"])
        self.assertEqual(observation["network"]["wan"]["source"], "http_health_check")
        self.assertEqual(observation["network"]["lan"]["source"], "same_process_simulator")
        self.assertEqual(observation["network_source"], "application_health_and_simulated_lan")
        self.assertEqual(result["resource_observation"], observation)

    def test_observation_age_advances_monotonically_and_generation_changes_per_sample(self):
        with mock.patch.object(bridge_server.time, "monotonic", side_effect=[100.0, 100.05, 101.0, 101.1]):
            bridge_server.process_command("请把客厅调整为阅读模式")
            first = bridge_server.gateway.calls[-1][2]
            bridge_server.backend_monitor.age_ms = 5000.0
            bridge_server.process_command("请把客厅调整为阅读模式")
            second = bridge_server.gateway.calls[-1][2]
            bridge_server.backend_monitor.sampled_at = 11.0
            bridge_server.backend_monitor.age_ms = 2.0
            bridge_server.process_command("请把客厅调整为阅读模式")
            third = bridge_server.gateway.calls[-1][2]
            bridge_server.backend_monitor.sampled_at = 10.5
            bridge_server.backend_monitor.age_ms = 0.0
            bridge_server.process_command("请把客厅调整为阅读模式")
            fourth = bridge_server.gateway.calls[-1][2]

        self.assertEqual(first["observation_id"], second["observation_id"])
        self.assertAlmostEqual(second["observed_age_ms"] - first["observed_age_ms"], 50.0, delta=0.1)
        self.assertGreater(third["observation_id"], second["observation_id"])
        self.assertEqual(third["observed_age_ms"], 2.0)
        self.assertGreater(fourth["observation_id"], third["observation_id"])
        self.assertTrue(fourth["source_out_of_order"])
        self.assertIsNone(fourth["observed_age_ms"])

    def test_unavailable_gateway_does_not_fall_back_to_legacy_success(self):
        bridge_server.gateway = None
        bridge_server.gateway_init_error = "policy_cli_unavailable"

        result = bridge_server.process_command("打开客厅的灯")

        self.assertFalse(result["interface_ok"])
        self.assertFalse(result["semantic_complete"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["error"], "policy_cli_unavailable")

    def test_missing_policy_cli_leaves_gateway_unavailable(self):
        class Monitor:
            closed = False

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return self

            def close(self):
                self.closed = True

        original_gateway = bridge_server.gateway
        original_monitor = bridge_server.backend_monitor
        original_error = bridge_server.gateway_init_error
        original_monitor_class = bridge_server.BackendMonitor
        bridge_server.gateway = None
        bridge_server.backend_monitor = None
        bridge_server.gateway_init_error = None
        try:
            with mock.patch.object(bridge_server, "BackendMonitor", Monitor), \
                    mock.patch.object(bridge_server.os.path, "isfile", return_value=False), \
                    mock.patch.dict(os.environ, {"SENSEHOME_POLICY_CLI": "missing-policy-cli"}):
                bridge_server._init_gateway()
            self.assertIsNone(bridge_server.gateway)
            self.assertIsNone(bridge_server.backend_monitor)
            self.assertEqual(bridge_server.gateway_init_error, "policy_cli_unavailable")
            response = bridge_server.process_command("打开客厅的灯")
            self.assertFalse(response["semantic_complete"])
            self.assertFalse(response["interface_ok"])
        finally:
            bridge_server.gateway = original_gateway
            bridge_server.backend_monitor = original_monitor
            bridge_server.gateway_init_error = original_error
            bridge_server.BackendMonitor = original_monitor_class

    def test_explicit_curtain_and_named_scene_work_but_ambiguous_fuzzy_needs_target(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = os.path.join(folder, "device_state.json")
                state = bridge_server.default_state()
                bridge_server.save_state(state)
                with mock.patch.object(bridge_server, 'generate_sensor_data'):
                    curtain = bridge_server._gateway_local_handler("打开客厅窗帘")
                    self.assertTrue(state["curtains"]["客厅"]["is_open"])
                    self.assertTrue(curtain["semantic_complete"])

                    cold = bridge_server._gateway_local_handler("有点冷")
                    self.assertFalse(cold["semantic_complete"])
                    self.assertEqual(cold["actions"], [])

                    leave = bridge_server._gateway_local_handler("离家模式")
                    self.assertFalse(leave['semantic_complete'])
                    self.assertTrue(leave['supported_scope_complete'])
                    current = bridge_server.load_state()
                    self.assertFalse(current['security']['armed'])
                    self.assertFalse(current['lights']['客厅']['is_on'])
                    self.assertFalse(current['curtains']['客厅']['is_open'])
        finally:
            bridge_server.STATE_FILE = original_file

    def test_simple_and_safety_commands_skip_synchronous_external_probe(self):
        probes = []

        class Policy:
            def decide(self, task_class, link):
                del link
                return "local-rules", "TASK_IS_" + task_class.upper(), "local policy"

        original_gateway, original_monitor = bridge_server.gateway, bridge_server.backend_monitor
        original_file = bridge_server.STATE_FILE
        try:
            bridge_server.backend_monitor = None
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = os.path.join(folder, "device_state.json")
                bridge_server.save_state(bridge_server.default_state())
                bridge_server.gateway = OffloadGateway(
                    policy=Policy(),
                    backends={"local-rules": RuleBackend(handler=bridge_server._gateway_local_handler)},
                    link_snapshot=LinkSnapshot(lan_ok=True, lan_rtt_ms=0),
                    link_probe=lambda: probes.append(True) or LinkSnapshot(lan_ok=True, lan_rtt_ms=0),
                )
                bridge_server.process_command("打开客厅的灯")
                bridge_server.process_command("关闭燃气报警器")
            self.assertEqual(probes, [])
        finally:
            bridge_server.gateway, bridge_server.backend_monitor = original_gateway, original_monitor
            bridge_server.STATE_FILE = original_file


if __name__ == "__main__":
    unittest.main()
