import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('v4_runner', HERE / 'run_comparison.py')
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

class DesignTests(unittest.TestCase):
    def test_schedule_covers_all_arms_and_conditions_without_duplicate_rows(self):
        self.assertTrue(callable(getattr(v, 'schedule', None)), 'registered schedule missing')
        jobs=v.schedule()
        primary=[j for j in jobs if j['cohort']=='reviewed_complex_regression']
        ancillary=[j for j in jobs if j['cohort']=='reviewed_empty_plan_regression']
        self.assertEqual(len(primary),24)
        self.assertEqual(len(ancillary),8)
        keys=[(j['condition'],j['case_id'],a) for j in jobs for a in j['arms']]
        self.assertEqual(len(keys),96)
        self.assertEqual(len(set(keys)),96)
        for job in jobs:
            self.assertEqual(set(job['arms']), {'local-first','second-first','joint'})
        self.assertEqual({j['case_id'] for j in primary},{'C01','C02','C03','C04'})
        for arm in ('local-first','second-first','joint'):
            self.assertEqual(sum(j['arms'][0]==arm for j in primary),8)

    def test_recommendation_prioritizes_completion_and_keeps_failed_latency(self):
        self.assertTrue(callable(getattr(v, 'summarize', None)), 'summary missing')
        rows=[]
        for arm,ok,ms in [('local-first',True,1500),('second-first',False,1),('joint',True,1000)]:
            rows.append({'cohort':'reviewed_complex_regression','condition':'healthy','arm':arm,
                'status':'finished','task_completed':ok,'executor_calls':int(ok),
                'timing_ms':{'total_wall':ms},'backend_attempts':[],'proxy_records':[],
                'safe_no_action_structure':False})
        report=v.summarize(rows)
        self.assertEqual(report['recommendation']['arm'],'joint')
        self.assertEqual(report['by_arm']['second-first']['median_total_wall_ms_all_requests'],1)
        self.assertEqual(report['by_arm']['second-first']['n'],1)
        self.assertEqual(report['by_arm']['second-first']['completed'],0)

if __name__=='__main__': unittest.main()

class VerificationTests(unittest.TestCase):
    def test_artifact_tampering_is_rejected_before_model_start(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp); (base/'source.txt').write_text('frozen')
            reg={'source_sha256':{'source.txt':v.digest(base/'source.txt')}}
            old=v.HERE
            try:
                v.HERE=base
                v.assert_sources(reg)
                (base/'source.txt').write_text('changed')
                with self.assertRaisesRegex(ValueError,'REGISTERED_SOURCE_CHANGED'):
                    v.assert_sources(reg)
            finally: v.HERE=old

    def test_schedule_order_matches_condition_grouped_execution(self):
        indices=[v.CONDITIONS.index(j['condition']) for j in v.schedule()]
        self.assertEqual(indices,sorted(indices),'registration must equal actual execution order')

    def test_loopback_fault_recovers_once_and_retains_raw_post_usage(self):
        import http.server
        import threading
        from contextlib import ExitStack
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send(200,{'data':[{'id':'fixture-model'}]})
            def do_POST(self):
                import json
                self.server.received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send(200,{'model':'fixture-model','choices':[{'message':{'content':json.dumps({
                    'answer':'已规划','actions':[{'name':'light_control','params':{'location':'客厅','brightness':60}}]})}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
            def send(self,status,value):
                import json
                raw=json.dumps(value).encode()
                self.send_response(status); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.received=[]
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with ExitStack() as stack:
                upstream=f'http://127.0.0.1:{server.server_port}/v1'
                proxies=[stack.enter_context(v.ControlledProxy(upstream)) for _ in range(2)]
                backends=[v.InventoryQueueBackend(p,'fixture-model',t) for p,t in zip(proxies,('local','cloud'))]
                proxies[0].mode='http_503'
                wrapper=v.PlanningRecoveryBackend(backends[0],backends[1],primary_name='local-model',secondary_name='cloud-model',
                    primary_url=proxies[0].url,secondary_url=proxies[1].url,launch_budget_ms=10000)
                result=wrapper.infer('客厅有点暗')
                self.assertTrue(result.ok)
                self.assertEqual(len(server.received),1)
                self.assertEqual(wrapper.recovery_records[0]['reason'],'secondary_success')
                self.assertEqual(len(backends[0].attempts),1)
                self.assertEqual(len(backends[1].attempts),1)
                record=backends[1].attempts[0]['upstream_exchanges'][0]
                self.assertEqual(record['response']['usage']['total_tokens'],15)
                self.assertEqual(server.received[0]['temperature'],0)
                self.assertEqual(server.received[0]['max_tokens'],256)
                self.assertTrue(proxies[0].records[-1]['http_status']==503)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)
