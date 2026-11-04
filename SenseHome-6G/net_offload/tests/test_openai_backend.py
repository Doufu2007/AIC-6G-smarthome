import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from backends.openai_backend import BackendConfig, create_server  # noqa: E402


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), None))
        self.reply({"data": [{"id": "test-model"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, dict(self.headers), payload))
        mode = self.server.mode
        if mode == "timeout":
            time.sleep(0.2)
        if mode == "http-error":
            return self.reply({"error": "secret must never be echoed"}, 500)
        actions = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]
        if mode == "unsafe":
            actions = [{"name": "unlock_door", "params": {"location": "客厅"}}]
        if mode == "invalid-params":
            actions[0]["params"]["brightness"] = 101
        if mode == "custom-actions":
            actions = self.server.actions
        answer = "我已经打开了客厅的灯。" if mode == "premature-success" else "灯光调至60%"
        content = json.dumps({"answer": answer, "actions": actions}, ensure_ascii=False)
        if mode == "malformed":
            content = "```json\nnot json\n```"
        self.reply({"model": "test-model-real-id", "choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}})


class OpenAIBackendTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        self.upstream.mode = "good"
        self.upstream.requests = []
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.wrapper = create_server(
            BackendConfig(
                upstream_url=f"http://127.0.0.1:{self.upstream.server_port}/v1",
                model="test-model", api_key="local-test-secret", timeout_s=0.05,
                gpu_info="fake-test-gpu", host="127.0.0.1", port=0,
            )
        )
        self.wrapper_thread = threading.Thread(target=self.wrapper.serve_forever, daemon=True)
        self.wrapper_thread.start()

    def tearDown(self):
        for server, thread in ((self.wrapper, self.wrapper_thread), (self.upstream, self.upstream_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.wrapper.server_port, timeout=2)
        try:
            conn.request(method, path, body=json.dumps(payload).encode("utf-8") if payload is not None else None,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_wrapper_calls_openai_and_returns_validated_actions_with_provenance(self):
        status, result = self.request("POST", "/infer", {"command": "客厅有点暗"})

        self.assertEqual(status, 200)
        self.assertEqual(result["actions"][0]["name"], "light_control")
        self.assertEqual(result["source"], "real-model")
        self.assertEqual(result["upstream_model"], "test-model-real-id")
        self.assertEqual(result["gpu_info_source"], "environment_declared")
        self.assertEqual(result["gpu_info"], "fake-test-gpu")
        self.assertGreaterEqual(result["model_ms"], 0)
        self.assertEqual(result["model_ms_kind"], "upstream_http_wall_clock")
        self.assertFalse(result["semantic_complete"], "a model plan is not an executed device action")
        path, headers, payload = self.upstream.requests[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer local-test-secret")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("local-test-secret", json.dumps(result))

    def test_system_prompt_has_explicit_examples_for_common_chinese_intents(self):
        self.request("POST", "/infer", {"command": "关闭卧室窗帘"})
        payload = self.upstream.requests[-1][2]
        system = payload["messages"][0]["content"]
        self.assertIn('"action":"close"', system)
        self.assertIn('"name":"read_temperature"', system)
        self.assertIn('"brightness":30', system)
        self.assertIn("打开书房窗帘", system)

    def test_bad_model_json_and_forbidden_or_invalid_actions_are_rejected(self):
        for mode, expected in (("malformed", "INVALID_MODEL_JSON"), ("unsafe", "INVALID_MODEL_ACTIONS"), ("invalid-params", "INVALID_MODEL_ACTIONS")):
            with self.subTest(mode=mode):
                self.upstream.mode = mode
                status, result = self.request("POST", "/infer", {"command": "客厅有点暗"})
                self.assertGreaterEqual(status, 400)
                self.assertEqual(result["error"], expected)
                self.assertEqual(result["actions"], [])

    def test_guessed_room_without_explicit_target_is_rejected(self):
        status, result = self.request("POST", "/infer", {
            "command": "把灯调亮一点", "context": {"location": "客厅"},
        })
        self.assertEqual(status, 422)
        self.assertEqual(result["error"], "MODEL_ACTION_TARGET_UNMENTIONED")
        self.assertEqual(result["actions"], [])
        self.assertFalse(result["semantic_complete"])

    def test_extra_unmentioned_room_rejects_the_entire_action_batch(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = [
            {"name": "light_control", "params": {"location": "客厅", "action": "on"}},
            {"name": "curtain_control", "params": {"location": "卧室", "action": "open"}},
        ]
        status, result = self.request("POST", "/infer", {"command": "打开客厅灯"})
        self.assertEqual(status, 422)
        self.assertEqual(result["error"], "MODEL_ACTION_TARGET_UNMENTIONED")
        self.assertEqual(result["actions"], [])

    def test_explicit_multi_room_plan_is_accepted(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = [
            {"name": "light_control", "params": {"location": "客厅", "action": "on"}},
            {"name": "curtain_control", "params": {"location": "卧室", "action": "open"}},
        ]
        status, result = self.request("POST", "/infer", {"command": "分别打开客厅灯和卧室窗帘"})
        self.assertEqual(status, 200)
        self.assertEqual(len(result["actions"]), 2)

    def test_negated_mutations_require_clarification(self):
        for command in ("不要打开客厅灯", "客厅灯别关", "禁止调整客厅灯", "不用关闭客厅灯"):
            with self.subTest(command=command):
                status, result = self.request("POST", "/infer", {"command": command})
                self.assertEqual(status, 422)
                self.assertEqual(result["error"], "MODEL_ACTION_REQUIRES_CLARIFICATION")
                self.assertEqual(result["actions"], [])

    def test_conditional_mutations_require_clarification(self):
        for command in ("如果客厅温度超过28度就打开灯", "除非很暗，否则不要打开客厅灯", "客厅温度超过28度就开灯"):
            with self.subTest(command=command):
                status, result = self.request("POST", "/infer", {"command": command})
                self.assertEqual(status, 422)
                self.assertEqual(result["error"], "MODEL_ACTION_REQUIRES_CLARIFICATION")
                self.assertEqual(result["actions"], [])

    def test_read_only_plan_remains_available_with_negation_or_condition(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = [{"name": "read_temperature", "params": {"location": "客厅"}}]
        for command in ("不要打开客厅灯，只查看温度", "如果方便，查看客厅温度"):
            with self.subTest(command=command):
                status, result = self.request("POST", "/infer", {"command": command})
                self.assertEqual(status, 200)
                self.assertEqual(result["actions"], [{"name": "read_temperature", "params": {"location": "客厅"}}])

    def test_empty_plan_remains_valid_when_clarification_is_needed(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = []
        status, result = self.request("POST", "/infer", {"command": "把灯调亮一点"})
        self.assertEqual(status, 200)
        self.assertEqual(result["actions"], [])
        self.assertFalse(result["semantic_complete"])

    def test_high_confidence_empty_plan_is_normalized_with_provenance(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = []
        cases = (
            ("卧室有点亮", [{"name": "light_control", "params": {"location": "卧室", "action": "set", "brightness": 30}}]),
            ("浴室现在温度是多少", [{"name": "read_temperature", "params": {"location": "浴室"}}]),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                status, result = self.request("POST", "/infer", {"command": command})
                self.assertEqual(status, 200)
                self.assertEqual(result["actions"], expected)
                self.assertEqual(result["raw_model_actions"], [])
                self.assertEqual(result["action_normalization"]["source"], "high_confidence_command_semantics")

    def test_ambiguous_command_is_not_normalized(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = []
        status, result = self.request("POST", "/infer", {"command": "卧室窗帘调整一下"})
        self.assertEqual(status, 200)
        self.assertEqual(result["actions"], [])
        self.assertNotIn("action_normalization", result)

    def test_ambiguous_curtain_action_is_removed_and_marked_for_clarification(self):
        self.upstream.mode = "custom-actions"
        self.upstream.actions = [{"name": "curtain_control", "params": {"location": "卧室", "action": "set", "position": 50}}]
        status, result = self.request("POST", "/infer", {"command": "卧室窗帘调整一下"})
        self.assertEqual(status, 200)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["raw_model_actions"], self.upstream.actions)
        self.assertEqual(result["action_normalization"]["rule"], "ambiguous_curtain_requires_clarification")

    def test_model_cannot_announce_execution_before_gateway_confirmation(self):
        self.upstream.mode = "premature-success"
        status, result = self.request("POST", "/infer", {"command": "打开客厅的灯"})
        self.assertEqual(status, 200)
        self.assertEqual(result["answer"], "动作计划已生成，等待网关执行确认。")
        self.assertNotIn("我已经打开", json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["semantic_complete"])

    def test_upstream_http_error_is_sanitized(self):
        self.upstream.mode = "http-error"
        status, result = self.request("POST", "/infer", {"command": "客厅有点暗"})
        self.assertEqual(status, 502)
        self.assertEqual(result["error"], "UPSTREAM_HTTP_500")
        self.assertNotIn("secret", json.dumps(result))

    def test_upstream_timeout_is_bounded(self):
        self.upstream.mode = "timeout"
        start = time.perf_counter()
        status, result = self.request("POST", "/infer", {"command": "客厅有点暗"})
        self.assertEqual(status, 504)
        self.assertEqual(result["error"], "UPSTREAM_TIMEOUT")
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_health_checks_model_inventory_and_probe_does_not_infer(self):
        status, result = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(result["ready"])
        self.assertEqual(result["configured_model"], "test-model")
        count = len(self.upstream.requests)
        status, result = self.request("GET", "/probe")
        self.assertEqual(status, 200)
        self.assertTrue(result["probe"])
        self.assertEqual(len(self.upstream.requests), count)


if __name__ == "__main__":
    unittest.main()
