"""Failures here mean duplicate sends or unbound execution claims are possible."""
from copy import deepcopy
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

NET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NET))

ACTIONS = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]


class SoftwareTransport:
    """Controlled transport boundary; the actual MQTT path has its own tests."""
    def __init__(self):
        self.receipts = {}
        self.publications = 0
        self.queries = 0
        self.lose_ack = False
        self.crash = False
        self.tamper = None
        self.partial = False

    def submit(self, payload):
        self.publications += 1
        if self.crash:
            raise KeyboardInterrupt("simulated interruption after durable reservation")
        n = 1 if self.partial else len(payload["actions"])
        receipt = {"schema": "sensehome.execution-receipt.v1", "evidence_kind": "mqtt-software-entity",
                   "command_id": payload["command_id"], "platform_id": payload["platform_id"],
                   "request_sha256": payload["request_sha256"], "requested_actions": deepcopy(payload["actions"]),
                   "status": "partial" if self.partial else "applied", "completed_indices": list(range(n)),
                   "state_version_before": 0, "state_version_after": 1,
                   "result": {"actions": [dict(a, description="软件状态已提交") for a in payload["actions"][:n]],
                              "observation": "软件实体状态", "semantic_complete": not self.partial}}
        if self.tamper:
            self.tamper(receipt)
        self.receipts[payload["command_id"]] = receipt
        return None if self.lose_ack else deepcopy(receipt)

    def lookup(self, command_id):
        self.queries += 1
        return deepcopy(self.receipts.get(command_id))


class DurableReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gateway.sqlite3"
        self.transport = SoftwareTransport()
        if importlib.util.find_spec("durable_receipts") is None:
            self.fail("missing durable SQLite execution boundary")
        from durable_receipts import SQLiteReceiptExecutor
        self.Executor = SQLiteReceiptExecutor

    def tearDown(self):
        self.tmp.cleanup()

    def executor(self, **kw):
        return self.Executor(self.path, self.transport, platform_id=kw.get("platform_id", "software-home"))

    def context(self, **kw):
        return {"command_id": "cmd-1", "_receipt_command": "客厅有点暗", **kw}

    def test_duplicate_and_restart_return_durable_receipt_without_second_publication(self):
        first = self.executor()(ACTIONS, self.context())
        second = self.executor()(ACTIONS, self.context())
        self.assertTrue(first["semantic_complete"])
        self.assertEqual(second["execution_state"], "applied")
        self.assertEqual(self.transport.publications, 1)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("select status, request_sha256 from commands").fetchone()
        self.assertEqual(row[0], "applied")
        self.assertEqual(len(row[1]), 64)

    def test_same_id_different_actions_command_or_platform_conflicts(self):
        self.executor()(ACTIONS, self.context())
        for actions, context, platform in [
            ([{"name": "light_control", "params": {"location": "客厅", "brightness": 20}}], self.context(), "software-home"),
            (ACTIONS, self.context(_receipt_command="另一条指令"), "software-home"),
            (ACTIONS, self.context(), "other-platform"),
        ]:
            with self.subTest(platform=platform, context=context, actions=actions):
                result = self.executor(platform_id=platform)(actions, context)
                self.assertEqual(result["execution_state"], "conflict")
                self.assertFalse(result["semantic_complete"])
        self.assertEqual(self.transport.publications, 1)

    def test_ack_loss_is_query_only_on_next_call(self):
        self.transport.lose_ack = True
        executor = self.executor()
        uncertain = executor(ACTIONS, self.context())
        self.assertEqual(uncertain["execution_state"], "unknown")
        self.assertFalse(uncertain["semantic_complete"])
        recovered = executor(ACTIONS, self.context())
        self.assertEqual(recovered["execution_state"], "applied")
        self.assertEqual(self.transport.publications, 1)
        self.assertEqual(self.transport.queries, 1)

    def test_process_interruption_persists_unknown_and_restart_never_resends(self):
        self.transport.crash = True
        with self.assertRaises(KeyboardInterrupt):
            self.executor()(ACTIONS, self.context())
        self.transport.crash = False
        result = self.executor()(ACTIONS, self.context())
        self.assertEqual(result["execution_state"], "unknown")
        self.assertEqual(self.transport.publications, 1)
        self.assertEqual(self.transport.queries, 1)

    def test_partial_result_keeps_completed_subset_and_does_not_run_remaining_action(self):
        actions = ACTIONS + [{"name": "curtain_control", "params": {"location": "客厅", "position": 20}}]
        self.transport.partial = True
        result = self.executor()(actions, self.context())
        duplicate = self.executor()(actions, self.context())
        self.assertEqual(result["execution_state"], "partial")
        self.assertFalse(result["semantic_complete"])
        self.assertEqual(len(duplicate["actions"]), 1)
        self.assertEqual(self.transport.publications, 1)

    def test_contradictory_receipts_cannot_confirm_execution(self):
        for mutation in [
            lambda r: r.update(platform_id="other"),
            lambda r: r.update(command_id="wrong-id"),
            lambda r: r.update(request_sha256="0" * 64),
            lambda r: r["requested_actions"][0]["params"].update(brightness=3),
            lambda r: r["result"]["actions"][0]["params"].update(brightness=3),
            lambda r: r.update(completed_indices=[]),
            lambda r: r.update(state_version_after=0),
            lambda r: r['result'].update(semantic_complete=False),
            lambda r: r.update(error='CONTRADICTORY_FAILURE'),
        ]:
            with self.subTest(mutation=mutation):
                self.path = Path(self.tmp.name) / ("case-%d.sqlite3" % self.transport.publications)
                self.transport.tamper = mutation
                result = self.executor()(ACTIONS, self.context())
                self.assertEqual(result["execution_state"], "unknown")
                self.assertFalse(result["semantic_complete"])
                self.assertEqual(result["actions"], [])

    def test_query_unknown_id_never_creates_command_or_sends(self):
        result = self.executor().lookup("not-seen")
        self.assertEqual(result["execution_state"], "unknown")
        self.assertEqual(self.transport.publications, 0)


if __name__ == "__main__":
    unittest.main()
