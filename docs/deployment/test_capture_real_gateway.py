"""Local HTTP fixture tests; these do not produce real-model evidence."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs/deployment/capture_real_gateway.py"


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.unsafe = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_json({"ready": True, "source": "test-fixture",
                                "configured_model": "local-http-fixture", "inflight": 0})

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                command = request["command"]
                owner.calls.append(command)
                actions = []
                if "有点暗" in command:
                    actions = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]
                    if owner.unsafe:
                        actions = [{"name": "unlock_door", "params": {"location": "客厅"}}]
                elif "并" in command:
                    actions = [{"name": "light_control", "params": {"location": "客厅", "action": "on"}},
                               {"name": "curtain_control", "params": {"location": "客厅", "action": "close"}}]
                self.send_json({"answer": "local HTTP fixture, not model inference", "actions": actions,
                                "semantic_complete": True, "source": "real-model", "model_ms": 1.25,
                                "configured_model": "local-http-fixture", "model_revision": "fixture-test-only"})

            def send_json(self, value):
                raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "capture"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_capture(self, tier="gateway", extra=()):
        result = subprocess.run([sys.executable, str(SCRIPT), "--url", self.url, "--tier", tier,
                                 "--out", str(self.out), *extra], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30)
        report_path = self.out / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
        return result, report

    def test_gateway_executes_actual_c_policy_and_isolated_json_state(self):
        # C-policy bypass, missing executor, or writing normal state must fail.
        normal = ROOT / "openvela_smart_home/device_state.json"
        before = normal.read_bytes() if normal.exists() else None
        result, report = self.run_capture(extra=("--repeat", "2"))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(len(report["rows"]), 12)
        rows = report["rows"][:6]
        self.assertEqual(rows[0]["state_after"]["lights"]["客厅"], {"is_on": True, "brightness": 100})
        self.assertEqual(rows[2]["state_after"]["lights"]["客厅"]["brightness"], 60)
        self.assertEqual(rows[2]["gateway_event"]["backend"], "local-model")
        self.assertEqual(rows[3]["state_after"]["curtains"]["客厅"]["position"], 0)
        self.assertEqual(rows[4]["source_class"], "rules")
        self.assertFalse(rows[4]["gateway_event"]["semantic_complete"])
        self.assertEqual(rows[5]["state_before"], rows[5]["state_after"])
        self.assertEqual(rows[2]["source_class"], "real-model")
        self.assertFalse(rows[2]["gateway_event"]["link"]["wan_ok"])
        self.assertGreaterEqual(rows[2]["gateway_event"]["link"]["lan_rtt_ms"], 0)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(normal.read_bytes() if normal.exists() else None, before)
        self.assertTrue(report["normal_state_unchanged"])
        self.assertEqual(report["meta"]["network_scope"], "controlled route inputs; not physical WAN/LAN or outage evidence")
        self.assertEqual(len(report["source_sha256"]["policy_cli"]), 64)

    def test_unsafe_model_action_is_preserved_before_gateway_fallback(self):
        # Dropping rejected upstream actions would hide the causal failure.
        self.unsafe = True
        result, report = self.run_capture()
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(report, result.stderr + result.stdout)
        self.assertEqual(len(report["rows"]), 6)
        row = report["rows"][2]
        self.assertEqual(row["gateway_event"]["error"], "UNSAFE_ACTION_REJECTED")
        self.assertEqual(row["source_class"], "fallback")
        self.assertEqual(row["backend_attempts"][0]["result"]["actions"][0]["name"], "unlock_door")
        self.assertEqual(len(report["rows"][-1]["gateway_event"]["event_id"]), 32)
        self.assertTrue((self.out / "events.jsonl").is_file())

    def test_cloud_unknown_lan_stays_unknown_and_injection_is_explicit(self):
        # Guessing LAN true would incorrectly turn an unknown path into evidence.
        result, report = self.run_capture(tier="cloud")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(report, result.stderr + result.stdout)
        self.assertIsNone(report["rows"][2]["gateway_event"]["link"]["lan_ok"])
        self.assertEqual(report["rows"][2]["gateway_event"]["reason"], "LAN_DOWN")
        self.assertEqual(self.calls, [])
        self.out = Path(self.temp.name) / "cloud-injected"
        result, report = self.run_capture(tier="cloud", extra=("--inject-lan-ok",))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(report["rows"][2]["gateway_event"]["backend"], "cloud-model")
        self.assertEqual(report["meta"]["link_field_sources"]["lan_ok"], "injected_route_fixture")
        self.assertIsNone(report["rows"][2]["gateway_event"]["link"]["lan_rtt_ms"])

    def test_existing_directory_is_never_overwritten(self):
        self.out.mkdir()
        marker = self.out / "report.json"
        marker.write_text('{"keep":true}', encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--url", self.url, "--tier", "gateway",
                                 "--out", str(self.out)], cwd=ROOT, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"output_directory_exists", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"keep":true}')
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
