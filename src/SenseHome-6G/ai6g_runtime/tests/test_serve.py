import json
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime import serve
from ai6g_runtime.health_monitor import CalibrationEntry


class _ModelHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.server.model_requests.append(json.loads(self.rfile.read(size)))
        content = json.dumps(
            {"result_kind": "clarification", "actions": [], "explanation": "probe"},
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ServeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.model_server = HTTPServer(("127.0.0.1", 0), _ModelHandler)
        self.model_server.model_requests = []
        self.model_thread = threading.Thread(target=self.model_server.serve_forever, daemon=True)
        self.model_thread.start()
        self.model_url = f"http://127.0.0.1:{self.model_server.server_port}/v1/chat/completions"

    def tearDown(self):
        self.model_server.shutdown()
        self.model_server.server_close()
        self.model_thread.join(timeout=2)

    def _start_runtime_server(self, runtime):
        handler = type("BoundRuntimeHandler", (serve.RuntimeHandler,), {"runtime": runtime})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _wait_until(predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_build_runtime_persists_state_from_explicit_or_environment_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.dict(os.environ, {}, clear=False):
                first = serve.build_runtime(Path(tmp) / "requests.jsonl", state_path=state_path)
            result = first.handle("打开客厅的灯", request_id="persist-1")
            self.assertTrue(result["ok"])
            self.assertTrue(state_path.exists())

            with patch.dict(os.environ, {"AI6G_STATE_PATH": str(state_path)}, clear=False):
                second = serve.build_runtime(Path(tmp) / "requests-2.jsonl")
            self.assertTrue(second.state()["lights"]["客厅"]["is_on"])

    def test_build_runtime_reads_resource_route_parameters(self):
        values = {
            "AI6G_RESOURCE_TTL_S": "7",
            "AI6G_MAX_QUEUE_MS": "180",
            "AI6G_MIN_FREE_MEM_MB": "512",
            "AI6G_MAX_INFLIGHT": "4",
            "AI6G_ROUTE_SWITCH_MARGIN_MS": "12",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, values, clear=False):
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
        self.assertEqual(runtime.router.resource_ttl_s, 7.0)
        self.assertEqual(runtime.router.max_queue_ms, 180.0)
        self.assertEqual(runtime.router.min_free_mem_mb, 512.0)
        self.assertEqual(runtime.router.max_inflight, 4)
        self.assertEqual(runtime.router.switch_margin_ms, 12.0)

    def test_parser_accepts_probe_and_state_options(self):
        parser = serve.build_parser()
        args = parser.parse_args(
            [
                "--state",
                "state.json",
                "--calibration",
                "calibration.json",
                "--probe-interval",
                "2.5",
                "--probe-log",
                "probe.jsonl",
            ]
        )
        self.assertEqual(args.state, "state.json")
        self.assertEqual(args.calibration, "calibration.json")
        self.assertEqual(args.probe_interval, 2.5)
        self.assertEqual(args.probe_log, "probe.jsonl")

    def test_parser_rejects_non_finite_probe_interval(self):
        parser = serve.build_parser()
        for value in ("nan", "inf", "-inf"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--probe-interval", value])

    def test_probe_thread_makes_real_probe_publishes_health_logs_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
            runtime.backends["local-model"] = serve.OpenAICompatibleBackend(
                "local-model", self.model_url, "probe"
            )
            health_server, health_thread = self._start_runtime_server(runtime)
            probe_log = Path(tmp) / "probes.jsonl"
            calibration = {"local-model": CalibrationEntry(0.93, "a" * 64)}
            stop_event, probe_thread = serve.start_probe_thread(
                runtime,
                health_url=f"http://127.0.0.1:{health_server.server_port}/v1/health",
                calibration=calibration,
                interval_s=0.05,
                probe_log=probe_log,
            )
            try:
                self.assertTrue(self._wait_until(lambda: len(self.model_server.model_requests) >= 1))
                self.assertTrue(self._wait_until(lambda: runtime.router.cache.get("local-model") is not None))
                observation = runtime.router.cache.get("local-model")
                self.assertAlmostEqual(observation.quality_score, 0.93)
                self.assertGreaterEqual(observation.latency_ms, 0.0)
                self.assertTrue(self._wait_until(probe_log.exists))
                row = json.loads(probe_log.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(row["backend"], "local-model")
                self.assertTrue(row["published"])
            finally:
                stop_event.set()
                probe_thread.join(timeout=2)
                health_server.shutdown()
                health_server.server_close()
                health_thread.join(timeout=2)
            self.assertFalse(probe_thread.is_alive())

    def test_probe_without_calibration_does_not_publish_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
            runtime.backends["local-model"] = serve.OpenAICompatibleBackend(
                "local-model", self.model_url, "probe"
            )
            health_server, health_thread = self._start_runtime_server(runtime)
            stop_event, probe_thread = serve.start_probe_thread(
                runtime,
                health_url=f"http://127.0.0.1:{health_server.server_port}/v1/health",
                calibration={},
                interval_s=None,
                probe_log=None,
            )
            try:
                self.assertTrue(self._wait_until(lambda: len(self.model_server.model_requests) >= 1))
                probe_thread.join(timeout=2)
                self.assertIsNone(runtime.router.cache.get("local-model"))
            finally:
                stop_event.set()
                probe_thread.join(timeout=2)
                health_server.shutdown()
                health_server.server_close()
                health_thread.join(timeout=2)
            self.assertFalse(probe_thread.is_alive())

    def test_health_endpoint_accepts_optional_resource_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
            server, thread = self._start_runtime_server(runtime)
            url = f"http://127.0.0.1:{server.server_port}/v1/health"
            payload = {
                "backend": "local-model",
                "reachable": True,
                "latency_ms": 25,
                "quality_score": 0.92,
                "checked_at": time.time(),
                "resources": {
                    "queue_ms": 120,
                    "free_mem_mb": 2048,
                    "inflight": 2,
                    "source": "node-exporter",
                    "observed_at": time.time(),
                },
            }
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                observation = runtime.router.cache.get("local-model")
                self.assertEqual(observation.resources.queue_ms, 120)
                self.assertEqual(observation.resources.source, "node-exporter")

                payload.pop("resources")
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                self.assertIsNone(runtime.router.cache.get("local-model").resources)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_health_endpoint_rejects_invalid_optional_resource_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
            server, thread = self._start_runtime_server(runtime)
            url = f"http://127.0.0.1:{server.server_port}/v1/health"
            payload = {
                "backend": "local-model",
                "reachable": True,
                "latency_ms": 25,
                "quality_score": 0.92,
                "resources": {"queue_ms": -1, "observed_at": time.time()},
            }
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(request, timeout=2)
                self.assertEqual(caught.exception.code, 400)
                self.assertIsNone(runtime.router.cache.get("local-model"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_health_endpoint_converts_numeric_overflow_to_client_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = serve.build_runtime(Path(tmp) / "requests.jsonl")
            server, thread = self._start_runtime_server(runtime)
            url = f"http://127.0.0.1:{server.server_port}/v1/health"
            payload = {
                "backend": "local-model",
                "reachable": True,
                "latency_ms": 25,
                "quality_score": 0.92,
                "resources": {"queue_ms": 10**400, "observed_at": time.time()},
            }
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(request, timeout=2)
                self.assertEqual(caught.exception.code, 400)
                self.assertIsNone(runtime.router.cache.get("local-model"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
