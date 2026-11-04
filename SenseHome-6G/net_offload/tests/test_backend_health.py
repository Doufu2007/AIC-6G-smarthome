import os
import sys
import unittest
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import HttpJsonBackend  # noqa: E402


class BackendHealthTests(unittest.TestCase):
    def test_http_reachability_is_independent_of_model_ready(self):
        backend = HttpJsonBackend("http://localhost:1", "cloud-model")
        with mock.patch.object(backend, "_request", return_value=(True, {"ready": False, "reachable": False}, 12.0, None)):
            health = backend.health()

        self.assertTrue(health["reachable"])
        self.assertFalse(health["ready"])
        self.assertEqual(health["latency_ms"], 12.0)

    def test_failed_http_cannot_be_overridden_by_backend_ready_field(self):
        backend = HttpJsonBackend("http://localhost:1", "cloud-model")
        with mock.patch.object(backend, "_request", return_value=(False, {"ready": True, "reachable": True}, 12.0, "HTTP_503")):
            health = backend.health()

        self.assertFalse(health["reachable"])
        self.assertFalse(health["ready"])
        self.assertEqual(health["error"], "HTTP_503")

    def test_health_and_inference_have_independent_timeouts(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.15)
                body = b'{"ready": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            def do_POST(self):
                time.sleep(0.01)
                body = json.dumps({
                    "answer": "ok", "actions": [], "source": "real-model",
                    "upstream_model": "model-v1", "model_ms": 10.0,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backend = HttpJsonBackend(
                f"http://127.0.0.1:{server.server_port}",
                "cloud-model",
                timeout_s=0.3,
                health_timeout_s=0.03,
                inference_timeout_s=0.3,
            )
            health = backend.health()
            result = backend.infer("复杂任务", {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertFalse(health["reachable"])
        self.assertEqual(health["error"], "TimeoutError")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "real-model")
        self.assertEqual(result.response["upstream_model"], "model-v1")

    def test_infer_preserves_wrapper_source_and_model_metadata(self):
        backend = HttpJsonBackend("http://localhost:1", "cloud-model")
        payload = {
            "answer": "ok", "actions": [], "source": "real-model",
            "upstream_model": "sensehome-large", "gpu_info": "A10",
            "gpu_info_source": "environment_declared", "model_ms_kind": "upstream_http_wall_clock",
        }
        with mock.patch.object(backend, "_request", return_value=(True, payload, 12.0, None)):
            result = backend.infer("复杂任务", {"trace": "x"})

        self.assertEqual(result.source, "real-model")
        self.assertEqual(result.response["source"], "real-model")
        self.assertEqual(result.response["upstream_model"], "sensehome-large")
        self.assertEqual(result.response["gpu_info_source"], "environment_declared")
        self.assertEqual(result.response["model_ms_kind"], "upstream_http_wall_clock")


if __name__ == "__main__":
    unittest.main()
