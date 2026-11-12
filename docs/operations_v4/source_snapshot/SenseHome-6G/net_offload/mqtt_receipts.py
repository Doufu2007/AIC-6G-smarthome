"""Real loopback MQTT broker/client adapter and durable *software* entity.

The entity is a protocol test target. It does not operate physical hardware.
AMQTT and paho-mqtt are optional dependencies, imported only when enabled.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from durable_receipts import EVIDENCE_KIND, canonical, checked_id, request_hash


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _append(path, value):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(canonical({"at": time.time(), **value}) + "\n")


def _validator():
    path = Path(__file__).resolve().parents[2] / "openvela_smart_home/dashboard/device_actions.py"
    spec = importlib.util.spec_from_file_location("mqtt_entity_device_actions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.execute_device_actions


class SoftwareEntityStore:
    """State and receipt commit in the same SQLite transaction, never hardware."""
    def __init__(self, path, platform_id, initial_state, *, partial_after=None):
        self.path, self.platform_id = Path(path), checked_id(platform_id)
        self.partial_after = partial_after
        self.validate = _validator()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS entity_state (id INTEGER PRIMARY KEY CHECK(id=1), platform_id TEXT, state_json TEXT, version INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS entity_commands (command_id TEXT PRIMARY KEY, request_sha256 TEXT, receipt_json TEXT)")
            row = db.execute("SELECT platform_id FROM entity_state WHERE id=1").fetchone()
            if row and row[0] != platform_id:
                raise ValueError("ENTITY_DATABASE_PLATFORM_MISMATCH")
            db.execute("INSERT OR IGNORE INTO entity_state VALUES (1, ?, ?, 0)", (platform_id, canonical(initial_state)))

    def lookup(self, command_id):
        checked_id(command_id)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT receipt_json FROM entity_commands WHERE command_id=?", (command_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            state_json, version = db.execute('SELECT state_json, version FROM entity_state WHERE id=1').fetchone()
        return {'schema': 'sensehome.software-state.v1', 'platform_id': self.platform_id,
                'evidence_kind': EVIDENCE_KIND, 'state_version': version, 'software_state': json.loads(state_json)}

    def submit(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"command_id", "platform_id", "command", "actions", "request_sha256"}
                or payload["platform_id"] != self.platform_id or payload["request_sha256"] != request_hash(payload)):
            return {"error": "REQUEST_BINDING_REJECTED"}
        checked_id(payload["command_id"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_sha256, receipt_json FROM entity_commands WHERE command_id=?", (payload["command_id"],)).fetchone()
            if old:
                return json.loads(old[1]) if old[0] == payload["request_sha256"] else {"error": "COMMAND_ID_CONFLICT"}
            state_json, before = db.execute("SELECT state_json, version FROM entity_state WHERE id=1").fetchone()
            state = json.loads(state_json)
            status, error, completed = "applied", None, []
            result = {"actions": [], "observation": "", "semantic_complete": False}
            try:
                # Validate the complete request through the original allowlist
                # before any software state can change, including fault tests.
                candidate = deepcopy(state)
                full_result = self.validate(candidate, deepcopy(payload["actions"]))
                completed = list(range(len(payload["actions"])))
                if self.partial_after is not None and 0 < self.partial_after < len(completed):
                    status = "partial"
                    completed = completed[:self.partial_after]
                    candidate = deepcopy(state)
                    result = self.validate(candidate, deepcopy(payload["actions"][:self.partial_after]))
                else:
                    result = full_result
                state = candidate
            except (ValueError, TypeError, KeyError) as exc:
                status, error = "rejected", str(exc)
                completed = []
            after = before + int(status in {"applied", "partial"})
            receipt = {"schema": "sensehome.execution-receipt.v1", "evidence_kind": EVIDENCE_KIND,
                       "command_id": payload["command_id"], "platform_id": self.platform_id,
                       "request_sha256": payload["request_sha256"], "requested_actions": payload["actions"],
                       "status": status, "completed_indices": completed,
                       "state_version_before": before, "state_version_after": after,
                       "result": result, "error": error, "software_state": state,
                       "physical_execution_confirmed": False}
            db.execute("UPDATE entity_state SET state_json=?, version=? WHERE id=1", (canonical(state), after))
            db.execute("INSERT INTO entity_commands VALUES (?, ?, ?)", (payload["command_id"], payload["request_sha256"], canonical(receipt)))
            return receipt


class MQTTReceiptTransport:
    def __init__(self, host, port, platform_id, *, timeout_s=2.0, event_path=None):
        if host != "127.0.0.1":
            raise ValueError("MQTT_ADAPTER_LOOPBACK_ONLY")
        if type(port) is not int or not 1 <= port <= 65535 or not 0 < timeout_s <= 30:
            raise ValueError("INVALID_MQTT_CONFIGURATION")
        import paho.mqtt.client as mqtt
        self.platform_id = checked_id(platform_id)
        self.timeout_s, self.event_path = timeout_s, event_path
        self.prefix = "sensehome/v4/" + platform_id
        self.reply_topic = self.prefix + "/replies/" + uuid.uuid4().hex
        self.pending, self.lock = {}, threading.Lock()
        self.ready = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gateway-" + uuid.uuid4().hex,
                                  protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.client.on_connect = self._connected
        self.client.on_subscribe = lambda *a: self.ready.set()
        self.client.on_message = self._received
        # There is no command retransmission or reconnect loop in this adapter.
        self.client.reconnect_delay_set(min_delay=1, max_delay=1)
        try:
            self.client.connect(host, port, keepalive=30)
            self.client.loop_start()
            if not self.ready.wait(timeout_s):
                raise TimeoutError("MQTT_SUBSCRIBE_NOT_READY")
        except BaseException:
            self.close()
            raise

    def _connected(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(self.reply_topic, qos=1)

    def _received(self, client, userdata, message):
        try:
            value = json.loads(message.payload)
            with self.lock:
                waiting = self.pending.get(value.get("correlation_id"))
            if waiting:
                waiting.put_nowait(value.get("receipt"))
        except (ValueError, TypeError, queue.Full):
            return

    def _exchange(self, operation, **body):
        correlation_id, waiting = uuid.uuid4().hex, queue.Queue(maxsize=1)
        with self.lock:
            self.pending[correlation_id] = waiting
        envelope = {"operation": operation, "correlation_id": correlation_id, "reply_to": self.reply_topic, **body}
        try:
            if self.event_path:
                _append(self.event_path, {"direction": "publish", "topic": self.prefix + "/requests", "envelope": envelope})
            # QoS 0 plus reconnect_on_failure=False prevents automatic command
            # retransmission. Durability and ACK recovery live above MQTT.
            info = self.client.publish(self.prefix + "/requests", canonical(envelope).encode("utf-8"), qos=0, retain=False)
            info.wait_for_publish(timeout=self.timeout_s)
            receipt = waiting.get(timeout=self.timeout_s)
            if self.event_path:
                _append(self.event_path, {"direction": "receive", "operation": operation, "receipt": receipt})
            return receipt
        except (queue.Empty, RuntimeError):
            return None
        finally:
            with self.lock:
                self.pending.pop(correlation_id, None)

    def submit(self, payload):
        return self._exchange("submit", payload=payload)

    def lookup(self, command_id):
        return self._exchange("lookup", command_id=checked_id(command_id))

    def snapshot(self):
        return self._exchange('snapshot')

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


async def _broker(directory, port):
    from amqtt.broker import Broker
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:%d" % port}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    actual_port = broker._servers["default"].instance.sockets[0].getsockname()[1]
    _write_json(directory / "broker_ready.json", {"port": actual_port, "pid": os.getpid(), "binding": "127.0.0.1"})
    try:
        while not (directory / "stop_broker").exists():
            await asyncio.sleep(0.05)
    finally:
        await broker.shutdown()


def _entity(directory, port, platform_id, drop_first_ack, partial_after):
    import paho.mqtt.client as mqtt
    store = SoftwareEntityStore(directory / "entity.sqlite3", platform_id,
                                json.loads((directory / "initial_state.json").read_text(encoding="utf-8")), partial_after=partial_after)
    prefix, dropped = "sensehome/v4/" + platform_id, set()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="entity-" + uuid.uuid4().hex, protocol=mqtt.MQTTv311)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            client.subscribe(prefix + "/requests", qos=1)

    def on_subscribe(*args):
        _write_json(directory / "entity_ready.json", {"pid": os.getpid(), "platform_id": platform_id, "evidence_kind": EVIDENCE_KIND})

    def on_message(client, userdata, message):
        try:
            value = json.loads(message.payload)
            _append(directory / "entity_events.jsonl", {"topic": message.topic, "envelope": value})
            reply = value.get("reply_to", "")
            if not isinstance(reply, str) or not reply.startswith(prefix + "/replies/"):
                return
            if value.get("operation") == "submit":
                receipt = store.submit(value["payload"])
                command_id = value["payload"]["command_id"]
                if drop_first_ack and command_id not in dropped:
                    dropped.add(command_id)
                    _append(directory / "entity_events.jsonl", {"ack_dropped_after_commit": command_id, "receipt": receipt})
                    return
            elif value.get("operation") == "lookup":
                receipt = store.lookup(value["command_id"])
            elif value.get('operation') == 'snapshot':
                receipt = store.snapshot()
            else:
                return
            response = {"correlation_id": value["correlation_id"], "receipt": receipt}
            _append(directory / "entity_events.jsonl", {"reply": response})
            client.publish(reply, canonical(response).encode("utf-8"), qos=1, retain=False)
        except Exception as exc:
            _append(directory / "entity_events.jsonl", {"error": type(exc).__name__, "detail": str(exc)})

    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    client.connect("127.0.0.1", port, keepalive=30)
    client.loop_start()
    try:
        while not (directory / "stop_entity").exists():
            time.sleep(0.05)
    finally:
        client.disconnect()
        client.loop_stop()


class LocalMQTTSession:
    """Own and clean up only this context's broker/entity child processes.

    Environment for an isolated bridge is available as ``session.environment``.
    The caller owns its private directory and must not overwrite old evidence.
    """
    def __init__(self, run_dir, *, initial_state, port=0, platform_id="software-home", drop_first_ack=False, partial_after=None):
        self.directory = Path(run_dir).resolve()
        self.initial_state, self.port = initial_state, port
        self.platform_id, self.drop_first_ack, self.partial_after = checked_id(platform_id), drop_first_ack, partial_after
        self.children, self.handles = [], []
        self.retired_children = []

    @property
    def environment(self):
        return {"SENSEHOME_RECEIPT_DB": str(self.directory / "gateway.sqlite3"),
                "SENSEHOME_MQTT_HOST": "127.0.0.1", "SENSEHOME_MQTT_PORT": str(self.port),
                "SENSEHOME_MQTT_PLATFORM": self.platform_id,
                "SENSEHOME_MQTT_LOG": str(self.directory / "gateway_mqtt.jsonl")}

    def _start(self, role, *args):
        stdout = (self.directory / (role + "_stdout.txt")).open("ab")
        stderr = (self.directory / (role + "_stderr.txt")).open("ab")
        self.handles.extend([stdout, stderr])
        argv = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--" + role, "--directory", str(self.directory), *args]
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.children.append((role, process, argv))
        _append(self.directory / 'process_launches.jsonl', {'role': role, 'pid': process.pid, 'argv': argv})
        until = time.monotonic() + 15
        ready_path = self.directory / (role + "_ready.json")
        while time.monotonic() < until:
            if process.poll() is not None:
                raise RuntimeError(role + " exited; see " + str(self.directory / (role + "_stderr.txt")))
            if ready_path.exists():
                return json.loads(ready_path.read_text(encoding="utf-8"))
            time.sleep(0.05)
        raise TimeoutError(role + " startup timed out")

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        # Reusing a live/old ready marker can falsely claim our child is ready.
        if any((self.directory / p).exists() for p in ("broker_ready.json", "entity_ready.json", "stop_broker", "stop_entity")):
            raise ValueError("MQTT_RUN_DIRECTORY_MUST_BE_FRESH")
        _write_json(self.directory / "initial_state.json", self.initial_state)
        try:
            self.port = self._start("broker", "--port", str(self.port))["port"]
            self._start_entity()
            _write_json(self.directory / "processes.json", {"processes": [{"role": role, "pid": p.pid, "argv": argv} for role, p, argv in self.children]})
            return self
        except BaseException:
            self.close()
            raise

    def _start_entity(self):
        options = ["--port", str(self.port), "--platform-id", self.platform_id]
        if self.drop_first_ack:
            options.append("--drop-first-ack")
        if self.partial_after is not None:
            options.extend(["--partial-after", str(self.partial_after)])
        self._start("entity", *options)

    def restart_entity(self):
        """Gracefully restart our software entity against its existing DB."""
        active = next((entry for entry in self.children if entry[0] == 'entity'), None)
        if active is None:
            raise RuntimeError('NO_OWNED_ENTITY')
        role, process, argv = active
        (self.directory / 'stop_entity').touch()
        process.wait(timeout=10)
        self.retired_children.append({"role": role, "pid": process.pid, "exit_code": process.returncode,
                                      "forced_termination": False, "reason": "requested_entity_restart", "argv": argv})
        self.children.remove(active)
        # These are our exact markers in the verified session directory.
        (self.directory / 'stop_entity').unlink()
        (self.directory / 'entity_ready.json').unlink()
        self._start_entity()

    def close(self):
        records = list(self.retired_children)
        for role, process, argv in reversed(self.children):
            (self.directory / ("stop_" + role)).touch()
            forced = False
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forced = True
                process.terminate()
                process.wait(timeout=5)
            records.append({"role": role, "pid": process.pid, "exit_code": process.returncode, "forced_termination": forced})
        for handle in self.handles:
            handle.close()
        _write_json(self.directory / "cleanup.json", {"owned_processes": records, "all_stopped": all(p.poll() is not None for _, p, _ in self.children)})
        self.children, self.handles = [], []

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("--entity", action="store_true")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--platform-id", default="software-home")
    parser.add_argument("--drop-first-ack", action="store_true")
    parser.add_argument("--partial-after", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.broker:
        asyncio.run(_broker(args.directory, args.port))
    elif args.entity:
        _entity(args.directory, args.port, args.platform_id, args.drop_first_ack, args.partial_after)
    else:
        parser.error("choose --broker or --entity")


if __name__ == "__main__":
    main()
