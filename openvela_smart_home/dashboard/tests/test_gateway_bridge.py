import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bridge_server  # noqa: E402


class FakeGateway:
    def __init__(self):
        self.commands = []

    def handle(self, command, context=None):
        self.commands.append((command, context))
        return {
            "event_id": "evt-1",
            "backend": "local-rules",
            "reason": "TASK_IS_SIMPLE",
            "latency": {"probe_ms": 1.0, "policy_ms": 2.0, "backend_ms": 3.0, "total_user_ms": 6.0},
            "semantic_complete": True,
            "actions": [{"description": "light on"}],
            "response": {"answer": "ok", "observation": "done", "thought": "rule"},
            "error": None,
            "link": {"wan_ok": True, "lan_ok": True},
        }

    def health(self):
        return {"link": {"wan_ok": True, "lan_ok": True}, "backends": {"local-rules": {"ready": True}}}


class DashboardGatewayTests(unittest.TestCase):
    def test_partial_wrapper_health_is_forwarded_with_unknown_metrics(self):
        class Monitor:
            def snapshot(self):
                return {"age_ms": 2, "backends": {"cloud-model": {
                    "ready": False, "inflight": 9, "queue_ms": 0,
                    "queue_scope": "wrapper_fail_fast_admission", "upstream_queue_ms": None,
                }}}

        bridge_server.backend_monitor = Monitor()
        bridge_server.process_command('把客厅灯光设为60%，再打开卧室窗帘')
        context = bridge_server.gateway.commands[-1][1]
        self.assertIn("compute", context)
        self.assertFalse(context["compute"]["model_ready"])
        self.assertEqual(context["compute"]["inflight"], 9)
        self.assertIsNone(context["compute"]["queue_ms"])
        self.assertIsNone(context["compute"]["free_mem_mb"])
        self.assertEqual(context["compute"]["source"], "backend_health_cache")

    def test_complete_health_keeps_legacy_four_fields(self):
        class Monitor:
            def snapshot(self):
                return {"age_ms": 2, "backends": {"cloud-model": {
                    "model_ready": True, "queue_ms": 100, "free_mem_mb": 1024, "inflight": 2,
                }}}

        bridge_server.backend_monitor = Monitor()
        bridge_server.process_command('把客厅灯光设为60%，再打开卧室窗帘')
        context = bridge_server.gateway.commands[-1][1]
        self.assertEqual(context["compute"]["free_mem_mb"], 1024)
        self.assertEqual(context["compute"]["queue_ms"], 100)

    def setUp(self):
        self.original = bridge_server.gateway
        self.original_monitor = bridge_server.backend_monitor
        bridge_server.gateway = FakeGateway()
        bridge_server.backend_monitor = None

    def tearDown(self):
        bridge_server.gateway = self.original
        bridge_server.backend_monitor = self.original_monitor

    def test_process_command_returns_gateway_event_and_legacy_response(self):
        result = bridge_server.process_command("打开客厅的灯")

        self.assertEqual(result["answer"], "ok")
        self.assertEqual(result["backend"], "local-rules")
        self.assertEqual(result["reason"], "TASK_IS_SIMPLE")
        self.assertEqual(result["event_id"], "evt-1")
        self.assertEqual(result["latency"]["total_user_ms"], 6.0)

    def test_gateway_health_is_exposed_for_dashboard(self):
        health = bridge_server.gateway_health()

        self.assertTrue(health["backends"]["local-rules"]["ready"])
        self.assertTrue(health["link"]["wan_ok"])

    def test_process_command_preserves_real_model_metadata(self):
        bridge_server.gateway = FakeGateway()
        bridge_server.gateway.handle = lambda command, context=None: {
            "event_id": "evt-real",
            "backend": "cloud-model",
            "reason": "WAN_OK",
            "latency": {"probe_ms": 1.0, "policy_ms": 1.0, "backend_ms": 5.0, "total_user_ms": 7.0},
            "semantic_complete": True,
            "interface_ok": True,
            "response": {
                "answer": "已执行",
                "actions": [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}],
                "source": "real-model",
                "upstream_model": "sensehome-large",
                "model_ms": 4.2,
                "model_ms_kind": "upstream_http_wall_clock",
                "gpu_info_source": "environment_declared",
            },
            "actions": [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}],
            "source": "real-model",
            "error": None,
            "link": {"wan_ok": True, "lan_ok": True},
        }

        result = bridge_server.process_command('把客厅灯光设为60%，再打开卧室窗帘')

        self.assertEqual(result["source"], "real-model")
        self.assertEqual(result["upstream_model"], "sensehome-large")
        self.assertEqual(result["model_ms_kind"], "upstream_http_wall_clock")
        self.assertEqual(result["gpu_info_source"], "environment_declared")

    def test_process_command_preserves_model_refusal_metadata(self):
        bridge_server.gateway = FakeGateway()
        bridge_server.gateway.handle = lambda command, context=None: {
            "event_id": "evt-refusal",
            "backend": "cloud-model",
            "reason": "WAN_OK",
            "latency": {"probe_ms": 1.0, "policy_ms": 1.0, "backend_ms": 5.0, "total_user_ms": 7.0},
            "semantic_complete": False,
            "interface_ok": False,
            "response": {"error": "MODEL_ACTION_TARGET_UNMENTIONED", "actions": []},
            "actions": [],
            "source": "real-model",
            "error": "HTTP_422",
            "fallback_attempted": False,
            "refusal_reason": "MODEL_ACTION_TARGET_UNMENTIONED",
            "link": {"wan_ok": True, "lan_ok": True},
        }
        result = bridge_server.process_command('打开灯')
        self.assertFalse(result["fallback_attempted"])
        self.assertEqual(result["refusal_reason"], "MODEL_ACTION_TARGET_UNMENTIONED")


if __name__ == "__main__":
    unittest.main()
