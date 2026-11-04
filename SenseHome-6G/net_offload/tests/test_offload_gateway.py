import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import (  # noqa: E402
    BackendResult,
    LinkSnapshot,
    OffloadGateway,
)


class FakePolicy:
    def decide(self, task_class, link):
        if task_class == "safety":
            return "local-rules", "TASK_IS_SAFETY", "safe"
        if task_class == "simple":
            return "local-rules", "TASK_IS_SIMPLE", "simple"
        if not link.wan_ok:
            return "local-model", "WAN_UNREACHABLE", "wan down"
        return "cloud-model", "WAN_OK", "wan ok"


class RecordingBackend:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def infer(self, command, context=None):
        self.calls.append((command, context or {}))
        return self.result

    def health(self):
        return {"ready": True}


class OffloadGatewayTests(unittest.TestCase):
    def make_gateway(self, **kwargs):
        result = BackendResult(
            ok=True,
            semantic_complete=True,
            response={"answer": "ok"},
            actions=[{"description": "light on"}],
            model_ms=12.0,
        )
        backends = {
            "cloud-model": RecordingBackend(result),
            "local-model": RecordingBackend(result),
            "local-rules": RecordingBackend(result),
        }
        backends["local-rules"].result = BackendResult(
            ok=True,
            semantic_complete=False,
            response={"answer": "无法理解"},
            actions=[],
            model_ms=0.0,
            error="UNRESOLVED_NO_NLU",
        )
        kwargs.setdefault(
            "link_snapshot",
            LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3),
        )
        return OffloadGateway(
            policy=FakePolicy(),
            backends=backends,
            **kwargs,
        ), backends

    def test_complex_command_uses_policy_selected_cloud_backend(self):
        gateway, backends = self.make_gateway()
        event = gateway.handle("有点冷")

        self.assertEqual(event["task_class"], "complex")
        self.assertEqual(event["backend"], "cloud-model")
        self.assertEqual(event["reason"], "WAN_OK")
        self.assertTrue(event["semantic_complete"])
        self.assertEqual(len(backends["cloud-model"].calls), 1)

    def test_wan_down_routes_complex_command_to_local_model(self):
        gateway, backends = self.make_gateway(
            link_snapshot=LinkSnapshot(wan_ok=False, wan_rtt_ms=None, lan_ok=True, lan_rtt_ms=3)
        )
        event = gateway.handle("有点冷")

        self.assertEqual(event["backend"], "local-model")
        self.assertEqual(event["reason"], "WAN_UNREACHABLE")
        self.assertEqual(len(backends["local-model"].calls), 1)

    def test_backend_failure_falls_back_to_rules_and_records_error(self):
        gateway, backends = self.make_gateway()
        backends["cloud-model"].result = BackendResult(
            ok=False,
            semantic_complete=False,
            response={},
            actions=[],
            error="timeout",
        )

        event = gateway.handle("有点冷")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertEqual(event["error"], "timeout")
        self.assertFalse(event["semantic_complete"])

    def test_event_is_json_serializable_and_contains_latency_breakdown(self):
        gateway, _ = self.make_gateway()
        event = gateway.handle("打开客厅的灯")

        json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["task_class"], "simple")
        self.assertIn("probe_ms", event["latency"])
        self.assertIn("policy_ms", event["latency"])
        self.assertIn("backend_ms", event["latency"])
        self.assertIn("total_user_ms", event["latency"])

    def test_compound_conditional_and_negative_commands_require_model_planning(self):
        for command in (
            "打开客厅灯然后关闭卧室灯",
            "把客厅灯光设为60%，再把卧室灯光设为30%",
            "如果卧室温度超过28度就打开窗帘",
            "打开客厅和卧室的灯",
            "不要关闭客厅的灯",
        ):
            with self.subTest(command=command):
                gateway, backends = self.make_gateway()
                event = gateway.handle(command)
                self.assertEqual(event["task_class"], "complex")
                self.assertEqual(event["backend"], "cloud-model")
                self.assertEqual(len(backends["local-rules"].calls), 0)


if __name__ == "__main__":
    unittest.main()
