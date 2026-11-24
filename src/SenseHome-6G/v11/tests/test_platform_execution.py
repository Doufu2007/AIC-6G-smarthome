"""Reliable execution semantics; HTTP fixture results are not real HA evidence."""
from pathlib import Path
from contextlib import closing
import copy
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parents[1] / 'platform_execution.py'

def load():
    assert SOURCE.exists(), 'missing durable HA execution implementation'
    spec = importlib.util.spec_from_file_location('v9_platform_under_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Fixture:
    def __init__(self):
        self.states = {name: {'entity_id':name,'state':'off','attributes':{},'last_updated':'2026-11-11T00:00:00Z'} for name in ('input_boolean.living','input_boolean.bedroom')}
        self.calls=[]
        self.mode='normal'
        self.db=None
        self.dispatch_committed=False
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                fixture.calls.append(('GET',self.path,None))
                if self.headers.get('Authorization')!='Bearer fixture-token':
                    self.send_response(401);self.end_headers();return
                entity=self.path.removeprefix('/api/states/')
                if entity not in fixture.states:
                    self.send_response(404);self.end_headers();return
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(fixture.states[entity]).encode())
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls.append(('POST',self.path,body))
                if fixture.db:
                    with closing(sqlite3.connect(fixture.db)) as db:
                        fixture.dispatch_committed=db.execute("SELECT COUNT(*) FROM steps WHERE status='dispatching'").fetchone()[0]>0
                entity=body['entity_id']
                if fixture.mode!='drop_before_apply':
                    fixture.states[entity]['state']='on' if self.path.endswith('turn_on') else 'off'
                    fixture.states[entity]['last_updated']='2026-11-11T00:00:01Z'
                if fixture.mode.startswith('drop_'):
                    self.connection.shutdown(2);self.connection.close();return
                if fixture.mode=='timeout_after_apply':
                    time.sleep(.2)
                    return
                self.send_response(200);self.end_headers()
                # Intentionally misleading response: executor must perform its own GET.
                self.wfile.write(b'[]')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.module=load()
        self.temp=tempfile.TemporaryDirectory()
        self.fixture=Fixture()
        self.db=Path(self.temp.name)/'execution.sqlite3'
        self.fixture.db=self.db
        self.clock=[100.0]
        self.client=self.module.HAClient(self.fixture.url,'fixture-token',timeout=1)
        self.store=self.module.ExecutionStore(self.db,self.client,now=lambda:self.clock[0])
        self.plan=[{'entity_id':'input_boolean.living','service':'input_boolean.turn_on','data':{}}]
        self.snapshot=self.store.snapshot(['input_boolean.living'])
    def tearDown(self):
        if hasattr(self,'fixture'):self.fixture.close()
        if hasattr(self,'temp'):self.temp.cleanup()
    def run_plan(self,request_id='one',**kwargs):
        return self.store.execute(request_id,self.plan,self.snapshot,authorized=True,**kwargs)
    def posts(self): return [x for x in self.fixture.calls if x[0]=='POST']

    def test_service_is_preceded_by_committed_intent_and_followed_by_get(self):
        result=self.run_plan()
        self.assertEqual(result['status'],'succeeded')
        self.assertTrue(self.fixture.dispatch_committed)
        self.assertEqual(self.posts(),[('POST','/api/services/input_boolean/turn_on',{'entity_id':'input_boolean.living'})])
        self.assertEqual(self.fixture.calls[-1][:2],('GET','/api/states/input_boolean.living'))
        self.assertEqual(self.fixture.states['input_boolean.living']['state'],'on')

    def test_authorization_must_be_literal_true(self):
        for value in (False,None,1,'true'):
            result=self.store.execute('unauthorized',self.plan,self.snapshot,authorized=value)
            self.assertEqual(result['status'],'rejected')
        self.assertEqual(self.posts(),[])

    def test_unknown_entity_prevents_entire_plan(self):
        self.plan.append({'entity_id':'input_boolean.missing','service':'input_boolean.turn_on','data':{}})
        self.snapshot['input_boolean.missing']={**self.snapshot['input_boolean.living'],'entity_id':'input_boolean.missing'}
        self.assertEqual(self.run_plan()['status'],'rejected')
        self.assertEqual(self.posts(),[])

    def test_stale_changed_future_and_extra_preconditions_reject_before_post(self):
        for kind in ('stale','changed','future','extra','missing'):
            original=copy.deepcopy(self.snapshot)
            if kind=='stale':self.snapshot['input_boolean.living']['observed_at']=60
            elif kind=='future':self.snapshot['input_boolean.living']['observed_at']=101
            elif kind=='changed':self.snapshot['input_boolean.living']['last_updated']='old'
            elif kind=='extra':self.snapshot['input_boolean.bedroom']=copy.deepcopy(original['input_boolean.living'])
            elif kind=='missing':self.snapshot={}
            self.assertEqual(self.run_plan(kind)['status'],'rejected',kind)
            self.snapshot=original
        self.assertEqual(self.posts(),[])

    def test_duplicate_is_durable_and_conflicting_id_cannot_execute(self):
        self.assertEqual(self.run_plan()['status'],'succeeded')
        self.store=self.module.ExecutionStore(self.db,self.client,now=lambda:100)
        duplicate=self.run_plan()
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['status'],'succeeded')
        self.plan[0]['service']='input_boolean.turn_off'
        conflict=self.run_plan()
        self.assertEqual(conflict['status'],'rejected')
        self.assertEqual(conflict['reason'],'request_id_conflict')
        self.assertEqual(len(self.posts()),1)

    def test_dropped_response_after_apply_is_verified_without_resending(self):
        self.fixture.mode='drop_after_apply'
        result=self.run_plan()
        self.assertEqual(result['status'],'verified_after_unknown')
        self.assertEqual(result['steps'][0]['verification']['state'],'on')
        self.run_plan()
        self.assertEqual(len(self.posts()),1)

    def test_http_timeout_after_apply_is_verified_by_independent_get(self):
        self.fixture.mode='timeout_after_apply'
        self.client.timeout=.1
        result=self.run_plan()
        self.assertEqual(result['status'],'verified_after_unknown')
        self.assertEqual(result['steps'][0]['error_type'],'TimeoutError')
        self.assertEqual(len(self.posts()),1)

    def test_number_targets_respect_observed_entity_range(self):
        entity='input_number.curtain'
        self.fixture.states[entity]={'entity_id':entity,'state':'0.0','attributes':{'min':0,'max':100},'last_updated':'2026-11-11T00:00:00Z'}
        self.plan=[{'entity_id':entity,'service':'input_number.set_value','data':{'value':101}}]
        self.snapshot=self.store.snapshot([entity])
        self.assertEqual(self.run_plan()['status'],'rejected')
        self.assertEqual(self.posts(),[])

    def test_dropped_before_apply_stays_unknown_and_duplicate_never_resends(self):
        self.fixture.mode='drop_before_apply'
        self.assertEqual(self.run_plan()['status'],'unknown')
        self.assertEqual(self.run_plan()['status'],'unknown')
        self.assertEqual(len(self.posts()),1)

    def test_restart_reconciles_dispatch_intent_without_service_retry(self):
        class CrashClient:
            def get_state(inner,entity):return self.client.get_state(entity)
            def call_service(inner,entity,service,data):
                self.client.call_service(entity,service,data)
                raise KeyboardInterrupt('process crash fixture')
        self.store=self.module.ExecutionStore(self.db,CrashClient(),now=lambda:100)
        with self.assertRaises(KeyboardInterrupt): self.run_plan()
        self.store=self.module.ExecutionStore(self.db,self.client,now=lambda:100)
        self.assertEqual(self.store.reconcile('one')['status'],'verified_after_unknown')
        self.assertEqual(len(self.posts()),1)

    def test_toggle_cross_domain_data_and_repeated_entity_are_rejected(self):
        variants=[
            [{'entity_id':'input_boolean.living','service':'input_boolean.toggle','data':{}}],
            [{'entity_id':'input_boolean.living','service':'light.turn_on','data':{}}],
            [{'entity_id':'input_boolean.living','service':'input_boolean.turn_on','data':{'entity_id':'input_boolean.bedroom'}}],
            self.plan+self.plan,
        ]
        for idx,plan in enumerate(variants):
            result=self.store.execute(str(idx),plan,self.snapshot,authorized=True)
            self.assertEqual(result['status'],'rejected')
        self.assertEqual(self.posts(),[])

    def test_success_response_without_target_state_is_unknown(self):
        class LyingClient:
            def get_state(inner,entity):return self.client.get_state(entity)
            def call_service(inner,entity,service,data):return []
        self.store=self.module.ExecutionStore(self.db,LyingClient(),now=lambda:100)
        self.assertEqual(self.run_plan()['status'],'unknown')

    def test_preflight_callback_cannot_mutate_journaled_plan_or_preconditions(self):
        class MutatingClient:
            def get_state(inner,entity):
                self.plan[0]['service']='input_boolean.turn_off'
                self.snapshot['input_boolean.living']['state']='on'
                return self.client.get_state(entity)
            def call_service(inner,entity,service,data):return self.client.call_service(entity,service,data)
        self.store=self.module.ExecutionStore(self.db,MutatingClient(),now=lambda:100)
        result=self.run_plan()
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.fixture.states['input_boolean.living']['state'],'on')
        self.assertEqual(self.posts()[0][1],'/api/services/input_boolean/turn_on')

    def test_second_step_expiry_or_state_change_stops_partial_plan(self):
        for kind in ('expiry','changed'):
            with self.subTest(kind=kind):
                self.fixture.states['input_boolean.living']['state']='off'
                self.fixture.states['input_boolean.bedroom']['state']='off'
                self.clock[0]=100
                plan=[{'entity_id':entity,'service':'input_boolean.turn_on','data':{}} for entity in ('input_boolean.living','input_boolean.bedroom')]
                snapshot=self.store.snapshot(['input_boolean.living','input_boolean.bedroom'])
                class ChangingClient:
                    def get_state(inner,entity):return self.client.get_state(entity)
                    def call_service(inner,entity,service,data):
                        value=self.client.call_service(entity,service,data)
                        if kind=='expiry':self.clock[0]+=31
                        else:self.fixture.states['input_boolean.bedroom']['last_updated']='concurrent-change'
                        return value
                store=self.module.ExecutionStore(Path(self.temp.name)/(kind+'.sqlite3'),ChangingClient(),now=lambda:self.clock[0])
                self.fixture.calls=[]
                result=store.execute(kind,plan,snapshot,authorized=True)
                self.assertEqual(result['status'],'unknown')
                self.assertEqual(self.fixture.states['input_boolean.bedroom']['state'],'off')
                self.assertEqual(len(self.posts()),1)
                self.assertEqual(result['steps'][1]['status'],'not_dispatched')

    def test_get_receipt_is_pure_durable_read(self):
        self.assertIsNone(self.store.get_receipt('never-seen'))
        self.run_plan()
        self.fixture.calls=[]
        receipt=self.store.get_receipt('one')
        self.assertEqual(receipt['status'],'succeeded')
        self.assertEqual(self.fixture.calls,[])

    def test_precondition_that_expires_during_get_cannot_be_dispatched(self):
        class SlowGetClient:
            calls=0
            def get_state(inner,entity):
                state=self.client.get_state(entity)
                inner.calls+=1
                if inner.calls==2:self.clock[0]+=31
                return state
            def call_service(inner,entity,service,data):return self.client.call_service(entity,service,data)
        self.store=self.module.ExecutionStore(self.db,SlowGetClient(),now=lambda:self.clock[0])
        result=self.run_plan()
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(self.posts(),[])

if __name__=='__main__': unittest.main(verbosity=2)
