"""Browser-compatible JSON at the real Runtime HTTP boundary."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai6g_runtime import serve


def browser_json(raw):
    def reject(value):
        raise AssertionError(f"Browser rejects non-standard JSON constant: {value}")
    return json.loads(raw, parse_constant=reject)


class JsonHttpTests(unittest.TestCase):
    def request(self, handler, path, payload=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            req = Request(f"http://127.0.0.1:{server.server_port}{path}",
                          data=None if payload is None else json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
            try:
                response = build_opener(ProxyHandler({})).open(req, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, browser_json(response.read())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_unavailable_route_is_browser_json_and_keeps_failure(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            runtime = serve.build_runtime(Path(td) / "requests.jsonl")
            handler = type("BoundHandler", (serve.RuntimeHandler,), {"runtime": runtime})
            status, body = self.request(handler, "/v1/tasks", {"text": "开灯", "request_id": "browser-1"})
            self.assertEqual(status, 422)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error_code"], "route_unavailable")
            self.assertEqual(body["route"]["backend"], "unavailable")
            self.assertIsNone(body["route"]["estimated_latency_ms"])
            self.assertFalse(body["execution"]["changed"])
            _, logs = self.request(handler, "/v1/logs")
            self.assertIsNone(logs[0]["route"]["estimated_latency_ms"])

    def test_nested_nonfinite_http_numbers_become_null(self):
        class Handler(serve.RuntimeHandler):
            def do_GET(self):
                self._send(200, {"nested": [{"values": (float("nan"), float("inf"), -float("inf"), 1.25)}],
                                 "label": "Infinity is text", "enabled": True})
        status, body = self.request(Handler, "/nested")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"nested": [{"values": [None, None, None, 1.25]}],
                                "label": "Infinity is text", "enabled": True})


if __name__ == "__main__":
    unittest.main()
