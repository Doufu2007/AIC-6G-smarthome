import json
import os
import sys
import threading
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
        self.assertFalse(event["semantic_complete"], 'planning alone cannot accept request goals')
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

    def test_event_persists_policy_description(self):
        gateway, _ = self.make_gateway()
        gateway.policy.describe = lambda: {
            "implementation": "test-policy",
            "active_decision_path": "fixture",
        }

        event = gateway.handle("打开客厅的灯")

        self.assertEqual(event["policy"], {
            "implementation": "test-policy",
            "active_decision_path": "fixture",
            "decision_sequence": 1,
        })

    def test_event_records_bounded_request_key_and_original_policy_choice(self):
        gateway, backends = self.make_gateway()
        backends["cloud-model"].result = BackendResult(
            ok=False, semantic_complete=False, response={}, actions=[], error="timeout")

        event = gateway.handle("有点冷", {"experiment_request_key": "request-17"})

        self.assertEqual(event["request_key"], "request-17")
        self.assertIsInstance(event["decision_sequence"], int)
        self.assertEqual(event["selected_backend"], "cloud-model")
        self.assertEqual(event["decision_reason"], "WAN_OK")
        self.assertEqual(event["backend"], "local-rules")

    def test_untrusted_long_request_key_is_not_persisted(self):
        gateway, _ = self.make_gateway()

        event = gateway.handle("打开客厅的灯", {"experiment_request_key": "x" * 257})

        self.assertNotIn("request_key", event)

    def test_concurrent_event_keeps_its_own_policy_description(self):
        gateway, _ = self.make_gateway()
        first_decision_entered = threading.Event()
        release_first_decision = threading.Event()
        second_decision_entered = threading.Event()

        class SequencedPolicy(FakePolicy):
            active_path = None

            def decide_observation(self, task_class, observation):
                self.active_path = observation["path"]
                if self.active_path == "first":
                    first_decision_entered.set()
                    release_first_decision.wait(2)
                else:
                    second_decision_entered.set()
                return "cloud-model", "WAN_OK", self.active_path

            def describe(self):
                return {"implementation": "sequenced",
                        "active_decision_path": self.active_path}

        gateway.policy = SequencedPolicy()
        events = {}
        first = threading.Thread(
            name="first", target=lambda: events.setdefault(
                "first", gateway.handle("有点冷", observation={"path": "first"})))
        second = threading.Thread(
            name="second", target=lambda: events.setdefault(
                "second", gateway.handle("有点冷", observation={"path": "second"})))
        first.start()
        self.assertTrue(first_decision_entered.wait(1))
        second.start()
        try:
            self.assertFalse(second_decision_entered.wait(0.2))
        finally:
            release_first_decision.set()
            first.join(timeout=3)
            second.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(events["first"]["policy"]["active_decision_path"], "first")
        self.assertEqual(events["second"]["policy"]["active_decision_path"], "second")

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

    def test_link_probe_and_policy_decision_keep_request_order(self):
        first_probe_entered = threading.Event()
        release_first_probe = threading.Event()
        second_probe_entered = threading.Event()
        probe_lock = threading.Lock()
        probe_index = 0
        policy_links = []

        class SequencedPolicy:
            def decide(self, task_class, link):
                policy_links.append(link.wan_ok)
                return ("cloud-model", "WAN_OK", "fresh") if link.wan_ok else (
                    "local-model", "WAN_UNREACHABLE", "stale")

        def probe():
            nonlocal probe_index
            with probe_lock:
                index = probe_index
                probe_index += 1
            if index == 0:
                first_probe_entered.set()
                release_first_probe.wait(2)
                return LinkSnapshot(wan_ok=False, wan_rtt_ms=None, lan_ok=True, lan_rtt_ms=3)
            second_probe_entered.set()
            return LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3)

        gateway, _ = self.make_gateway(link_probe=probe)
        gateway.policy = SequencedPolicy()
        events = {}
        first = threading.Thread(target=lambda: events.setdefault(
            "first", gateway.handle("complex order test", {"experiment_request_key": "first"})))
        second = threading.Thread(target=lambda: events.setdefault(
            "second", gateway.handle("complex order test", {"experiment_request_key": "second"})))
        first.start()
        self.assertTrue(first_probe_entered.wait(1))
        second.start()
        try:
            self.assertFalse(second_probe_entered.wait(0.2))
        finally:
            release_first_probe.set()
            first.join(3)
            second.join(3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(events["first"]["decision_sequence"], 1)
        self.assertEqual(events["second"]["decision_sequence"], 2)
        self.assertEqual(policy_links, [False, True])

    def test_event_sink_failure_is_returned_after_action_without_raising(self):
        gateway, backends = self.make_gateway()
        executed = []
        actions = [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}},
                   {'name': 'curtain_control', 'params': {'location': '卧室', 'action': 'open'}}]
        backends['cloud-model'].result = BackendResult(True, False, {}, actions)

        def fail_sink(event):
            raise OSError("event storage unavailable")

        gateway.event_sink = fail_sink
        gateway.action_executor = lambda actions, context: executed.append(actions) or {
            "actions": actions, "semantic_complete": True, 'plan_valid': True,
            'execution_complete': True, 'task_complete': True,
        }

        event = gateway.handle('把客厅灯光设为60%，再打开卧室窗帘')

        self.assertEqual(len(executed), 1)
        self.assertTrue(event["semantic_complete"])
        self.assertFalse(event["event_persisted"])
        self.assertEqual(event["event_persistence_error"], "OSError")


if __name__ == "__main__":
    unittest.main()
