"""Lifecycle regressions against an owned real loopback broker and entity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

NET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NET))
from durable_receipts import SQLiteReceiptExecutor
from mqtt_receipts import LocalMQTTSession, MQTTReceiptTransport

STATE = {"lights": {"客厅": {"is_on": False, "brightness": 0}},
         "curtains": {"客厅": {"is_open": False, "position": 0}},
         "temperature": {"客厅": {"temp": 25, "humidity": 50}}}
ACTION = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]


def eventually(read, predicate, timeout=8):
    until = time.monotonic() + timeout
    value = None
    while time.monotonic() < until:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.03)
    raise AssertionError("condition did not become true; last value: %r" % (value,))


def stop_owned_child(session, role):
    entry = next(entry for entry in session.children if entry[0] == role)
    _, process, argv = entry
    (session.directory / ("stop_" + role)).touch()
    process.wait(timeout=10)
    session.children.remove(entry)
    session.retired_children.append({"role": role, "pid": process.pid,
                                    "exit_code": process.returncode, "argv": argv,
                                    "forced_termination": False, "reason": "lifecycle_regression"})
    (session.directory / ("stop_" + role)).unlink()
    (session.directory / (role + "_ready.json")).unlink()


def restart_owned_broker(session):
    session._start("broker", "--port", str(session.port))


def envelopes(directory):
    path = Path(directory) / "entity_events.jsonl"
    return [record["envelope"] for line in path.read_text(encoding="utf-8").splitlines()
            if "envelope" in (record := json.loads(line))]


class MQTTLifecycleTests(unittest.TestCase):
    def test_snapshot_reports_real_entity_sample_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                try:
                    before = time.time()
                    current = transport.snapshot()
                    after = time.time()
                    self.assertIsInstance(current.get("sampled_at"), float)
                    self.assertLessEqual(before, current["sampled_at"])
                    self.assertLessEqual(current["sampled_at"], after)
                finally:
                    transport.close()

    def test_original_transport_recovers_after_real_broker_restart_without_resending_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE), drop_first_ack=True) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id, timeout_s=0.4)
                executor = SQLiteReceiptExecutor(Path(tmp) / "gateway.sqlite3", transport,
                                                platform_id=session.platform_id)
                try:
                    first = executor(ACTION, {"command_id": "lost-ack", "_receipt_command": "客厅灯60%"})
                    self.assertEqual(first["execution_state"], "unknown")
                    stop_owned_child(session, "broker")
                    eventually(transport.client.is_connected, lambda connected: not connected)
                    # This submission is reserved durably while the network is
                    # down. Reconnection must not turn it into a queued action.
                    offline = executor(ACTION, {"command_id": "offline", "_receipt_command": "客厅灯60%"})
                    self.assertEqual(offline["execution_state"], "unknown")
                    restart_owned_broker(session)
                    current = eventually(transport.snapshot, lambda value: isinstance(value, dict))
                    self.assertEqual(current["state_version"], 1)
                    recovered = eventually(lambda: executor.lookup("lost-ack"),
                                           lambda value: value["execution_state"] == "applied")
                    self.assertEqual(recovered["receipt"]["state_version_after"], 1)
                    self.assertEqual(executor.lookup("offline")["execution_state"], "unknown")
                    self.assertEqual(executor(ACTION, {"command_id": "lost-ack", "_receipt_command": "客厅灯60%"})
                                     ["execution_state"], "applied")
                    fresh = executor(ACTION, {"command_id": "after-reconnect", "_receipt_command": "客厅灯60%"})
                    self.assertEqual(fresh["execution_state"], "unknown")  # first ACK is deliberately dropped
                    self.assertEqual(executor.lookup("after-reconnect")["receipt"]["state_version_after"], 2)
                    submitted = [v["payload"]["command_id"] for v in envelopes(tmp) if v["operation"] == "submit"]
                    self.assertEqual(submitted, ["lost-ack", "after-reconnect"])
                finally:
                    transport.close()

    def test_disconnect_clears_subscription_readiness_and_reports_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                try:
                    self.assertTrue(transport.ready.is_set())
                    stop_owned_child(session, "broker")
                    eventually(transport.client.is_connected, lambda connected: not connected)
                    self.assertFalse(transport.ready.is_set())
                    self.assertFalse(transport.connection_state()["connected"])
                    self.assertFalse(transport.connection_state()["subscribed"])
                finally:
                    transport.close()

    def test_unsent_qos0_submission_is_discarded_on_reconnect(self):
        # Narrow characterization of the paho boundary: pause its network writer
        # so the request is accepted locally but has not reached the broker.
        # A disconnect must discard that packet rather than replay it later.
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id, timeout_s=2)
                executor = SQLiteReceiptExecutor(Path(tmp) / "gateway.sqlite3", transport,
                                                platform_id=session.platform_id)
                results = []
                try:
                    transport.client.loop_stop()
                    # paho's loop_start allocates a fresh wake-up socket pair;
                    # release the paused loop's pair before starting it again.
                    transport.client._reset_sockets(sockpair_only=True)
                    transport.client.on_socket_register_write = lambda *args: None
                    worker = threading.Thread(target=lambda: results.append(executor(
                        ACTION, {"command_id": "queued-before-loss", "_receipt_command": "客厅灯60%"})))
                    worker.start()
                    eventually(lambda: bool(transport.client._out_packet), bool)
                    stop_owned_child(session, "broker")
                    transport.client.loop_start()
                    eventually(transport.client.is_connected, lambda connected: not connected)
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(results[0]["execution_state"], "unknown")
                    restart_owned_broker(session)
                    current = eventually(transport.snapshot, lambda value: isinstance(value, dict))
                    self.assertEqual(current["state_version"], 0)
                    self.assertEqual(executor.lookup("queued-before-loss")["execution_state"], "unknown")
                    self.assertFalse(any(v["operation"] == "submit" for v in envelopes(tmp)))
                finally:
                    transport.close()

    def test_close_unblocks_an_exchange_and_never_reconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id, timeout_s=4)
                stop_owned_child(session, "entity")
                responses = []
                worker = threading.Thread(target=lambda: responses.append(transport.snapshot()))
                worker.start()
                eventually(lambda: bool(transport.pending), bool)
                transport.close()
                worker.join(timeout=0.3)
                self.assertFalse(worker.is_alive(), "closing must release an outstanding receipt wait")
                self.assertEqual(responses, [None])
                self.assertIsNone(transport.snapshot())
                self.assertTrue(transport.connection_state()["closed"])
                self.assertFalse(transport.connection_state()["connected"])
                transport.close()

    def test_persistent_database_directory_survives_a_new_runtime_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, data = Path(tmp), Path(tmp) / "persistent"
            first_run, second_run = root / "first", root / "second"
            with LocalMQTTSession(first_run, initial_state=deepcopy(STATE), data_dir=data) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                try:
                    executor = SQLiteReceiptExecutor(session.environment["SENSEHOME_RECEIPT_DB"], transport,
                                                    platform_id=session.platform_id)
                    self.assertEqual(executor(ACTION, {"command_id": "persist-1", "_receipt_command": "客厅灯60%"})
                                     ["execution_state"], "applied")
                    self.assertEqual(Path(session.environment["SENSEHOME_RECEIPT_DB"]), data / "gateway.sqlite3")
                finally:
                    transport.close()
            replacement_initial_state = deepcopy(STATE)
            replacement_initial_state["lights"]["客厅"]["brightness"] = 99
            with LocalMQTTSession(second_run, initial_state=replacement_initial_state, data_dir=data) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                try:
                    current = transport.snapshot()
                    self.assertEqual(current["state_version"], 1)
                    self.assertEqual(current["software_state"]["lights"]["客厅"]["brightness"], 60)
                    executor = SQLiteReceiptExecutor(session.environment["SENSEHOME_RECEIPT_DB"], transport,
                                                    platform_id=session.platform_id)
                    self.assertEqual(executor.lookup("persist-1")["execution_state"], "applied")
                    self.assertFalse(any(v["operation"] == "submit" for v in envelopes(second_run)))
                finally:
                    transport.close()
            self.assertTrue((data / "entity.sqlite3").is_file())
            self.assertTrue((data / "gateway.sqlite3").is_file())
            self.assertFalse((first_run / "entity.sqlite3").exists())
            self.assertTrue((first_run / "cleanup.json").is_file())
            self.assertTrue((second_run / "cleanup.json").is_file())

    def test_concurrent_queries_keep_their_own_correlation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport("127.0.0.1", session.port, session.platform_id)
                executor = SQLiteReceiptExecutor(Path(tmp) / "gateway.sqlite3", transport,
                                                platform_id=session.platform_id)
                try:
                    for index in range(2):
                        action = deepcopy(ACTION)
                        action[0]["params"]["brightness"] = 20 + index
                        self.assertEqual(executor(action, {"command_id": "query-%d" % index,
                                                          "_receipt_command": "灯光测试"})["execution_state"], "applied")
                    ids = ["query-%d" % (index % 2) for index in range(20)]
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        replies = list(pool.map(transport.lookup, ids))
                    self.assertEqual([reply["command_id"] for reply in replies], ids)
                    self.assertEqual([reply["state_version_after"] for reply in replies], [1, 2] * 10)
                finally:
                    transport.close()


if __name__ == "__main__":
    unittest.main()
