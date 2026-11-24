import json
import hashlib
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.health_monitor import (
    CalibrationEntry,
    HealthMonitor,
    calibration_file_sha256,
    load_calibration,
)
from ai6g_runtime.models import OpenAICompatibleBackend


class _ProbeModelHandler(BaseHTTPRequestHandler):
    requests = []
    malformed = False
    unavailable = False

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.model_requests.append(body)
        if self.unavailable:
            self.send_error(503, "unavailable")
            return
        content = "not-json" if self.malformed else json.dumps(
            {"result_kind": "clarification", "actions": [], "explanation": "probe"},
            ensure_ascii=False,
        )
        response = {"model": "probe-model", "choices": [{"message": {"content": content}}]}
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass


class _HealthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.health_requests.append(body)
        encoded = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass


class HealthMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model_server = HTTPServer(("127.0.0.1", 0), _ProbeModelHandler)
        cls.model_server.model_requests = []
        cls.model_thread = threading.Thread(target=cls.model_server.serve_forever, daemon=True)
        cls.model_thread.start()
        cls.model_url = "http://127.0.0.1:%d/v1/chat/completions" % cls.model_server.server_port

        cls.health_server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        cls.health_server.health_requests = []
        cls.health_thread = threading.Thread(target=cls.health_server.serve_forever, daemon=True)
        cls.health_thread.start()
        cls.health_url = "http://127.0.0.1:%d/v1/health" % cls.health_server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.model_server.shutdown()
        cls.health_server.shutdown()
        cls.model_thread.join(timeout=2)
        cls.health_thread.join(timeout=2)

    def setUp(self):
        self.model_server.model_requests.clear()
        self.health_server.health_requests.clear()

    def _monitor(self, *, calibration=None, ttl_s=5, clock=lambda: 100.0, resource_provider=None):
        backend = OpenAICompatibleBackend("local-model", self.model_url, model="probe")
        return HealthMonitor(
            health_url=self.health_url,
            backends={"local-model": backend},
            calibration=calibration,
            timeout_s=2,
            ttl_s=ttl_s,
            clock=clock,
            resource_provider=resource_provider,
        )

    def test_success_probe_calls_real_model_and_publishes_calibrated_health(self):
        calibration = {"local-model": CalibrationEntry(0.91, "a" * 64)}
        monitor = self._monitor(calibration=calibration)
        result = monitor.run_once()[0]
        self.assertTrue(result.reachable)
        self.assertTrue(result.model_real)
        self.assertTrue(result.published)
        self.assertEqual(result.quality_score, 0.91)
        self.assertEqual(len(self.model_server.model_requests), 1)
        self.assertIn("HEALTH_PROBE", self.model_server.model_requests[0]["messages"][1]["content"])
        self.assertEqual(len(self.health_server.health_requests), 1)
        body = self.health_server.health_requests[0]
        self.assertTrue(body["reachable"])
        self.assertEqual(body["quality_score"], 0.91)
        self.assertEqual(body["checked_at"], 100.0)
        self.assertGreaterEqual(body["latency_ms"], 0.0)
        self.assertIn("a" * 64, body["detail"])

    def test_resource_provider_publishes_timestamped_backend_metrics(self):
        resource = {
            "queue_ms": 37.5,
            "free_mem_mb": 4096,
            "inflight": 2,
            "source": "test-provider",
            "observed_at": 100.0,
        }
        monitor = self._monitor(
            calibration={"local-model": CalibrationEntry(0.91, "e" * 64)},
            resource_provider=lambda backend: resource if backend == "local-model" else None,
        )

        report = monitor.run_once()[0]

        self.assertTrue(report.published)
        self.assertEqual(report.resources, resource)
        self.assertEqual(self.health_server.health_requests[0]["resources"], resource)

    def test_invalid_resource_provider_data_does_not_poison_model_health(self):
        monitor = self._monitor(
            calibration={"local-model": CalibrationEntry(0.91, "f" * 64)},
            resource_provider=lambda _backend: {
                "queue_ms": -1,
                "source": "invalid-provider",
                "observed_at": 100.0,
            },
        )

        report = monitor.run_once()[0]

        self.assertTrue(report.reachable)
        self.assertTrue(report.published)
        self.assertIsNone(report.resources)
        self.assertIsNotNone(report.resource_error)
        self.assertNotIn("resources", self.health_server.health_requests[0])

    def test_failed_probe_publishes_unreachable_with_calibration(self):
        _ProbeModelHandler.malformed = True
        try:
            monitor = self._monitor(calibration={"local-model": CalibrationEntry(0.91, "b" * 64)})
            result = monitor.run_once()[0]
            self.assertFalse(result.reachable)
            self.assertFalse(result.model_real)
            self.assertTrue(result.published)
            body = self.health_server.health_requests[0]
            self.assertFalse(body["reachable"])
            self.assertEqual(body["error_rate"], 1.0)
            self.assertEqual(body["quality_score"], 0.91)
        finally:
            _ProbeModelHandler.malformed = False

    def test_missing_calibration_does_not_publish_fabricated_quality(self):
        monitor = self._monitor(calibration=None)
        result = monitor.run_once()[0]
        self.assertTrue(result.reachable)
        self.assertFalse(result.published)
        self.assertIsNone(result.quality_score)
        self.assertIn("calibration", result.detail.lower())
        self.assertEqual(self.health_server.health_requests, [])

    def test_ttl_suppresses_early_probe_and_reprobes_after_expiry(self):
        now = [100.0]
        monitor = self._monitor(
            calibration={"local-model": CalibrationEntry(0.9, "c" * 64)},
            ttl_s=5,
            clock=lambda: now[0],
        )
        self.assertEqual(len(monitor.run_due()), 1)
        now[0] = 103.0
        skipped = monitor.run_due()
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].skipped)
        self.assertEqual(len(self.model_server.model_requests), 1)
        now[0] = 105.1
        reprobe = monitor.run_due()
        self.assertFalse(reprobe[0].skipped)
        self.assertEqual(len(self.model_server.model_requests), 2)

    def test_probe_backend_runs_one_named_backend_immediately(self):
        monitor = self._monitor(calibration={"local-model": CalibrationEntry(0.9, "d" * 64)})
        report = monitor.probe_backend("local-model")
        self.assertEqual(report.backend, "local-model")
        self.assertFalse(report.skipped)
        with self.assertRaises(KeyError):
            monitor.probe_backend("missing-model")

    def test_calibration_requires_score_and_source_sha256(self):
        path = Path(self.id().replace(".", "_") + ".json")
        try:
            path.write_text(json.dumps({"local-model": {"quality_score": 0.9}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_calibration(path)
        finally:
            path.unlink(missing_ok=True)

    def test_calibration_file_hash_is_byte_hash(self):
        path = Path(self.id().replace(".", "_") + ".json")
        data = b'{"backends":{}}\n'
        try:
            path.write_bytes(data)
            self.assertEqual(calibration_file_sha256(path), hashlib.sha256(data).hexdigest())
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
