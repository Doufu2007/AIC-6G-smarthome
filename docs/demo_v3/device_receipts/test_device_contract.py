"""HTTP device contract fixture tests; no physical devices or Matter stack."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import http.server
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

HERE = Path(__file__).parent
ACTION = {"name": "light_control", "params": {"location": "客厅", "brightness": 60}}
INITIAL = {"lights": {"客厅": {"is_on": False, "brightness": 0}},
           "curtains": {"客厅": {"is_open": False, "position": 0}},
           "temperature": {"客厅": {"temp": 25, "humidity": 50}}}


def load_implementation(test):
    path = HERE / "device_contract.py"
    test.assertTrue(path.is_file(), "Independent device receipt contract fixture is not implemented")
    name = "device_receipt_contract_test_impl"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def command(command_id="demo-001", *, actions=None, version=0):
    return {"command_id": command_id, "expected_state_version": version,
            "actions": deepcopy([ACTION] if actions is None else actions)}


@contextmanager
def held_post_fixture(module):
    """POST is held until after the first receipt lookup; NOT a real device."""
    store = module.DeviceContractStore(INITIAL)
    release = threading.Event()
    applied = threading.Event()
    trace = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            trace.append(("POST", self.path))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            release.wait(3)
            status, body = store.submit(payload)
            applied.set()
            self.reply(status, body)

        def do_GET(self):
            trace.append(("GET", self.path))
            self.reply(*store.get_receipt(self.path.rsplit("/", 1)[-1]))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", trace, store, release, applied
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@contextmanager
def response_fixture(post_status, post_body):
    """Deliberately malformed receipt service; GET cannot confirm anything."""
    trace = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            trace.append(("POST", self.path))
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(post_status, post_body)
        def do_GET(self):
            trace.append(("GET", self.path))
            self.reply(404, {"error": "COMMAND_NOT_FOUND"})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", trace
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class DeviceReceiptTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_implementation(self)
        self.store = self.mod.DeviceContractStore(INITIAL)

    def test_same_id_and_payload_returns_original_receipt_without_second_commit(self):
        status, first = self.store.submit(command())
        status2, second = self.store.submit(command())
        self.assertEqual((status, status2), (200, 200))
        self.assertEqual(first, second)
        state = self.store.snapshot()
        self.assertEqual(state["state_version"], 1)
        self.assertEqual(state["applied_commands"], 1)
        self.assertEqual(state["state"]["lights"]["客厅"]["brightness"], 60)
        self.assertEqual(first["fixture_commit_count"], 1)
        self.assertEqual(first["evidence_kind"], "software-device-contract-fixture")

    def test_canonical_key_order_keeps_same_idempotency_fingerprint(self):
        status, receipt = self.store.submit(command())
        reordered = {"actions": [{"params": {"brightness": 60, "location": "客厅"},
                                  "name": "light_control"}],
                     "expected_state_version": 0, "command_id": "demo-001"}
        self.assertEqual(self.store.submit(reordered), (status, receipt))
        self.assertEqual(self.store.snapshot()["applied_commands"], 1)

    def test_same_id_different_payload_is_409_and_preserves_first_command(self):
        self.store.submit(command())
        different = command()
        different["actions"][0]["params"]["brightness"] = 20
        status, conflict = self.store.submit(different)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "COMMAND_ID_CONFLICT")
        self.assertEqual(self.store.snapshot()["state"]["lights"]["客厅"]["brightness"], 60)
        self.assertEqual(self.store.snapshot()["applied_commands"], 1)

    def test_entire_batch_is_validated_before_any_commit(self):
        batch = [ACTION, {"name": "unlock_door", "params": {"location": "客厅"}}]
        status, receipt = self.store.submit(command(actions=batch))
        self.assertEqual(status, 422)
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["error"], "ACTION_NOT_ALLOWED")
        self.assertEqual(receipt["fixture_commit_count"], 0)
        self.assertEqual(self.store.snapshot()["state"], INITIAL)
        self.assertEqual(self.store.snapshot()["state_version"], 0)

    def test_parameter_limits_and_supported_locations_are_authoritative(self):
        invalid_actions = [
            {"name": "light_control", "params": {"location": "客厅", "brightness": 101}},
            {"name": "light_control", "params": {"location": "客厅", "brightness": True}},
            {"name": "light_control", "params": {"location": "不存在", "brightness": 60}},
            {"name": "light_control", "params": {"location": "客厅", "action": "on", "shell": "x"}},
            {"name": "curtain_control", "params": {"location": "客厅", "position": -1}},
            {"name": "read_temperature", "params": {"location": "客厅", "value": 2}},
        ]
        for index, action in enumerate(invalid_actions):
            with self.subTest(action=action):
                status, receipt = self.store.submit(command(f"bad-{index}", actions=[action]))
                self.assertEqual(status, 422)
                self.assertEqual(receipt["fixture_commit_count"], 0)
                self.assertEqual(self.store.snapshot()["state"], INITIAL)

    def test_stale_expected_version_is_409_with_zero_commit(self):
        self.store.submit(command())
        status, receipt = self.store.submit(command("new-id", version=0))
        self.assertEqual(status, 409)
        self.assertEqual(receipt["error"], "STATE_VERSION_CONFLICT")
        self.assertEqual(receipt["state_version_before"], 1)
        self.assertEqual(receipt["state_version_after"], 1)
        self.assertEqual(receipt["fixture_commit_count"], 0)
        self.assertEqual(self.store.snapshot()["applied_commands"], 1)

    def test_rejected_id_is_still_reserved_and_receipt_is_queryable(self):
        invalid = command(actions=[{"name": "shell", "params": {}}])
        status, receipt = self.store.submit(invalid)
        self.assertEqual(status, 422)
        self.assertEqual(self.store.get_receipt("demo-001"), (200, receipt))
        self.assertEqual(self.store.submit(invalid), (422, receipt))
        self.assertEqual(self.store.submit(command())[0], 409)
        self.assertEqual(self.store.snapshot()["applied_commands"], 0)

    def test_bad_request_shape_and_invalid_ids_do_not_create_receipts(self):
        bad = [None, [], {}, {**command(), "extra": True}, {**command(), "command_id": "../../escape"},
               {**command(), "command_id": "a" * 65}, {**command(), "expected_state_version": True},
               {**command(), "expected_state_version": -1}]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self.store.submit(payload)[0], 400)
        self.assertEqual(self.store.snapshot()["applied_commands"], 0)

    def test_receipt_and_snapshot_copies_cannot_mutate_store(self):
        _, receipt = self.store.submit(command())
        receipt["status"] = "fabricated"
        state = self.store.snapshot()
        state["state"]["lights"]["客厅"]["brightness"] = 90
        self.assertEqual(self.store.get_receipt("demo-001")[1]["status"], "applied")
        self.assertEqual(self.store.snapshot()["state"]["lights"]["客厅"]["brightness"], 60)

    def test_concurrent_duplicate_commands_commit_only_once(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.store.submit(command()), range(16)))
        self.assertTrue(all(value == results[0] for value in results))
        self.assertEqual(self.store.snapshot()["state_version"], 1)
        self.assertEqual(self.store.snapshot()["applied_commands"], 1)

    def test_missing_receipt_is_unknown_not_proof_of_non_execution(self):
        status, body = self.store.get_receipt("unknown-id")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "COMMAND_NOT_FOUND")
        self.assertEqual(body["execution_state"], "unknown")
        self.assertEqual(body["store_persistence"], "memory_until_server_stops")

    def test_http_idempotency_conflict_and_query_with_real_loopback_server(self):
        with self.mod.DeviceContractServer(INITIAL) as server:
            client = self.mod.DeviceContractClient(server.url)
            first = client.submit(command())
            second = client.submit(command())
            self.assertEqual(first["outcome"], "applied")
            self.assertEqual(first["receipt"], second["receipt"])
            different = command()
            different["actions"][0]["params"]["brightness"] = 20
            conflict = client.submit(different)
            self.assertEqual(conflict["http_status"], 409)
            self.assertEqual(conflict["outcome"], "conflict")
            queried = client.lookup("demo-001")
            self.assertEqual(queried["outcome"], "reported_applied")
            self.assertEqual(queried["confirmation_scope"], "reported_receipt")
            self.assertFalse(queried["epoch_verified"])
            self.assertEqual(client.lookup("demo-001", expected_payload=command())["outcome"], "applied")
            self.assertEqual(server.store.snapshot()["applied_commands"], 1)
            self.assertEqual([method for method, path in server.trace].count("POST"), 3)

    def test_http_invalid_second_action_has_zero_partial_commit(self):
        with self.mod.DeviceContractServer(INITIAL) as server:
            client = self.mod.DeviceContractClient(server.url)
            batch = [ACTION, {"name": "disable_alarm", "params": {"location": "客厅"}}]
            result = client.submit(command(actions=batch))
            self.assertEqual(result["outcome"], "rejected")
            self.assertEqual(result["http_status"], 422)
            self.assertEqual(result["query_attempts"], 0)
            self.assertEqual(server.store.snapshot()["state"], INITIAL)
            self.assertEqual(server.store.snapshot()["applied_commands"], 0)

    def test_post_timeout_after_commit_queries_receipt_without_resending_action(self):
        with self.mod.DeviceContractServer(INITIAL, response_delay_s=0.15) as server:
            client = self.mod.DeviceContractClient(server.url, timeout_s=0.03)
            result = client.submit(command())
            self.assertEqual(result["outcome"], "applied")
            self.assertEqual(result["post_attempts"], 1)
            self.assertEqual(result["query_attempts"], 1)
            self.assertEqual(result["post_error"], "TimeoutError")
            self.assertEqual(server.trace, [("POST", "/commands"), ("GET", "/commands/demo-001")])
            self.assertEqual(server.store.snapshot()["applied_commands"], 1)

    def test_post_timeout_before_commit_remains_unknown_then_can_be_queried_later(self):
        with held_post_fixture(self.mod) as (url, trace, store, release, applied):
            client = self.mod.DeviceContractClient(url, timeout_s=0.03)
            result = client.submit(command())
            self.assertEqual(result["outcome"], "unknown")
            self.assertEqual(result["post_attempts"], 1)
            self.assertEqual(result["query_attempts"], 1)
            self.assertEqual(store.snapshot()["applied_commands"], 0)
            release.set()
            self.assertTrue(applied.wait(2))
            self.assertEqual(client.lookup("demo-001", expected_payload=command())["outcome"], "applied")
            self.assertEqual([item for item in trace if item[0] == "POST"], [("POST", "/commands")])
            self.assertEqual(store.snapshot()["applied_commands"], 1)

    def test_client_rejects_non_literal_loopback_urls_and_invalid_timeout(self):
        for url in ("http://localhost:1", "http://example.com", "https://127.0.0.1:1",
                    "http://127.0.0.1:1?q=1", "http://u:p@127.0.0.1:1", "http://127.0.0.1:1#x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.mod.DeviceContractClient(url)
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.mod.DeviceContractClient("http://127.0.0.1:1", timeout_s=timeout)

    def test_incomplete_or_conflicting_receipt_does_not_confirm_execution(self):
        _, valid = self.store.submit(command())
        absent_version = {k: v for k, v in valid.items() if k != "state_version_after"}
        cases = [(200, absent_version), (503, valid),
                 (200, {**valid, "fixture_commit_count": True}),
                 (200, {**valid, "state_version_after": 0}),
                 (200, {**valid, "command_id": "different-id"}),
                 (200, {**valid, "payload_sha256": "0" * 64})]
        for status, body in cases:
            with self.subTest(status=status, body=body), response_fixture(status, body) as (url, trace):
                result = self.mod.DeviceContractClient(url).submit(command())
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(trace, [("POST", "/commands"), ("GET", "/commands/demo-001")])

    def test_applied_receipt_is_bound_to_original_version_and_exact_action_batch(self):
        _, valid = self.store.submit(command())
        wrong_params = deepcopy(valid)
        wrong_params["actions"][0]["params"]["brightness"] = 20
        cases = [
            {**valid, "state_version_before": 99, "state_version_after": 100},
            {**valid, "requested_action_count": 0, "actions": []},
            {**valid, "requested_action_count": 2},
            wrong_params,
        ]
        for body in cases:
            with self.subTest(body=body), response_fixture(200, body) as (url, trace):
                result = self.mod.DeviceContractClient(url).submit(command())
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(trace, [("POST", "/commands"), ("GET", "/commands/demo-001")])

    def test_contract_json_schema_covers_request_receipt_and_state(self):
        import jsonschema
        schema_path = HERE / "device_contract.schema.json"
        self.assertTrue(schema_path.is_file(), "Device contract JSON schema is not supplied")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        for definition, value in (("command", command()), ("state", self.store.snapshot()),
                                  ("receipt", self.store.submit(command())[1])):
            with self.subTest(definition=definition):
                validator = jsonschema.Draft202012Validator({"$ref": f"#/$defs/{definition}", **schema})
                validator.validate(value)
        invalid = command()
        invalid["actions"][0]["params"]["brightness"] = 101
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator({"$ref": "#/$defs/command", **schema}).validate(invalid)
        # Loading the file directly must enforce a contract, too; a $defs-only
        # schema would silently accept any document unless the caller adds a ref.
        jsonschema.Draft202012Validator(schema).validate(command())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(invalid)
        _, receipt = self.store.get_receipt("demo-001")
        empty_applied = {**receipt, "requested_action_count": 0, "actions": []}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(empty_applied)


if __name__ == "__main__":
    unittest.main(verbosity=2)
