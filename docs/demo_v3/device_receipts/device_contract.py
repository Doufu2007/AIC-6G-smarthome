"""Independent loopback HTTP device-contract fixture, not a device driver.

The state, commits and receipts are software fixtures only.  There is no Matter
stack, hardware, cloud access or integration with the existing gateway here.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import http.server
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "openvela_smart_home/dashboard"))
from device_actions import execute_device_actions  # noqa: E402

EVIDENCE_KIND = "software-device-contract-fixture"
PERSISTENCE = "memory_until_server_stops"
MAX_BODY = 65536
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
DEFAULT_STATE = {
    "lights": {"客厅": {"is_on": False, "brightness": 0}},
    "curtains": {"客厅": {"is_open": False, "position": 0}},
    "temperature": {"客厅": {"temp": 25, "humidity": 50}},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def checked_id(value):
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValueError("INVALID_COMMAND_ID")
    return value


class DeviceContractStore:
    """One locked, in-memory epoch of atomic fixture state transactions."""
    def __init__(self, initial_state=None):
        self._state = deepcopy(DEFAULT_STATE if initial_state is None else initial_state)
        self._version = 0
        self._applied = 0
        self._receipts = {}
        self._lock = threading.Lock()
        self.store_id = uuid.uuid4().hex
        self._last_commit = time.monotonic()

    def _metadata(self, schema):
        return {"schema": schema, "evidence_kind": EVIDENCE_KIND, "store_id": self.store_id,
                "store_persistence": PERSISTENCE}

    def _error(self, error, **extra):
        return {**self._metadata("sensehome.device-error.v1"), "error": error, **extra}

    def snapshot(self):
        with self._lock:
            return {**self._metadata("sensehome.device-state.v1"), "state_version": self._version,
                    "applied_commands": self._applied, "state": deepcopy(self._state),
                    "observed_at_utc": now(),
                    "state_age_ms": round((time.monotonic() - self._last_commit) * 1000, 3)}

    def get_receipt(self, command_id):
        try:
            checked_id(command_id)
        except ValueError:
            return 400, self._error("INVALID_COMMAND_ID", execution_state="unknown")
        with self._lock:
            stored = self._receipts.get(command_id)
            if stored is None:
                # This is also returned after an epoch restart; absence cannot
                # prove a previous instance did not execute a physical action.
                return 404, self._error("COMMAND_NOT_FOUND", command_id=command_id,
                                        execution_state="unknown")
            return 200, deepcopy(stored["receipt"])

    def submit(self, payload):
        payload = deepcopy(payload)
        if not isinstance(payload, dict) or set(payload) != {"command_id", "expected_state_version", "actions"}:
            return 400, self._error("INVALID_COMMAND_SCHEMA")
        try:
            command_id = checked_id(payload["command_id"])
            payload_hash = fingerprint(payload)
        except (ValueError, TypeError):
            return 400, self._error("INVALID_COMMAND_SCHEMA")
        expected = payload["expected_state_version"]
        if type(expected) is not int or expected < 0:
            return 400, self._error("INVALID_EXPECTED_STATE_VERSION")

        # Check idempotency, version and commit inside the same lock. A second
        # thread cannot validate against stale state and then commit twice.
        with self._lock:
            stored = self._receipts.get(command_id)
            if stored is not None:
                if payload_hash != stored["payload_hash"]:
                    return 409, self._error("COMMAND_ID_CONFLICT", command_id=command_id,
                                            stored_payload_sha256=stored["payload_hash"],
                                            payload_sha256=payload_hash)
                return stored["http_status"], deepcopy(stored["receipt"])

            before = self._version
            status, error, result = 200, None, None
            candidate = deepcopy(self._state)
            if expected != before:
                status, error = 409, "STATE_VERSION_CONFLICT"
            else:
                try:
                    # The existing allowlist validates the whole batch on a
                    # copy before mutating this private candidate. No live
                    # device operation, shell, network call or file write.
                    result = execute_device_actions(candidate, deepcopy(payload["actions"]))
                except (TypeError, ValueError, KeyError) as exc:
                    status, error = 422, str(exc) or type(exc).__name__
            if status == 200:
                self._state = candidate
                self._version += 1
                self._applied += 1
                self._last_commit = time.monotonic()
            receipt = {
                **self._metadata("sensehome.device-receipt.v1"),
                "command_id": command_id, "payload_sha256": payload_hash,
                "status": "applied" if status == 200 else "rejected", "http_status": status,
                "state_version_before": before, "state_version_after": self._version,
                "fixture_commit_count": int(status == 200), "error": error,
                "requested_action_count": len(payload["actions"]) if isinstance(payload["actions"], list) else 0,
                "actions": deepcopy(result["actions"]) if result is not None else [],
                "observation": result["observation"] if result is not None else "",
                "created_at_utc": now(),
            }
            self._receipts[command_id] = {"payload_hash": payload_hash, "http_status": status,
                                          "receipt": deepcopy(receipt)}
            return status, receipt


class DeviceContractServer:
    """Only binds IPv4 loopback. response_delay_s simulates a lost/delayed ACK."""
    def __init__(self, initial_state=None, *, port=0, response_delay_s=0):
        if (type(port) is not int or not 0 <= port <= 65535
                or isinstance(response_delay_s, bool) or not isinstance(response_delay_s, (int, float))
                or not math.isfinite(response_delay_s) or not 0 <= response_delay_s <= 5):
            raise ValueError("INVALID_FIXTURE_SERVER_CONFIGURATION")
        self.store = DeviceContractStore(initial_state)
        self.port = port
        self.response_delay_s = response_delay_s
        self.trace = []

    def __enter__(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The committed receipt remains GET-queryable.

            def do_GET(self):
                owner.trace.append(("GET", self.path))
                if self.path == "/health":
                    self.reply(200, {**owner.store._metadata("sensehome.device-health.v1"), "ready": True})
                elif self.path == "/state":
                    self.reply(200, owner.store.snapshot())
                elif self.path.startswith("/commands/"):
                    self.reply(*owner.store.get_receipt(self.path[len("/commands/"):]))
                else:
                    self.reply(404, owner.store._error("NOT_FOUND"))

            def do_POST(self):
                owner.trace.append(("POST", self.path))
                if self.path != "/commands":
                    self.reply(404, owner.store._error("NOT_FOUND"))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError("INVALID_LENGTH")
                    def reject_constant(value):
                        raise ValueError("NON_FINITE_JSON")
                    payload = json.loads(self.rfile.read(length), parse_constant=reject_constant)
                except (ValueError, UnicodeError):
                    self.reply(400, owner.store._error("INVALID_JSON_OR_LENGTH"))
                    return
                status, receipt = owner.store.submit(payload)
                if owner.response_delay_s:
                    time.sleep(owner.response_delay_s)
                self.reply(status, receipt)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def checked_url(value):
    if not isinstance(value, str) or value != value.strip() or "?" in value or "#" in value:
        raise ValueError("ONLY_LITERAL_LOOPBACK_HTTP")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ONLY_LITERAL_LOOPBACK_HTTP") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/")
            or any(char.isspace() for char in value)):
        raise ValueError("ONLY_LITERAL_LOOPBACK_HTTP")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    if parsed.netloc != (host if port is None else f"{host}:{port}") or (port is not None and not 1 <= port <= 65535):
        raise ValueError("ONLY_LITERAL_LOOPBACK_HTTP")
    return parsed


class DeviceContractClient:
    """Send a command once. An uncertain response allows GET lookup only."""
    def __init__(self, base_url, *, timeout_s=2):
        self.endpoint = checked_url(base_url)
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("TIMEOUT_MUST_BE_POSITIVE_FINITE")
        self.timeout_s = timeout_s

    def _request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(self.endpoint.hostname, self.endpoint.port, timeout=self.timeout_s)
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
        try:
            connection.request(method, path, raw, {"Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                return response.status, {}, "RESPONSE_TOO_LARGE"
            body = json.loads(data)
            if not isinstance(body, dict):
                return response.status, {}, "INVALID_RESPONSE_OBJECT"
            return response.status, body, None
        except Exception as exc:
            return 0, {}, type(exc).__name__
        finally:
            connection.close()

    @staticmethod
    def _receipt_outcome(body, command_id, payload_hash=None, expected_payload=None):
        required = {"schema", "evidence_kind", "store_id", "store_persistence", "command_id", "payload_sha256",
                    "status", "http_status", "state_version_before", "state_version_after", "fixture_commit_count",
                    "error", "requested_action_count", "actions", "observation", "created_at_utc"}
        if (set(body) != required or body.get("schema") != "sensehome.device-receipt.v1"
                or body.get("evidence_kind") != EVIDENCE_KIND or body.get("store_persistence") != PERSISTENCE
                or body.get("command_id") != command_id
                or (payload_hash is not None and body.get("payload_sha256") != payload_hash)):
            return "unknown"
        if (not isinstance(body["store_id"], str) or re.fullmatch(r"[a-f0-9]{32}", body["store_id"]) is None
                or not isinstance(body["payload_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", body["payload_sha256"]) is None
                or not isinstance(body["actions"], list) or not isinstance(body["observation"], str)
                or not isinstance(body["created_at_utc"], str)):
            return "unknown"
        for field in ("state_version_before", "state_version_after", "fixture_commit_count", "http_status", "requested_action_count"):
            if type(body[field]) is not int or body[field] < 0:
                return "unknown"
        before, after = body["state_version_before"], body["state_version_after"]
        if expected_payload is not None:
            expected_actions = expected_payload.get("actions")
            expected_count = len(expected_actions) if isinstance(expected_actions, list) else 0
            if (body["payload_sha256"] != fingerprint(expected_payload)
                    or body["requested_action_count"] != expected_count):
                return "unknown"
        if (body["status"] == "applied" and body["fixture_commit_count"] == 1
                and body["http_status"] == 200 and body["error"] is None and after == before + 1):
            if not 1 <= len(body["actions"]) <= 16 or len(body["actions"]) != body["requested_action_count"]:
                return "unknown"
            for action in body["actions"]:
                if (not isinstance(action, dict) or set(action) != {"name", "params", "description"}
                        or action["name"] not in {"light_control", "curtain_control", "read_temperature"}
                        or not isinstance(action["params"], dict) or not isinstance(action["description"], str)):
                    return "unknown"
            if expected_payload is not None:
                if before != expected_payload.get("expected_state_version"):
                    return "unknown"
                actual_actions = [{"name": action["name"], "params": action["params"]} for action in body["actions"]]
                if actual_actions != expected_actions:
                    return "unknown"
            return "applied"
        if (body["status"] == "rejected" and body["fixture_commit_count"] == 0
                and body["http_status"] in (409, 422) and isinstance(body["error"], str)
                and body["error"] and after == before and body["actions"] == []):
            return "rejected"
        return "unknown"

    def lookup(self, command_id, *, payload_hash=None, expected_payload=None):
        command_id = checked_id(command_id)
        expected_payload = deepcopy(expected_payload)
        if expected_payload is not None and (not isinstance(expected_payload, dict) or expected_payload.get("command_id") != command_id):
            raise ValueError("EXPECTED_PAYLOAD_ID_MISMATCH")
        status, body, error = self._request("GET", "/commands/" + command_id)
        outcome = self._receipt_outcome(body, command_id, payload_hash, expected_payload) if status == 200 and not error else "unknown"
        if expected_payload is None and outcome != "unknown":
            outcome = "reported_" + outcome
        return {"outcome": outcome, "receipt": body if outcome != "unknown" else None,
                "http_status": status, "error": error or body.get("error"),
                "post_attempts": 0, "query_attempts": 1,
                "confirmation_scope": "request_bound_receipt" if expected_payload is not None else "reported_receipt",
                "epoch_verified": False}

    def submit(self, payload):
        payload = deepcopy(payload)
        if not isinstance(payload, dict):
            raise ValueError("INVALID_COMMAND_SCHEMA")
        command_id = checked_id(payload.get("command_id"))
        payload_hash = fingerprint(payload)
        status, body, error = self._request("POST", "/commands", payload)
        outcome = (self._receipt_outcome(body, command_id, payload_hash, payload)
                   if not error and status in (200, 409, 422) and body.get("http_status") == status else "unknown")
        if status == 409 and body.get("error") == "COMMAND_ID_CONFLICT":
            outcome = "conflict"
        if (400 <= status < 500 and outcome == "unknown" and not error
                and body.get("schema") == "sensehome.device-error.v1" and isinstance(body.get("error"), str)):
            outcome = "rejected"
        if outcome != "unknown":
            return {"outcome": outcome, "receipt": body, "http_status": status,
                    "error": body.get("error"), "post_attempts": 1, "query_attempts": 0,
                    "post_http_status": status, "post_error": error,
                    "confirmation_scope": "request_bound_receipt" if body.get("schema") == "sensehome.device-receipt.v1" else "explicit_rejection",
                    "epoch_verified": False}
        # No POST retry, even for a timeout before commit, a 5xx, invalid JSON
        # or a missing receipt. Lookup absence remains execution-state unknown.
        result = self.lookup(command_id, payload_hash=payload_hash, expected_payload=payload)
        return {**result, "post_attempts": 1, "post_http_status": status,
                "post_error": error or ("HTTP_%d" % status if status else "UNKNOWN_POST_STATE")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--response-delay-ms", type=float, default=0,
                        help="Fixture ACK delay only; not network or hardware latency evidence")
    args = parser.parse_args()
    with DeviceContractServer(port=args.port, response_delay_s=args.response_delay_ms / 1000) as server:
        print(json.dumps({"url": server.url, "evidence_kind": EVIDENCE_KIND,
                          "store_id": server.store.store_id, "store_persistence": PERSISTENCE}, ensure_ascii=False), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
