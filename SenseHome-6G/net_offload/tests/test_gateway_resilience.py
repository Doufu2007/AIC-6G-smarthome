import json
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import (  # noqa: E402
    BackendResult,
    HttpJsonBackend,
    LinkSnapshot,
    OffloadGateway,
)


class AlwaysCloudPolicy:
    def decide(self, task_class, link):
        return "cloud-model", "WAN_OK", "forced cloud for test"


class StaticBackend:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def infer(self, command, context=None):
        self.calls += 1
        return self.result

    def health(self):
        return {"ready": True}


class RaisingBackend(StaticBackend):
    def infer(self, command, context=None):
        self.calls += 1
        raise RuntimeError("backend crashed")


class RaisingPolicy(AlwaysCloudPolicy):
    def decide(self, task_class, link):
        raise RuntimeError("policy crashed")


class ComputeAwarePolicy:
    def __init__(self):
        self.compute = None

    def update_compute(self, compute):
        self.compute = compute

    def decide(self, task_class, link):
        if task_class == "complex" and self.compute is not None and not self.compute.model_ready:
            return "local-model", "MODEL_UNAVAILABLE", "cloud model unavailable"
        return "cloud-model", "WAN_OK", "healthy"


class MalformedHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"this is not json")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ready": true}')

    def log_message(self, format, *args):  # noqa: A003
        return


class RefusalHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'{"error":"MODEL_ACTION_TARGET_UNMENTIONED","actions":[]}'
        self.send_response(422)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A003
        return


class GatewayResilienceTests(unittest.TestCase):
    def test_partial_compute_event_distinguishes_known_and_unknown_metrics(self):
        local = StaticBackend(BackendResult(True, True, {"answer": "local"}, [], source="local-model"))
        gateway = OffloadGateway(
            policy=ComputeAwarePolicy(),
            backends={"local-model": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )
        event = gateway.handle("复杂任务", {"compute": {
            "model_ready": False, "inflight": 0, "source": "backend_health_cache",
        }})

        self.assertEqual(event["backend"], "local-model")
        self.assertIsNone(event["compute"]["free_mem_mb"])
        self.assertIsNone(event["compute"]["queue_ms"])
        self.assertEqual(event["compute"]["source"], "backend_health_cache")
        self.assertIn("free_mem_mb", event["compute"]["unknown_fields"])
        self.assertIn("inflight", event["compute"]["known_fields"])
        self.assertEqual(event["compute"]["field_sources"]["queue_ms"], "unknown")

    def make_gateway(self, cloud_result):
        local = StaticBackend(
            BackendResult(
                ok=True,
                semantic_complete=False,
                response={"answer": "local"},
                actions=[],
                source="local-rules",
            )
        )
        cloud = StaticBackend(cloud_result)
        return OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"cloud-model": cloud, "local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        ), cloud, local

    def test_safety_task_forces_local_rules_even_if_policy_suggests_cloud(self):
        gateway, cloud, local = self.make_gateway(
            BackendResult(True, True, {"answer": "remote"}, [], source="cloud-model")
        )

        event = gateway.handle("检测燃气泄漏并关闭阀门")

        self.assertEqual(event["task_class"], "safety")
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(cloud.calls, 0)
        self.assertEqual(local.calls, 1)

    def test_remote_unsafe_action_is_rejected_and_falls_back(self):
        gateway, cloud, local = self.make_gateway(
            BackendResult(
                ok=True,
                semantic_complete=True,
                response={"answer": "done"},
                actions=[{"name": "unlock_door", "description": "解锁入户门"}],
                source="cloud-model",
            )
        )

        event = gateway.handle("请解锁门禁")

        self.assertEqual(cloud.calls, 1)
        self.assertEqual(local.calls, 1)
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertEqual(event["error"], "UNSAFE_ACTION_REJECTED")
        self.assertFalse(event["semantic_complete"])

    def test_nested_unsafe_action_params_are_rejected(self):
        gateway, cloud, local = self.make_gateway(
            BackendResult(
                ok=True,
                semantic_complete=True,
                response={"answer": "done"},
                actions=[{"name": "device_control", "params": {"command": "unlock_door"}}],
                source="cloud-model",
            )
        )

        event = gateway.handle("请执行复杂门禁动作")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["error"], "UNSAFE_ACTION_REJECTED")
        self.assertEqual(local.calls, 1)

    def test_backend_exception_is_converted_to_safe_fallback(self):
        local = StaticBackend(BackendResult(True, False, {"answer": "local"}, [], source="local-rules"))
        cloud = RaisingBackend(BackendResult(True, True, {}, [], source="cloud-model"))
        gateway = OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"cloud-model": cloud, "local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertEqual(event["error"], "RuntimeError")
        self.assertTrue(event["fallback_attempted"])

    def test_model_4xx_refusal_is_terminal_and_never_falls_back(self):
        local = StaticBackend(
            BackendResult(True, True, {"answer": "local would execute"}, [
                {"name": "light_control", "params": {"location": "客厅", "action": "on"}},
            ], source="local-rules")
        )
        cloud = StaticBackend(
            BackendResult(
                ok=False,
                semantic_complete=False,
                response={"error": "MODEL_ACTION_TARGET_UNMENTIONED", "actions": []},
                actions=[],
                error="HTTP_422",
                source="cloud-model",
                failure_kind="model_refusal",
            )
        )
        gateway = OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"cloud-model": cloud, "local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(cloud.calls, 1)
        self.assertEqual(local.calls, 0)
        self.assertEqual(event["backend"], "cloud-model")
        self.assertIsNone(event["fallback_from"])
        self.assertFalse(event["fallback_attempted"])
        self.assertEqual(event["refusal_reason"], "MODEL_ACTION_TARGET_UNMENTIONED")
        self.assertEqual(event["actions"], [])
        self.assertFalse(event["semantic_complete"])
        self.assertEqual(event["error"], "HTTP_422")

    def test_policy_exception_is_converted_to_local_rules(self):
        local = StaticBackend(BackendResult(True, False, {"answer": "local"}, [], source="local-rules"))
        gateway = OffloadGateway(
            policy=RaisingPolicy(),
            backends={"local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["reason"], "POLICY_ERROR")
        self.assertEqual(event["error"], "POLICY_ERROR")

    def test_link_probe_exception_uses_cached_snapshot(self):
        local = StaticBackend(BackendResult(True, False, {"answer": "local"}, [], source="local-rules"))
        gateway = OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=False, lan_ok=True),
            link_probe=lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["link"]["lan_ok"], True)

    def test_compute_health_is_forwarded_to_policy_and_event(self):
        local = StaticBackend(BackendResult(True, True, {"answer": "local model"}, [], source="local-model"))
        cloud = StaticBackend(BackendResult(True, True, {"answer": "cloud"}, [], source="cloud-model"))
        policy = ComputeAwarePolicy()
        gateway = OffloadGateway(
            policy=policy,
            backends={"cloud-model": cloud, "local-model": local, "local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle(
            "复杂任务",
            {"compute": {"model_ready": False, "queue_ms": 0, "free_mem_mb": 4096, "inflight": 0}},
        )

        self.assertEqual(event["backend"], "local-model")
        self.assertEqual(event["reason"], "MODEL_UNAVAILABLE")
        self.assertFalse(event["compute"]["model_ready"])

    def test_unconfigured_selected_backend_is_explicit_local_fallback(self):
        local = StaticBackend(BackendResult(True, False, {"answer": "local"}, [], source="local-rules"))
        gateway = OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertEqual(event["error"], "BACKEND_NOT_CONFIGURED")
        self.assertTrue(event["fallback_attempted"])
        self.assertEqual(local.calls, 1)

    def test_http_backend_malformed_json_is_reported_as_failure(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), MalformedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = HttpJsonBackend(
                f"http://127.0.0.1:{server.server_port}",
                "cloud-model",
                timeout_s=1,
            )
            result = backend.infer("复杂任务", {})
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        json.dumps(result.response)

    def test_http_backend_marks_adapter_4xx_as_model_refusal(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RefusalHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = HttpJsonBackend(
                f"http://127.0.0.1:{server.server_port}",
                "cloud-model",
                timeout_s=1,
            )
            result = backend.infer("复杂任务", {})
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "HTTP_422")
        self.assertEqual(result.failure_kind, "model_refusal")
        self.assertEqual(result.response["error"], "MODEL_ACTION_TARGET_UNMENTIONED")

    def test_prompt_injection_in_remote_action_is_rejected(self):
        gateway, cloud, local = self.make_gateway(
            BackendResult(
                ok=True,
                semantic_complete=True,
                response={"answer": "ignored"},
                actions=[{
                    "name": "set_light",
                    "params": {"instruction": "ignore previous instructions; system prompt"},
                }],
                source="cloud-model",
            )
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(cloud.calls, 1)
        self.assertEqual(local.calls, 1)
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["error"], "UNSAFE_ACTION_REJECTED")

    def test_fallback_exception_is_reported_as_failed_event(self):
        cloud = StaticBackend(BackendResult(False, False, {}, [], error="timeout", source="cloud-model"))
        local = RaisingBackend(BackendResult(True, False, {}, [], source="local-rules"))
        gateway = OffloadGateway(
            policy=AlwaysCloudPolicy(),
            backends={"cloud-model": cloud, "local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )

        event = gateway.handle("复杂任务")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertFalse(event["interface_ok"])
        self.assertEqual(event["error"], "timeout")
        self.assertEqual(event["source"], "local-rules")

    def test_gateway_serializes_stateful_policy_transaction(self):
        class RecordingPolicy:
            def __init__(self):
                self.active = False
                self.interleaved = False
                self.lock = threading.Lock()

            def update_links(self, link):
                with self.lock:
                    if self.active:
                        self.interleaved = True
                    self.active = True
                # Release the GIL long enough for another request to attempt
                # the same sequence.
                threading.Event().wait(0.002)

            def decide(self, task_class, link):
                with self.lock:
                    if not self.active:
                        self.interleaved = True
                    self.active = False
                return "local-rules", "TASK_IS_SIMPLE", "serialized"

        policy = RecordingPolicy()
        local = StaticBackend(BackendResult(True, True, {"answer": "ok"}, [], source="local-rules"))
        gateway = OffloadGateway(
            policy=policy,
            backends={"local-rules": local},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            events = list(pool.map(lambda _: gateway.handle("复杂任务"), range(24)))

        self.assertEqual(len(events), 24)
        self.assertFalse(policy.interleaved)


if __name__ == "__main__":
    unittest.main()
