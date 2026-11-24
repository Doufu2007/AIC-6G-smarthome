import sys
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.models import ModelResult, OpenAICompatibleBackend, RuleBackend
from ai6g_runtime.protocol import classify_task
from ai6g_runtime.router import HealthObservation, ObservationCache, TaskRouter


class _ModelHandler(BaseHTTPRequestHandler):
    payload = {"actions": [{"type": "light_on", "location": "客厅", "brightness": 70}]}
    malformed = False

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.seen = body
        content = "not-json" if self.malformed else json.dumps(self.payload, ensure_ascii=False)
        response = {
            "model": "demo-server",
            "system_fingerprint": "fp-test",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "choices": [{"message": {"content": content}}],
        }
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass


class ModelAndRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _ModelHandler)
        cls.server.seen = None
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/v1/chat/completions" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def test_openai_compatible_backend_parses_action_json(self):
        backend = OpenAICompatibleBackend("cloud-model", self.url, model="demo")
        task = SimpleNamespace(text="打开客厅的灯")
        result = backend.infer(task, timeout_s=2)
        self.assertTrue(result.ok)
        self.assertTrue(result.model_real)
        self.assertEqual(result.payload["actions"][0]["type"], "light_on")
        self.assertEqual(self.server.seen["model"], "demo")

    def test_request_has_strict_schema_examples_and_reproducible_generation(self):
        backend = OpenAICompatibleBackend("cloud-model", self.url, model="demo")
        backend.infer(SimpleNamespace(text="请规划一个客厅灯光动作"), timeout_s=2)
        body = self.server.seen
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 384)
        self.assertEqual(body["seed"], 20261103)
        prompt = body["messages"][0]["content"]
        self.assertIn('"result_kind":"action"', prompt)
        self.assertIn('"type":"light_on"', prompt)
        self.assertIn('"location":"客厅"', prompt)
        self.assertIn('"brightness":100', prompt)
        self.assertIn('"result_kind":"clarification"', prompt)
        self.assertIn('"result_kind":"refusal"', prompt)
        self.assertIn('"actions":[]', prompt)
        self.assertNotIn('"clarification" or "refusal"', prompt)

    def test_invalid_content_preserves_raw_model_response_metadata(self):
        _ModelHandler.malformed = True
        try:
            backend = OpenAICompatibleBackend("cloud-model", self.url, model="demo")
            result = backend.infer(SimpleNamespace(text="打开客厅的灯"), timeout_s=2)
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.raw_response)
            self.assertEqual(result.raw_response["model"], "demo-server")
            self.assertEqual(result.raw_response["system_fingerprint"], "fp-test")
            self.assertEqual(result.raw_response["usage"]["completion_tokens"], 7)
        finally:
            _ModelHandler.malformed = False

    def test_openai_compatible_backend_rejects_malformed_content(self):
        _ModelHandler.malformed = True
        try:
            backend = OpenAICompatibleBackend("cloud-model", self.url, model="demo")
            result = backend.infer(SimpleNamespace(text="打开客厅的灯"), timeout_s=2)
            self.assertFalse(result.ok)
            self.assertFalse(result.model_real)
            self.assertIn("json", result.error.lower())
        finally:
            _ModelHandler.malformed = False

    def test_rule_backend_is_explicitly_non_real(self):
        result = RuleBackend().infer(SimpleNamespace(text="打开客厅的灯", intent="light_on", location="客厅"), 1)
        self.assertTrue(result.ok)
        self.assertFalse(result.model_real)
        self.assertEqual(result.payload["actions"][0]["location"], "客厅")

    def test_rule_backend_does_not_attach_brightness_to_light_off(self):
        task = SimpleNamespace(text="\u5173\u95ed\u5ba2\u5385\u7684\u706f", intent="light_off", location="\u5ba2\u5385")
        result = RuleBackend().infer(task, 1)
        self.assertTrue(result.ok)
        self.assertNotIn("brightness", result.payload["actions"][0])
    def test_rule_backend_preserves_requested_brightness(self):
        task = SimpleNamespace(text="\u628a\u4e66\u623f\u706f\u4eae\u5ea6\u8c03\u523060", intent="set_brightness", location="\u4e66\u623f")
        result = RuleBackend().infer(task, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["actions"][0]["type"], "set_brightness")
        self.assertEqual(result.payload["actions"][0]["brightness"], 60)

    def test_rule_backend_refuses_dangerous_hazard_action(self):
        task = classify_task("发生烟雾报警，请锁上客厅门")
        result = RuleBackend().infer(task, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["result_kind"], "refusal")
        self.assertEqual(result.payload["actions"], [])
        self.assertTrue(result.payload["explanation"])

    def test_router_sends_safety_to_local_rule_clarification(self):
        task = classify_task("发生烟雾报警，请锁上客厅门")
        choice = TaskRouter(ObservationCache()).choose(task, now=101.0)
        self.assertEqual(choice.backend, "local-rules")
        self.assertIn("SAFETY", choice.reason)

    def test_default_prompt_declares_complete_action_protocol(self):
        backend = OpenAICompatibleBackend("cloud-model", self.url, model="demo")
        for action in ("light_on", "light_off", "set_brightness", "curtain_open",
                       "curtain_close", "security_lock", "security_unlock"):
            self.assertIn(action, backend.system_prompt)
        for location in ("客厅", "卧室", "厨房", "浴室", "书房", "室外"):
            self.assertIn(location, backend.system_prompt)
        self.assertIn("clarification", backend.system_prompt)
        self.assertIn("refusal", backend.system_prompt)
    def test_router_marks_expired_observation_and_uses_deadline(self):
        cache = ObservationCache(ttl_s=5)
        cache.put(HealthObservation("local-model", True, 120, 0.78, checked_at=100.0))
        cache.put(HealthObservation("cloud-model", True, 420, 0.95, checked_at=100.0))
        router = TaskRouter(cache)
        task = SimpleNamespace(task_class="complex", deadline_ms=200, quality_required=0.7, privacy="standard")
        choice = router.choose(task, now=102.0)
        self.assertEqual(choice.backend, "local-model")
        self.assertFalse(choice.refresh_required)
        stale = router.choose(task, now=106.0)
        self.assertTrue(stale.refresh_required)
        self.assertIn("stale", stale.reason.lower())

    def test_router_honors_task_quality_floor(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(HealthObservation("local-model", True, 80, 0.82, checked_at=100.0))
        cache.put(HealthObservation("cloud-model", True, 220, 0.94, checked_at=100.0))
        router = TaskRouter(cache)
        task = SimpleNamespace(task_class="complex", deadline_ms=500, quality_floor=0.9, privacy="standard")
        choice = router.choose(task, now=101.0)
        self.assertEqual(choice.backend, "cloud-model")
    def test_router_does_not_rank_stale_latency_as_fresh(self):
        cache = ObservationCache(ttl_s=5)
        cache.put(HealthObservation("local-model", True, 20, 0.95, checked_at=100.0))
        cache.put(HealthObservation("cloud-model", True, 180, 0.95, checked_at=108.0))
        router = TaskRouter(cache)
        task = SimpleNamespace(task_class="complex", deadline_ms=500, quality_floor=0.9, privacy="standard")
        choice = router.choose(task, now=110.0)
        self.assertEqual(choice.backend, "cloud-model")
        self.assertTrue(choice.refresh_required)
    def test_router_prefers_cloud_when_local_would_miss_deadline(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(HealthObservation("local-model", True, 500, 0.9, checked_at=100.0))
        cache.put(HealthObservation("cloud-model", True, 180, 0.95, checked_at=100.0))
        router = TaskRouter(cache)
        task = SimpleNamespace(task_class="complex", deadline_ms=250, quality_required=0.9, privacy="standard")
        choice = router.choose(task, now=101.0)
        self.assertEqual(choice.backend, "cloud-model")
        self.assertLessEqual(choice.estimated_latency_ms, 250)

    def test_router_keeps_strict_privacy_off_cloud(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(HealthObservation("cloud-model", True, 80, 0.99, checked_at=100.0))
        router = TaskRouter(cache)
        task = classify_task("不要上传任何信息，帮我把客厅灯打开")
        self.assertEqual(task.privacy, "strict")
        choice = router.choose(task, now=101.0)
        self.assertEqual(choice.backend, "local-rules")
        self.assertNotEqual(choice.backend, "cloud-model")

    def test_strict_privacy_ambiguous_task_uses_local_model_only(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(HealthObservation("cloud-model", True, 80, 0.99, checked_at=100.0))
        task = classify_task("不要上传任何信息，帮我把客厅灯调到合适亮度")
        choice = TaskRouter(cache).choose(task, now=101.0)
        self.assertEqual(choice.backend, "unavailable")
        self.assertNotEqual(choice.backend, "cloud-model")

    def test_router_sends_ambiguous_and_multi_action_simple_text_to_model(self):
        cache = ObservationCache(ttl_s=30)
        cache.put(HealthObservation("local-model", True, 90, 0.95, checked_at=100.0))
        router = TaskRouter(cache)
        ambiguous = router.choose(classify_task("帮我把书房灯调到舒服的亮度"), now=101.0)
        multi = router.choose(classify_task("关闭客厅和卧室的灯"), now=101.0)
        self.assertEqual(ambiguous.backend, "local-model")
        self.assertEqual(multi.backend, "local-model")

    def test_rule_action_eligibility_excludes_dry_run(self):
        router = TaskRouter(ObservationCache(ttl_s=30))
        explicit = router.choose(classify_task("打开客厅的灯"), now=101.0)
        dry_run = classify_task("打开客厅的灯，不要真的执行")
        self.assertEqual(explicit.backend, "local-rules")
        self.assertFalse(dry_run.rule_eligible)
        result = RuleBackend().infer(dry_run, 1)
        self.assertEqual(result.payload["result_kind"], "clarification")
        self.assertEqual(result.payload["actions"], [])

    def test_router_returns_unavailable_for_all_stale_backends(self):
        cache = ObservationCache(ttl_s=5)
        cache.put(HealthObservation("local-model", True, 20, 0.95, checked_at=100.0))
        cache.put(HealthObservation("cloud-model", True, 80, 0.99, checked_at=100.0))
        choice = TaskRouter(cache).choose(classify_task("有点暗，帮我调亮客厅"), now=110.0)
        self.assertEqual(choice.backend, "unavailable")
        self.assertTrue(choice.refresh_required)


if __name__ == "__main__":
    unittest.main()




