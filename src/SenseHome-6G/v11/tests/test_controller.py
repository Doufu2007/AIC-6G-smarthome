import copy
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from controller import Controller, SoftwareClient


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.catalog=[dict(entity_id="light.read",kind="light",room="书房",name="阅读灯",aliases=[],available=True)]
        self.client=SoftwareClient({"light.read":dict(state="off",attributes={},last_updated="v0")})
        self.app=Controller(pathlib.Path(self.temp.name),self.catalog,self.client)

    def tearDown(self):self.temp.cleanup()

    def propose(self):return self.app.propose("打开阅读灯")

    def confirm(self,card,**extra):
        return self.app.confirm(dict(proposal_id=card["proposal_id"],digest=card["digest"],request_id="one",confirmed=True,**extra))

    def test_proposal_confirmation_and_receipt_are_one_chain(self):
        card=self.propose()
        self.assertEqual(self.client.get_state("light.read")["state"],"off")
        result=self.confirm(card)
        self.assertEqual(result["status"],"succeeded")
        self.assertEqual(self.client.get_state("light.read")["state"],"on")
        self.assertEqual(self.client.calls,1)
        replay=self.confirm(card)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(self.client.calls,1)

    def test_client_cannot_supply_plan_or_authority(self):
        card=self.propose()
        for extra in ({"plan":[]},{"authorized":True},{"expected_states":{}}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.confirm(card,**extra)
        self.assertEqual(self.client.calls,0)

    def test_request_id_collision_never_replays_another_card_receipt(self):
        first=self.propose();self.confirm(first)
        second=self.app.propose("关闭阅读灯")
        for reopen in (False,False,True):
            if reopen:self.app=Controller(pathlib.Path(self.temp.name),self.catalog,self.client)
            rejected=self.confirm(second)
            self.assertEqual(rejected["status"],"rejected")
            self.assertEqual(rejected["reason"],"request_id_conflict")
            self.assertEqual(self.client.get_state("light.read")["state"],"on")
        self.assertEqual(self.client.calls,1)

    def test_different_cards_same_request_id_across_controllers(self):
        from concurrent.futures import ThreadPoolExecutor
        first=self.propose();second=self.app.propose("打开阅读灯")
        other=Controller(pathlib.Path(self.temp.name),self.catalog,self.client)
        def do(app,card):
            return app.confirm(dict(proposal_id=card['proposal_id'],digest=card['digest'],request_id='shared',confirmed=True))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda pair:do(*pair),[(self.app,first),(other,second)]))
        self.assertEqual(sorted(r['status'] for r in results),['rejected','succeeded'])
        self.assertEqual(self.client.calls,1)

    def test_same_card_concurrent_slow_snapshot_returns_owned_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        card=self.propose();other=Controller(pathlib.Path(self.temp.name),self.catalog,self.client)
        entered=Event();release=Event();original=other.execution.snapshot
        def slow_snapshot(ids):
            entered.set()
            if not release.wait(5):raise RuntimeError('test barrier timeout')
            return original(ids)
        other.execution.snapshot=slow_snapshot
        body=dict(proposal_id=card['proposal_id'],digest=card['digest'],request_id='same-card',confirmed=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(other.confirm,body)
            self.assertTrue(entered.wait(5))
            try:self.assertEqual(self.app.confirm(body)['status'],'succeeded')
            finally:release.set()
            result=future.result(timeout=5)
        self.assertEqual(result['status'],'succeeded')
        self.assertTrue(result['duplicate'])
        self.assertEqual(self.client.calls,1)

    def test_changed_card_does_not_change_stored_plan(self):
        card=self.propose();card["payload"]["actions"][0]["service"]="light.turn_off"
        self.confirm(card)
        self.assertEqual(self.client.get_state("light.read")["state"],"on")

    def test_state_change_after_proposal_requires_new_confirmation(self):
        card=self.propose()
        self.client.call_service("light.read","light.turn_on",{})
        with self.assertRaisesRegex(ValueError,"state changed"):self.confirm(card)
        self.assertEqual(self.client.calls,1)

    def test_ambiguous_or_invalid_model_payload_stays_non_executable(self):
        self.app.planner=lambda view:{"result_kind":"clarification","actions":[],"reason":"请明确"}
        result=self.propose();self.assertEqual(result["status"],"clarification")
        self.app.planner=lambda view:{"result_kind":"proposal","actions":[{"entity_id":"light.other","service":"light.turn_on","data":{}}],"reason":"x"}
        # v10 returns a non-executable structured clarification for malformed
        # extension/model output instead of surfacing a parser exception.
        invalid=self.propose()
        self.assertEqual(invalid["status"],"clarification")
        self.assertEqual(invalid["payload"]["actions"],[])
        self.assertEqual(self.client.calls,0)


if __name__=="__main__":unittest.main()
