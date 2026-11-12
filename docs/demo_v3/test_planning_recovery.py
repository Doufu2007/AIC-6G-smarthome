"""Planning recovery contract tests; HTTP fixtures are NOT real-model evidence."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import http.server
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "SenseHome-6G/net_offload"))
from offload_gateway import BackendResult, HttpJsonBackend, LinkSnapshot, OffloadGateway  # noqa: E402

COMMAND = "关闭书房的灯并打开书房窗帘"
ACTION = {"name": "light_control", "params": {"location": "书房", "action": "off"}}
COMPLETE_ACTIONS = [ACTION, {'name': 'curtain_control', 'params': {'location': '书房', 'action': 'open'}}]


def load_implementation(test):
    path = Path(__file__).with_name("planning_recovery.py")
    test.assertTrue(path.is_file(), "Independent planning recovery adapter is not implemented")
    name = "planning_recovery_contract_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def failed(error="HTTP_503", **kwargs):
    return BackendResult(False, False, {}, [], error=error, source="fixture-primary", **kwargs)


def succeeded():
    return BackendResult(True, True, {"answer": "fixture plan", "actions": [deepcopy(ACTION)]},
                         [deepcopy(ACTION)], model_ms=2, source="fixture-secondary")


class BackendFixture:
    """Small deterministic planner boundary; never executes device actions."""
    def __init__(self, result, url, *, health=None, mutate=False, hook=None):
        self.result = result
        self.base_url = url
        self.health_value = {"ready": True, "reachable": True} if health is None else health
        self.calls = []
        self.health_calls = 0
        self.mutate = mutate
        self.hook = hook

    def health(self):
        self.health_calls += 1
        if isinstance(self.health_value, Exception):
            raise self.health_value
        return deepcopy(self.health_value)

    def infer(self, command, context=None):
        self.calls.append((command, deepcopy(context)))
        if self.hook:
            self.hook()
        if self.mutate and context is not None:
            context["devices"]["客厅"]["brightness"] = 99
            context["recovery"] = {"route": "injected"}
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ManualClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


@contextmanager
def http_fixture(*, infer_status=200, infer_body=None, health=None, redirect=None):
    """A real loopback HTTP server with literal, hand-authored protocol data."""
    calls = []
    if infer_body is None:
        infer_body = {"semantic_complete": True, "actions": [ACTION], "source": "http-fixture"}
    if health is None:
        health = {"ready": True}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if redirect:
                self.send_header("Location", redirect)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            calls.append(("GET", self.path, None))
            self.reply(200, health)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, payload))
            self.reply(infer_status, infer_body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class PlanningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cls = load_implementation(self).PlanningRecoveryBackend

    def wrap(self, primary=None, secondary=None, **kwargs):
        self.primary = primary or BackendFixture(failed(), "http://127.0.0.1:18101")
        self.secondary = secondary or BackendFixture(succeeded(), "http://127.0.0.1:18102")
        return self.cls(self.primary, self.secondary, primary_name="cloud-model",
                        secondary_name="local-model", primary_url=self.primary.base_url,
                        secondary_url=self.secondary.base_url, **kwargs)

    def test_primary_success_never_launches_second_planner(self):
        wrapper = self.wrap(primary=BackendFixture(succeeded(), "http://127.0.0.1:18101"))
        result = wrapper.infer(COMMAND)
        self.assertTrue(result.ok)
        self.assertEqual(result.actions, [ACTION])
        self.assertEqual(self.secondary.calls, [])
        self.assertEqual(self.secondary.health_calls, 0)
        self.assertEqual(result.response["recovery"]["actual_backend"], "cloud-model")
        self.assertFalse(result.response["recovery"]["attempted"])

    def test_explicit_transport_failures_recover_with_visible_primary_failure(self):
        for error in ("HTTP_503", "HTTP_504", "TimeoutError", "ConnectionRefusedError", "ConnectionResetError"):
            with self.subTest(error=error):
                original = failed(error)
                wrapper = self.wrap(primary=BackendFixture(original, "http://127.0.0.1:18101"))
                result = wrapper.infer(COMMAND)
                self.assertTrue(result.ok)
                self.assertIsNone(result.error)
                self.assertEqual(len(self.secondary.calls), 1)
                self.assertEqual(self.secondary.health_calls, 1)
                meta = result.response["recovery"]
                self.assertEqual(meta["primary_error"], error)
                self.assertEqual(meta["actual_backend"], "local-model")
                self.assertEqual(meta["from"], "cloud-model")
                self.assertEqual(meta["to"], "local-model")
                self.assertFalse(meta["primary_status"]["ok"])
                self.assertTrue(meta["secondary_status"]["ok"])
                self.assertIn("local-model", result.source)
                self.assertEqual(original.response, {})
                self.assertEqual(original.error, error)

    def test_only_explicit_nested_502_transport_codes_allow_recovery(self):
        for nested, want in (("UPSTREAM_HTTP_503", True), ("UPSTREAM_HTTP_504", True),
                             ("UPSTREAM_TimeoutError", True), ("UPSTREAM_JSONDecodeError", False),
                             ("UPSTREAM_INVALID_JSON_OBJECT", False), ("UPSTREAM_HTTP_502", False),
                             ("UPSTREAM_HTTP_429", False), ("upstream unavailable", False)):
            with self.subTest(nested=nested):
                result = failed("HTTP_502")
                result.response = {"error": nested}
                wrapper = self.wrap(primary=BackendFixture(result, "http://127.0.0.1:18101"))
                out = wrapper.infer(COMMAND)
                self.assertEqual(out.ok, want)
                self.assertEqual(len(self.secondary.calls), int(want))

    def test_terminal_refusal_invalid_unsafe_clarification_and_redirect_do_not_recover(self):
        cases = [failed(error) for error in ("HTTP_400", "HTTP_401", "HTTP_403", "HTTP_429",
                 "HTTP_502", "JSONDecodeError", "INVALID_JSON_OBJECT", "INVALID_ACTIONS",
                 "UNSAFE_ACTION_REJECTED", "CLARIFICATION_REQUIRED", "ACTION_NOT_ALLOWED", "HTTP_307")]
        cases += [failed("HTTP_503", failure_kind="model_refusal"),
                  failed("HTTP_503", failure_kind="clarification"),
                  failed("HTTP_503", failure_kind="unsafe_action")]
        contradictory = failed("HTTP_503")
        contradictory.response = {"needs_clarification": True}
        cases.append(contradictory)
        for primary in cases:
            with self.subTest(error=primary.error, failure_kind=primary.failure_kind, response=primary.response):
                wrapper = self.wrap(primary=BackendFixture(primary, "http://127.0.0.1:18101"))
                result = wrapper.infer(COMMAND)
                self.assertFalse(result.ok)
                self.assertEqual(result.error, primary.error)
                self.assertEqual(self.secondary.calls, [])
                self.assertEqual(self.secondary.health_calls, 0)

    def test_any_existing_action_or_completion_stops_second_plan(self):
        for actions, complete in (([ACTION], False), ([], True)):
            with self.subTest(actions=actions, complete=complete):
                primary = failed()
                primary.actions, primary.semantic_complete = actions, complete
                wrapper = self.wrap(primary=BackendFixture(primary, "http://127.0.0.1:18101"))
                out = wrapper.infer(COMMAND)
                self.assertEqual(out.actions, actions)
                self.assertEqual(self.secondary.calls, [])

    def test_conflicting_failure_payload_never_replans_existing_or_terminal_plan(self):
        # HttpJsonBackend drops response.actions on non-2xx; the raw body still
        # must not authorize a second plan when it contains an existing plan.
        responses = ({"actions": [ACTION]}, {"semantic_complete": True},
                     {"error": "UNSAFE_ACTION_REJECTED"}, {"error": "INVALID_JSON_OBJECT"},
                     {"error": "CLARIFICATION_REQUIRED"}, {"error": "ACTION_NOT_ALLOWED"})
        for response in responses:
            with self.subTest(response=response):
                primary = failed("HTTP_503")
                primary.response = response
                wrapper = self.wrap(primary=BackendFixture(primary, "http://127.0.0.1:18101"))
                self.assertFalse(wrapper.infer(COMMAND).ok)
                self.assertEqual(self.secondary.calls, [])
                self.assertEqual(self.secondary.health_calls, 0)

    def test_budget_configuration_and_redirect_enabled_transport_are_rejected(self):
        for budget in (True, 0, -1, float("inf"), float("nan"), "100"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.wrap(launch_budget_ms=budget)
        primary = BackendFixture(failed(), "http://127.0.0.1:18101")
        primary.follow_redirects = True
        with self.assertRaises(ValueError):
            self.wrap(primary=primary)

    def test_simple_and_safety_tasks_never_use_recovery(self):
        for command in ("打开客厅灯", "关闭燃气报警器"):
            with self.subTest(command=command):
                wrapper = self.wrap()
                wrapper.infer(command)
                self.assertEqual(self.secondary.calls, [])
                self.assertEqual(self.secondary.health_calls, 0)

    def test_health_must_be_fresh_explicitly_true_and_reachable(self):
        for health in ({}, {"ready": True}, {"reachable": True}, {"ready": False, "reachable": True},
                       {"ready": True, "reachable": False}, {"ready": "true", "reachable": True},
                       {"ready": True, "reachable": 1}, TimeoutError("fixture health")):
            with self.subTest(health=health):
                secondary = BackendFixture(succeeded(), "http://127.0.0.1:18102", health=health)
                wrapper = self.wrap(secondary=secondary)
                out = wrapper.infer(COMMAND, {"recovery": {"ready": True, "route": "local-model"}})
                self.assertFalse(out.ok)
                self.assertEqual(secondary.health_calls, 1)
                self.assertEqual(secondary.calls, [])

    def test_each_request_rechecks_secondary_health(self):
        wrapper = self.wrap()
        self.assertTrue(wrapper.infer(COMMAND).ok)
        self.secondary.health_value = {"ready": False, "reachable": True}
        self.assertFalse(wrapper.infer(COMMAND).ok)
        self.assertEqual(self.secondary.health_calls, 2)
        self.assertEqual(len(self.secondary.calls), 1)

    def test_primary_latency_can_exhaust_launch_budget(self):
        clock = ManualClock()
        def consume():
            clock.value = 0.100
        wrapper = self.wrap(primary=BackendFixture(failed(), "http://127.0.0.1:18101", hook=consume),
                            launch_budget_ms=100, clock=clock)
        out = wrapper.infer(COMMAND)
        self.assertFalse(out.ok)
        self.assertEqual(self.secondary.calls, [])
        self.assertEqual(out.response["recovery"]["reason"], "launch_budget_exhausted")

    def test_health_latency_can_exhaust_launch_budget(self):
        clock = ManualClock()
        secondary = BackendFixture(succeeded(), "http://127.0.0.1:18102")
        def late_health():
            clock.value = 0.101
            return {"ready": True, "reachable": True}
        secondary.health = late_health
        wrapper = self.wrap(secondary=secondary, launch_budget_ms=100, clock=clock)
        out = wrapper.infer(COMMAND)
        self.assertFalse(out.ok)
        self.assertEqual(secondary.calls, [])

    def test_budget_is_launch_gate_not_claim_of_synchronous_deadline(self):
        clock = ManualClock()
        def slow_secondary():
            clock.value = 0.2
        wrapper = self.wrap(secondary=BackendFixture(succeeded(), "http://127.0.0.1:18102", hook=slow_secondary),
                            launch_budget_ms=100, clock=clock)
        result = wrapper.infer(COMMAND)
        self.assertTrue(result.ok)
        self.assertEqual(result.response["recovery"]["budget_scope"], "secondary_launch_only")
        self.assertGreater(result.response["recovery"]["elapsed_ms"], 100)

    def test_second_failure_or_refusal_never_starts_third_inference(self):
        for second in (failed("HTTP_503"), failed("HTTP_422", failure_kind="model_refusal")):
            with self.subTest(error=second.error):
                wrapper = self.wrap(secondary=BackendFixture(second, "http://127.0.0.1:18102"))
                out = wrapper.infer(COMMAND)
                self.assertFalse(out.ok)
                self.assertEqual(out.error, second.error)
                self.assertEqual(len(self.primary.calls), 1)
                self.assertEqual(len(self.secondary.calls), 1)
                self.assertEqual(out.response["recovery"]["primary_error"], "HTTP_503")
                self.assertEqual(wrapper.recovery_records[-1]["secondary_status"]["error"], second.error)

    def test_identical_command_context_and_mutation_isolation(self):
        context = {"devices": {"客厅": {"brightness": 40}}, "recovery": {"route": "http://evil.test"}}
        primary = BackendFixture(failed(), "http://127.0.0.1:18101", mutate=True)
        secondary = BackendFixture(succeeded(), "http://127.0.0.1:18102", mutate=True)
        wrapper = self.wrap(primary=primary, secondary=secondary)
        wrapper.infer(COMMAND, context)
        self.assertEqual(primary.calls[0], (COMMAND, context))
        self.assertEqual(secondary.calls[0], (COMMAND, context))
        self.assertEqual(context["devices"]["客厅"]["brightness"], 40)
        self.assertEqual(context["recovery"]["route"], "http://evil.test")

    def test_raised_transport_exception_is_visible_and_recoverable(self):
        wrapper = self.wrap(primary=BackendFixture(TimeoutError("fixture"), "http://127.0.0.1:18101"))
        result = wrapper.infer(COMMAND)
        self.assertTrue(result.ok)
        self.assertEqual(result.response["recovery"]["primary_error"], "TimeoutError")

    def test_primary_health_forwarding_never_probes_alternate(self):
        wrapper = self.wrap()
        self.assertEqual(wrapper.health(), {"ready": True, "reachable": True})
        self.assertEqual(self.primary.health_calls, 1)
        self.assertEqual(self.secondary.health_calls, 0)

    def test_non_literal_external_credentials_query_fragment_and_https_are_rejected(self):
        invalid = ("http://localhost:18101", "http://example.com", "http://192.168.1.1",
                   "http://127.0.0.2", "http://127.0.0.1@evil.test", "http://user:pass@127.0.0.1",
                   "http://127.0.0.1:18101?a=b", "http://127.0.0.1:18101#x",
                   "http://127.0.0.1:18101?", "https://127.0.0.1:18101", " http://127.0.0.1:18101")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.wrap(primary=BackendFixture(failed(), url))
        wrapper = self.wrap(primary=BackendFixture(failed(), "http://[::1]:18101/v1"))
        self.assertTrue(wrapper.infer(COMMAND).ok)

    def test_endpoint_mismatch_or_runtime_endpoint_change_blocks_inference(self):
        primary = BackendFixture(failed(), "http://127.0.0.1:18101")
        secondary = BackendFixture(succeeded(), "http://127.0.0.1:18102")
        with self.assertRaises(ValueError):
            self.cls(primary, secondary, primary_name="p", secondary_name="s",
                     primary_url="http://127.0.0.1:9999", secondary_url=secondary.base_url)
        wrapper = self.wrap(primary=primary, secondary=secondary)
        primary.base_url = "http://example.com"
        with self.assertRaises(ValueError):
            wrapper.infer(COMMAND)
        self.assertEqual(primary.calls, [])

    def test_nested_wrappers_and_same_backend_cannot_form_retry_cycle(self):
        wrapper = self.wrap()
        with self.assertRaises(ValueError):
            self.cls(wrapper, self.secondary, primary_name="p", secondary_name="s",
                     primary_url="http://127.0.0.1:18101", secondary_url="http://127.0.0.1:18102")
        with self.assertRaises(ValueError):
            self.cls(self.primary, self.primary, primary_name="p", secondary_name="s",
                     primary_url="http://127.0.0.1:18101", secondary_url="http://127.0.0.1:18101")

    def test_real_http_503_then_ready_second_service_has_exactly_two_posts(self):
        with http_fixture(infer_status=503, infer_body={"error": "fixture outage"}) as (first_url, first), \
                http_fixture() as (second_url, second):
            wrapper = self.wrap(primary=HttpJsonBackend(first_url, "fixture-p"),
                                secondary=HttpJsonBackend(second_url, "fixture-s"))
            context = {"devices": {"客厅": {"brightness": 0}}}
            result = wrapper.infer(COMMAND, context)
            self.assertTrue(result.ok)
            self.assertEqual([x[:2] for x in first], [("POST", "/infer")])
            self.assertEqual([x[:2] for x in second], [("GET", "/health"), ("POST", "/infer")])
            self.assertEqual(first[0][2], second[1][2])
            self.assertEqual(result.response["recovery"]["primary_error"], "HTTP_503")

    def test_real_http_refusal_malformed_json_and_redirect_never_contact_second(self):
        for status, body in ((422, {"error": "ACTION_NOT_ALLOWED"}), (200, b"{broken"),
                             (307, {"error": "fixture redirect"})):
            with self.subTest(status=status), \
                    http_fixture(infer_status=status, infer_body=body,
                                 redirect="http://127.0.0.1:1/forbidden") as (first_url, first), \
                    http_fixture() as (second_url, second):
                wrapper = self.wrap(primary=HttpJsonBackend(first_url, "fixture-p"),
                                    secondary=HttpJsonBackend(second_url, "fixture-s"))
                result = wrapper.infer(COMMAND)
                self.assertFalse(result.ok)
                self.assertEqual(len(first), 1)
                self.assertEqual(second, [])

    def test_existing_direct_backend_preserves_upstream_503_and_recovers_over_http(self):
        # This checks the real adapter's HTTP_502 wrapping, not model quality.
        sys.path.insert(0, str(ROOT / "docs/deployment"))
        from fused_eval import DirectModelBackend
        plan = {"answer": "fixture plan", "actions": [
            {"name": "light_control", "params": {"location": "书房", "action": "off"}},
            {"name": "curtain_control", "params": {"location": "书房", "action": "open"}},
        ]}
        openai_body = {"model": "fixture-plan", "choices": [{"message": {"content": json.dumps(plan)}}]}
        with http_fixture(infer_status=503, infer_body={"error": "fixture outage"}) as (first_url, first), \
                http_fixture(infer_body=openai_body, health={"data": [{"id": "fixture-plan"}]}) as (second_url, second):
            primary = DirectModelBackend(first_url + "/v1", "fixture-plan", "cloud",
                                         evidence_kind="http-fixture-not-model")
            secondary = DirectModelBackend(second_url + "/v1", "fixture-plan", "local",
                                           evidence_kind="http-fixture-not-model")
            wrapper = self.cls(primary, secondary, primary_name="cloud-model", secondary_name="local-model",
                               primary_url=first_url + "/v1", secondary_url=second_url + "/v1")
            executions = []
            event = self.gateway(wrapper, executions).handle(COMMAND)
            self.assertTrue(event["semantic_complete"])
            self.assertEqual(len(executions), 1)
            self.assertEqual(len(executions[0]), 2)
            meta = event["response"]["recovery"]
            self.assertEqual(meta["primary_error"], "HTTP_502")
            self.assertEqual(meta["primary_response"]["error"], "UPSTREAM_HTTP_503")
            self.assertEqual([x[:2] for x in first], [("POST", "/v1/chat/completions")])
            self.assertEqual([x[:2] for x in second], [("GET", "/v1/models"), ("POST", "/v1/chat/completions")])
            self.assertEqual(first[0][2]["messages"], second[1][2]["messages"])

    def test_existing_direct_backend_model_json_error_never_becomes_transport_retry(self):
        sys.path.insert(0, str(ROOT / "docs/deployment"))
        from fused_eval import DirectModelBackend
        malformed = {"choices": [{"message": {"content": "{broken"}}]}
        with http_fixture(infer_body=malformed) as (first_url, first), \
                http_fixture() as (second_url, second):
            primary = DirectModelBackend(first_url + "/v1", "fixture-plan", "cloud",
                                         evidence_kind="http-fixture-not-model")
            secondary = HttpJsonBackend(second_url, "fixture-secondary")
            wrapper = self.cls(primary, secondary, primary_name="cloud-model", secondary_name="local-model",
                               primary_url=first_url + "/v1", secondary_url=second_url)
            result = wrapper.infer(COMMAND)
            self.assertFalse(result.ok)
            self.assertEqual(result.response["recovery"]["primary_response"]["error"], "INVALID_MODEL_JSON")
            self.assertEqual(second, [])
            self.assertEqual(len(first), 1)

    def gateway(self, wrapper, executed):
        class Policy:
            def decide(self, task_class, link):
                return "cloud-model", "WAN_OK", "fixture policy"
        class Rules:
            def infer(self, command, context=None):
                return BackendResult(True, False, {"answer": "unresolved fixture"}, [],
                                     error="UNRESOLVED_NO_NLU", source="local-rules")
        def execute(actions, context):
            executed.append(deepcopy(actions))
            return {"actions": deepcopy(actions), "semantic_complete": True,
                    'plan_valid': True, 'execution_complete': True, 'task_complete': True}
        return OffloadGateway(Policy(), {"cloud-model": wrapper, "local-rules": Rules()},
                              LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=3),
                              action_executor=execute)

    def test_original_gateway_executes_recovered_actions_exactly_once(self):
        complete = succeeded()
        complete.actions = deepcopy(COMPLETE_ACTIONS)
        complete.response = {'actions': deepcopy(COMPLETE_ACTIONS)}
        wrapper = self.wrap(secondary=BackendFixture(complete, 'http://127.0.0.1:18102'))
        executions = []
        event = self.gateway(wrapper, executions).handle(COMMAND)
        self.assertEqual(executions, [COMPLETE_ACTIONS])
        self.assertTrue(event["semantic_complete"])
        self.assertEqual(event["selected_backend"], "cloud-model")
        self.assertEqual(event["response"]["recovery"]["actual_backend"], "local-model")
        self.assertIn("local-model", event["source"])

    def test_original_gateway_executes_zero_for_refusal_or_double_failure(self):
        for first, second in ((failed("HTTP_422", failure_kind="model_refusal"), succeeded()),
                              (failed("HTTP_503"), failed("HTTP_504")),
                              (failed("HTTP_503"), failed("HTTP_422", failure_kind="model_refusal"))):
            with self.subTest(first=first.error, second=second.error):
                wrapper = self.wrap(primary=BackendFixture(first, "http://127.0.0.1:18101"),
                                    secondary=BackendFixture(second, "http://127.0.0.1:18102"))
                executions = []
                event = self.gateway(wrapper, executions).handle(COMMAND)
                self.assertEqual(executions, [])
                self.assertFalse(event["semantic_complete"])

    def test_original_gateway_rejects_unsafe_recovered_actions_before_execution(self):
        unsafe = succeeded()
        unsafe.actions = [{"name": "shell", "params": {"command": "delete"}}]
        wrapper = self.wrap(secondary=BackendFixture(unsafe, "http://127.0.0.1:18102"))
        executions = []
        event = self.gateway(wrapper, executions).handle(COMMAND)
        self.assertEqual(executions, [])
        self.assertEqual(event["error"], "UNSAFE_ACTION_REJECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
