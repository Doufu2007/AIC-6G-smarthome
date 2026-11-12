"""Durable at-most-one-send boundary for request-bound software receipts.

An unknown result may only be queried. This is not an exactly-once claim about
physical devices: the MQTT entity commits software state, not physical state.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
EVIDENCE_KIND = "mqtt-software-entity"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_hash(payload):
    return hashlib.sha256(canonical({k: v for k, v in payload.items() if k != "request_sha256"}).encode("utf-8")).hexdigest()


def checked_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("INVALID_COMMAND_OR_PLATFORM_ID")
    return value


def receipt_outcome(receipt, payload):
    """Only a consistent receipt bound to the complete saved request can confirm."""
    if not isinstance(receipt, dict):
        return "unknown"
    if (receipt.get("schema") != "sensehome.execution-receipt.v1"
            or receipt.get("evidence_kind") != EVIDENCE_KIND
            or any(receipt.get(k) != payload[k] for k in ("command_id", "platform_id", "request_sha256"))
            or receipt.get("requested_actions") != payload["actions"]):
        return "unknown"
    before, after = receipt.get("state_version_before"), receipt.get("state_version_after")
    indices, result = receipt.get("completed_indices"), receipt.get("result")
    if (type(before) is not int or type(after) is not int or before < 0 or after < before
            or not isinstance(indices, list) or any(type(i) is not int for i in indices)
            or indices != sorted(set(indices)) or any(i < 0 or i >= len(payload["actions"]) for i in indices)
            or not isinstance(result, dict) or not isinstance(result.get("actions"), list)):
        return "unknown"
    actual = result["actions"]
    if any(not isinstance(a, dict) or set(a) != {"name", "params", "description"}
           or not isinstance(a["description"], str) for a in actual):
        return "unknown"
    normalized = [{"name": a["name"], "params": a["params"]} for a in actual]
    if normalized != [payload["actions"][i] for i in indices]:
        return "unknown"
    status = receipt.get("status")
    if (status == "applied" and indices == list(range(len(payload["actions"]))) and after == before + 1
            and result.get('semantic_complete') is True and receipt.get('error') is None):
        return "applied"
    if (status == "partial" and 0 < len(indices) < len(payload["actions"]) and after == before + 1
            and result.get('semantic_complete') is False and receipt.get('error') is None):
        return "partial"
    if (status == "rejected" and indices == [] and after == before and result.get('semantic_complete') is False
            and isinstance(receipt.get('error'), str) and receipt['error']):
        return "rejected"
    return "unknown"


class SQLiteReceiptExecutor:
    def __init__(self, db_path, transport, *, platform_id):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        self.platform_id = checked_id(platform_id)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                receipt_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, command_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
        return dict(row) if row else None

    def _result(self, payload, status, receipt=None, error=None):
        confirmed = status == "applied"
        result = deepcopy(receipt.get("result", {})) if receipt and status in {"applied", "partial"} else {}
        return {**result, "command_id": payload["command_id"], "request_sha256": payload["request_sha256"],
                "platform_id": payload["platform_id"], "execution_state": status,
                "semantic_complete": confirmed, "interface_ok": confirmed,
                "actions": result.get("actions", []), "receipt": deepcopy(receipt),
                "error": error or (None if confirmed else "EXECUTION_" + status.upper()),
                "execution_scope": EVIDENCE_KIND, "physical_execution_confirmed": False,
                "answer": ("已确认 MQTT 软件实体状态提交。" if confirmed else
                           "执行状态为%s；仅可回查，未自动重发。" % status)}

    def _accept(self, payload, receipt, error=None):
        status = receipt_outcome(receipt, payload)
        bound_receipt = receipt if status != "unknown" else None
        with self._db() as db:
            db.execute("UPDATE commands SET status=?, receipt_json=?, error=?, updated_at=? "
                       "WHERE command_id=? AND request_sha256=? AND status='unknown'",
                       (status, canonical(bound_receipt) if bound_receipt else None,
                        error or ("UNBOUND_OR_MISSING_RECEIPT" if status == "unknown" else None),
                        time.time(), payload["command_id"], payload["request_sha256"]))
        row = self._read(payload["command_id"])
        return self._from_row(row)

    def _from_row(self, row):
        return self._result(json.loads(row["payload_json"]), row["status"],
                            json.loads(row["receipt_json"]) if row["receipt_json"] else None, row["error"])

    def __call__(self, actions, context=None):
        context = dict(context or {})
        command_id = checked_id(context.get("command_id", uuid.uuid4().hex))
        command = context.get("_receipt_command")
        if not isinstance(command, str) or not command.strip() or len(command) > 4096:
            raise ValueError("MISSING_TRUSTED_RECEIPT_COMMAND")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
            raise ValueError("INVALID_ACTION_BATCH")
        payload = {"command_id": command_id, "platform_id": self.platform_id,
                   "command": command, "actions": deepcopy(actions)}
        payload["request_sha256"] = request_hash(payload)
        # Commit the unknown reservation BEFORE a network call. A process stop
        # at any following instruction must not make a later caller resend.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO commands VALUES (?, ?, ?, 'unknown', NULL, NULL, ?, ?)",
                           (command_id, payload["request_sha256"], canonical(payload), time.time(), time.time()))
        if existing is not None:
            if existing["request_sha256"] != payload["request_sha256"]:
                return self._result(payload, "conflict", error="COMMAND_ID_CONFLICT")
            return self.lookup(command_id)
        try:
            receipt = self.transport.submit(payload)
            return self._accept(payload, receipt)
        except Exception as exc:
            return self._accept(payload, None, type(exc).__name__)

    def lookup(self, command_id):
        command_id = checked_id(command_id)
        row = self._read(command_id)
        if row is None:
            # No stored request means there is no local binding with which to
            # verify a platform report. Do not create or submit a command.
            return {"command_id": command_id, "execution_state": "unknown", "semantic_complete": False,
                    "actions": [], "error": "COMMAND_NOT_FOUND", "physical_execution_confirmed": False}
        if row["status"] != "unknown":
            return self._from_row(row)
        payload = json.loads(row["payload_json"])
        try:
            return self._accept(payload, self.transport.lookup(command_id))
        except Exception as exc:
            return self._accept(payload, None, type(exc).__name__)

    def close(self):
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def snapshot(self):
        """Current platform report is separate from a historical command receipt."""
        return self.transport.snapshot()
