"""Regression tests for observed product failures; software fixtures only."""
import copy
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from controller import Controller, SoftwareClient
from platform_execution import ExecutionStore, NotDispatched


def catalog(count=1):
    return [dict(entity_id=f'light.l{i}', kind='light', room='书房',
                 name=f'阅读灯{i}', aliases=[], available=True) for i in range(count)]


def states(entries):
    return {e['entity_id']:dict(state='off',attributes={},last_updated='v0') for e in entries}


def body(card, request_id='repair'):
    return dict(proposal_id=card['proposal_id'], digest=card['digest'],
                request_id=request_id, confirmed=True)


class ProductRepairs(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_failure_returns_durable_success_and_replay(self):
        entries=catalog();client=SoftwareClient(states(entries))
        app=Controller(self.home,entries,client);card=app.propose('打开阅读灯0')
        app.audit_path=self.home/'blocked';app.audit_path.mkdir()
        for _ in range(2):
            result=app.confirm(body(card))
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(result['audit_status']['status'],'failed')
            self.assertEqual(result['audit_status']['error_type'],'PermissionError')
        self.assertEqual(client.calls,1)
        self.assertEqual(app.receipt('repair')['status'],'succeeded')

    def test_audit_failure_does_not_make_proposal_inaccessible(self):
        entries=catalog();app=Controller(self.home,entries,SoftwareClient(states(entries)))
        app.audit_path=self.home/'blocked';app.audit_path.mkdir()
        card=app.propose('打开阅读灯0')
        self.assertEqual(card['status'],'awaiting_confirmation')
        self.assertEqual(card['audit_status']['status'],'failed')
        self.assertEqual(app.confirm(body(card))['status'],'succeeded')

    def test_software_checked_write_compares_state_inside_transaction(self):
        entries=catalog();initial=states(entries)
        client=SoftwareClient(initial,self.home/'shared.json')
        other=SoftwareClient(initial,self.home/'shared.json')
        expected=client.get_state('light.l0')
        other.call_service('light.l0','light.turn_off',{})
        with self.assertRaises(NotDispatched):
            client.call_service_checked('light.l0','light.turn_on',{},state=expected,
                                        authorization_expires_at=time.time()+20,now=time.time)
        self.assertEqual(client.calls,0)
        self.assertEqual(other.get_state('light.l0')['state'],'off')

    def test_software_checked_memory_write_compares_state_and_capability(self):
        entries=catalog();client=SoftwareClient(states(entries));expected=client.get_state('light.l0')
        client.states['light.l0']['attributes']['brightness_supported']=False
        with self.assertRaises(NotDispatched):
            client.call_service_checked('light.l0','light.turn_on',{'brightness_pct':50},state=expected,
                                        authorization_expires_at=time.time()+20,now=time.time)
        self.assertEqual(client.calls,0)

    def test_missing_or_malformed_state_before_checked_write_is_not_dispatched(self):
        entries=catalog()
        for mutation in ('delete','attributes_none'):
            with self.subTest(mutation=mutation):
                client=SoftwareClient(states(entries));expected=client.get_state('light.l0')
                if mutation=='delete':del client.states['light.l0']
                else:client.states['light.l0']['attributes']=None
                with self.assertRaises(NotDispatched):
                    client.call_service_checked('light.l0','light.turn_on',{},state=expected,
                                                authorization_expires_at=time.time()+20,now=time.time)
                self.assertEqual(client.calls,0)

    def test_concurrent_update_after_execution_precheck_is_not_dispatched(self):
        entries=catalog();initial=states(entries);entered=threading.Event();release=threading.Event()
        class PausedClient(SoftwareClient):
            def call_service_checked(self,*args,**kwargs):
                entered.set()
                if not release.wait(3):raise TimeoutError('fixture barrier')
                return super().call_service_checked(*args,**kwargs)
        client=PausedClient(initial,self.home/'shared.json');writer=SoftwareClient(initial,self.home/'shared.json')
        store=ExecutionStore(self.home/'execution.sqlite',client)
        expected=store.snapshot(['light.l0'])
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(store.execute,'race',[dict(entity_id='light.l0',service='light.turn_on',data={})],
                                   expected,authorized=True,authorization_expires_at=time.time()+20)
            self.assertTrue(entered.wait(2))
            try:writer.call_service('light.l0','light.turn_off',{})
            finally:release.set()
            receipt=future.result(timeout=3)
        self.assertEqual(receipt['status'],'rejected')
        self.assertEqual(receipt['steps'][0]['status'],'not_dispatched')
        self.assertEqual(client.calls,0)
        self.assertEqual(writer.get_state('light.l0')['state'],'off')

    def test_one_target_reads_only_dependency_twice(self):
        entries=catalog(100)
        class CountingClient(SoftwareClient):
            def get_state(self,entity):
                self.reads.append(entity)
                return super().get_state(entity)
        client=CountingClient(states(entries));client.reads=[]
        app=Controller(self.home,entries,client)
        card=app.propose('打开阅读灯0')
        self.assertEqual(card['status'],'awaiting_confirmation')
        self.assertEqual(client.reads,['light.l0','light.l0'])

    def test_changed_unselected_dynamic_member_requires_new_proposal(self):
        entries=catalog(2);initial=states(entries);initial['light.l0']['state']='on'
        client=SoftwareClient(initial);app=Controller(self.home,entries,client)
        card=app.propose('关闭书房当前亮着的灯')
        self.assertEqual(set(card['dependency_states']),{'light.l0','light.l1'})
        self.assertEqual(set(card['states']),{'light.l0'})
        client.call_service('light.l1','light.turn_on',{})
        with self.assertRaisesRegex(ValueError,'dependenc'):
            app.confirm(body(card))
        self.assertEqual(client.calls,1)
        self.assertEqual(client.get_state('light.l0')['state'],'on')

    def test_unselected_dependency_read_error_blocks_confirmation(self):
        entries=catalog(2);initial=states(entries);initial['light.l0']['state']='on'
        client=SoftwareClient(initial);app=Controller(self.home,entries,client)
        card=app.propose('关闭书房当前亮着的灯')
        client.states['light.l1']['read_error_type']='TimeoutError'
        with self.assertRaisesRegex(ValueError,'dependenc'):
            app.confirm(body(card))
        self.assertEqual(client.calls,0)

    def test_model_outside_rule_grammar_is_explicitly_reviewable_and_confirmable(self):
        entries=catalog();client=SoftwareClient(states(entries))
        def model(view):
            return dict(result_kind='proposal',actions=[dict(entity_id='light.l0',
                        service='light.turn_on',data={})],reason='根据阅读需求建议打开阅读灯')
        app=Controller(self.home,entries,client,planner=model,allow_model_proposals=True)
        card=app.propose('我想看书，帮我点亮适合阅读的那盏灯')
        self.assertEqual(card['status'],'awaiting_confirmation')
        self.assertTrue(card['metadata']['intent_review_required'])
        self.assertEqual(card['metadata']['semantic_mode'],'model_proposal_with_explicit_review')
        self.assertFalse(card['authorized']);self.assertEqual(client.calls,0)
        self.assertEqual(app.confirm(body(card))['status'],'succeeded')

    def test_finite_model_extension_reads_only_named_dependency(self):
        entries=catalog(3)
        class CountingClient(SoftwareClient):
            def __init__(self, initial):
                super().__init__(initial)
                self.reads=[]
            def get_state(self, entity):
                self.reads.append(entity)
                return super().get_state(entity)
        client=CountingClient(states(entries))
        def model(view):
            return dict(result_kind='proposal',actions=[dict(entity_id='light.l0',
                service='light.turn_on',data={})],reason='explicit immediate effect')
        app=Controller(self.home,entries,client,planner=model,allow_model_proposals=True)
        card=app.propose('让阅读灯0亮起来')
        self.assertEqual(card['status'],'awaiting_confirmation')
        self.assertTrue(card['metadata']['intent_review_required'])
        self.assertEqual(client.reads,['light.l0','light.l0'])
        self.assertEqual(client.calls,0)

    def test_default_mode_still_rejects_model_outside_rule_grammar(self):
        entries=catalog();client=SoftwareClient(states(entries))
        model=lambda view:dict(result_kind='proposal',actions=[dict(entity_id='light.l0',service='light.turn_on',data={})],reason='x')
        app=Controller(self.home,entries,client,planner=model)
        self.assertEqual(app.propose('这里太暗了')['status'],'clarification')
        self.assertEqual(client.calls,0)

    def test_slow_planning_does_not_block_confirmation(self):
        entries=catalog();client=SoftwareClient(states(entries));app=Controller(self.home,entries,client)
        card=app.propose('打开阅读灯0');entered=threading.Event();release=threading.Event()
        from catalog_planner import catalog_rules
        def slow(view):
            entered.set()
            if not release.wait(3):raise TimeoutError('fixture barrier')
            return catalog_rules(view)
        app.planner=slow
        with ThreadPoolExecutor(max_workers=2) as executor:
            planning=executor.submit(app.propose,'打开阅读灯0')
            self.assertTrue(entered.wait(2))
            confirming=executor.submit(app.confirm,body(card))
            try:
                self.assertEqual(confirming.result(timeout=.5)['status'],'succeeded')
            finally:release.set()
            planning.result(timeout=3)

    def test_snapshot_deadline_bounds_stuck_read_and_preserves_other_entity(self):
        entries=catalog(2);release=threading.Event()
        class SlowClient(SoftwareClient):
            def get_state(self,entity):
                if entity=='light.l0':release.wait(3)
                return super().get_state(entity)
        client=SlowClient(states(entries))
        app=Controller(self.home,entries,client,snapshot_timeout_seconds=.1)
        try:
            start=time.monotonic();snapshot=app.snapshot()
            self.assertLess(time.monotonic()-start,.6)
            self.assertEqual(snapshot['light.l0']['state'],'unavailable')
            self.assertEqual(snapshot['light.l1']['state'],'off')
        finally:release.set()


if __name__=='__main__':unittest.main()
