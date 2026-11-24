import copy
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from proposal import ProposalStore, validate_payload


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = 1000.0
        self.catalog = [{"entity_id": "light.read", "kind": "light", "room": "阅览间", "name": "晨读灯", "aliases": ["阅读灯"], "available": True}]
        self.states = {"light.read": {"state": "off", "attributes": {}, "last_updated": "t0", "observed_at": self.t}}
        self.payload = {"result_kind": "proposal", "actions": [{"entity_id": "light.read", "service": "light.turn_on", "data": {"brightness_pct": 35}}], "reason": "明确的阅读灯请求"}
        self.store = ProposalStore(pathlib.Path(self.tmp.name) / "proposals.sqlite", now=lambda: self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self):
        return self.store.create("阅读灯调到35%", self.catalog, self.states, self.payload)

    def confirm(self, card, **changes):
        args = dict(proposal_id=card["proposal_id"], digest=card["digest"], request_id="r1", confirmed=True,
                    current_catalog=self.catalog, current_states=self.states)
        args.update(changes)
        return self.store.confirm(**args)

    def test_proposal_is_explicitly_not_authorized(self):
        card = self.create()
        self.assertFalse(card["authorized"])
        self.assertEqual(card["status"], "awaiting_confirmation")

    def test_confirmation_binds_request_and_survives_reopen(self):
        card = self.create()
        accepted = self.confirm(card)
        self.assertTrue(accepted["authorized"])
        self.assertEqual(accepted["plan"], self.payload["actions"])
        self.store = ProposalStore(self.store.path, now=lambda: self.t)
        self.assertTrue(self.confirm(card)["duplicate_confirmation"])
        with self.assertRaisesRegex(ValueError, "request"):
            self.confirm(card, request_id="r2")

    def test_digest_or_nonboolean_confirmation_rejected(self):
        card = self.create()
        for change in ({"digest": "0"*64}, {"confirmed": "true"}, {"confirmed": 1}, {"confirmed": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.confirm(card, **change)

    def test_catalog_and_state_change_rejected(self):
        card = self.create()
        changed = copy.deepcopy(self.catalog); changed[0]["entity_id"] = "light.other"
        with self.assertRaisesRegex(ValueError, "catalog"):
            self.confirm(card, current_catalog=changed)
        states = copy.deepcopy(self.states); states["light.read"]["last_updated"] = "t1"
        with self.assertRaisesRegex(ValueError, "state"):
            self.confirm(card, current_states=states)

    def test_expired_and_future_snapshot_rejected(self):
        card = self.create(); self.t += 31
        with self.assertRaisesRegex(ValueError, "expired"):
            self.confirm(card)
        self.t = 1000
        states = copy.deepcopy(self.states); states["light.read"]["observed_at"] += 10
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.store.create("开灯", self.catalog, states, self.payload)

    def test_untrusted_entities_toggle_and_extra_fields_rejected(self):
        bad_actions = [
            {"entity_id": "light.unknown", "service": "light.turn_on", "data": {}},
            {"entity_id": "light.read", "service": "light.toggle", "data": {}},
            {"entity_id": "light.read", "service": "light.turn_on", "data": {"brightness_pct": True}},
            {"entity_id": "light.read", "service": "light.turn_off", "data": {"brightness_pct": 20}},
            {"entity_id": "light.read", "service": "light.turn_on", "data": {}, "target": "all"},
        ]
        for action in bad_actions:
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_payload({"result_kind": "proposal", "actions": [action], "reason": "x"}, self.catalog)

    def test_clarification_cannot_create_an_executable_card(self):
        payload = {"result_kind": "clarification", "actions": [], "reason": "请选择房间"}
        self.assertEqual(validate_payload(payload, self.catalog)["actions"], [])
        with self.assertRaisesRegex(ValueError, "clarification"):
            self.store.create("开那盏灯", self.catalog, self.states, payload)

    def test_duplicate_entity_and_unavailable_rejected(self):
        payload = copy.deepcopy(self.payload); payload["actions"] *= 2
        with self.assertRaises(ValueError): validate_payload(payload, self.catalog)
        catalog = copy.deepcopy(self.catalog); catalog[0]["available"] = False
        with self.assertRaises(ValueError): validate_payload(self.payload, catalog)


if __name__ == "__main__": unittest.main()
