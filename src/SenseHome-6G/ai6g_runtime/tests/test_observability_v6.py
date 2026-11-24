"""Meaningful observation budgets and honest latency semantics over real HTTP fixtures."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import sys
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.health_monitor import CalibrationEntry
from ai6g_runtime.models import ModelResult
from ai6g_runtime.observability_v6 import BudgetedHealthMonitor, LatencyEvidence


class Clock:
    value=1000.
    def __call__(self):return self.value

class Backend:
    def __init__(self):self.calls=0;self.fail=False;self.action=False
    def infer(self,task,timeout_s):
        self.calls+=1
        return ModelResult(not self.fail,payload={'result_kind':'action' if self.action else 'clarification',
            'actions':[{'type':'light_on','location':'客厅'}] if self.action else [],'explanation':'probe only'},
            error='failed' if self.fail else None,latency_ms=400.,model_real=not self.fail,backend='local-model',
            raw_response={'usage':{'total_tokens':25}})

class Server:
    def __init__(self):
        self.healthy=True;self.redirect=False;self.requests=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append(self.path)
                if owner.redirect:
                    self.send_response(302);self.send_header('Location',owner.url+'/target');self.end_headers();return
                self.send_response(200 if owner.healthy else 503);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'status':'ok' if owner.healthy else 'error'}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.server=Server();self.addCleanup(self.server.close)
        self.clock=Clock();self.backend=Backend();self.published=[]
        self.latency=LatencyEvidence(700.,'a'*64,990.)
        self.calibration=CalibrationEntry(.85,'b'*64,{'curtain':.9})
    def monitor(self,**kwargs):
        return BudgetedHealthMonitor(backends={'local-model':self.backend},
            health_urls={'local-model':self.server.url+'/health'},
            calibration={'local-model':self.calibration},latencies={'local-model':self.latency},
            publish=lambda name,**payload:self.published.append((name,payload)),
            clock=self.clock,monotonic=kwargs.pop('monotonic',self.clock),
            canary_ttl_s=kwargs.pop('canary_ttl_s',60.),min_canary_interval_s=kwargs.pop('min_canary_interval_s',30.),**kwargs)
    def test_passive_latency_window_is_independent_of_semantic_canary_ttl(self):
        for ttl,minimum in [(2.,2.),(60.,30.),(1e9,30.)]:
            self.clock.value=1000.
            monitor=self.monitor(canary_ttl_s=ttl,min_canary_interval_s=minimum)
            monitor.run_once();monitor.record_business('local-model',ok=True,latency_ms=1250.)
            self.clock.value=1006.
            self.assertEqual(monitor.run_once()[0]['inference_latency_estimate_ms'],1250.)
            self.clock.value=1061.
            self.assertEqual(monitor.run_once()[0]['inference_latency_estimate_ms'],700.)
    def test_record_business_does_not_wait_for_network_monitor_lock(self):
        monitor=self.monitor();finished=threading.Event()
        with monitor.lock:
            thread=threading.Thread(target=lambda:(monitor.record_business('local-model',ok=True,latency_ms=200.),finished.set()))
            thread.start();self.assertTrue(finished.wait(.3),'business completion blocked behind health network I/O')
        thread.join(timeout=1)
    def test_canary_cooldown_uses_monotonic_clock_when_wallclock_moves_backward(self):
        steady=Clock();monitor=self.monitor(monotonic=steady);monitor.run_once()
        monitor.record_business('local-model',ok=False,latency_ms=100.)
        self.clock.value=999.;steady.value=1031.
        self.assertTrue(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,2)
    def test_verified_offline_timestamp_survives_wallclock_rollback_past_measurement(self):
        steady=Clock();monitor=self.monitor(monotonic=steady);monitor.run_once()
        self.clock.value=899.;steady.value=1300.
        self.assertTrue(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,2)
    def test_twenty_healthy_ticks_only_one_real_canary_and_inference_latency_is_not_health_rtt(self):
        monitor=self.monitor()
        for _ in range(20):monitor.run_once();self.clock.value+=1
        self.assertEqual(self.backend.calls,1)
        self.assertEqual(len(self.server.requests),20)
        self.assertTrue(all(payload['latency_ms']>=700 for _,payload in self.published))
        self.assertTrue(all(payload['quality_score']==.85 for _,payload in self.published))
    def test_health_200_does_not_mask_failed_semantic_canary_or_retry_flood(self):
        self.backend.action=True;monitor=self.monitor()
        first=monitor.run_once()[0]
        for _ in range(10):self.clock.value+=1;monitor.run_once()
        self.assertFalse(first['reachable']);self.assertEqual(self.backend.calls,1)
        self.assertTrue(all(not payload['reachable'] for _,payload in self.published))
        self.clock.value=1031.;self.backend.action=False
        self.assertTrue(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,2)
    def test_backend_unhealthy_never_spends_generated_tokens_and_recovery_needs_canary(self):
        self.server.healthy=False;monitor=self.monitor()
        self.assertFalse(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,0)
        self.server.healthy=True;self.clock.value+=1
        self.assertTrue(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,1)
    def test_expired_semantic_evidence_not_rejuvenated_by_health_or_budget(self):
        monitor=self.monitor();monitor.run_once()
        self.clock.value+=61;self.backend.fail=True
        self.assertFalse(monitor.run_once()[0]['reachable'])
        self.clock.value+=1;self.backend.fail=False
        self.assertFalse(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,2)
    def test_transport_failure_requires_bounded_fresh_canary_after_recovery(self):
        monitor=self.monitor();monitor.run_once();monitor.record_business('local-model',ok=False,latency_ms=100)
        self.clock.value+=1
        self.assertFalse(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,1)
        self.clock.value+=30
        self.assertTrue(monitor.run_once()[0]['reachable']);self.assertEqual(self.backend.calls,2)
    def test_passive_task_latency_is_observed_without_redefining_model_quality(self):
        monitor=self.monitor();monitor.run_once();monitor.record_business('local-model',ok=True,latency_ms=1250.)
        self.clock.value+=2;monitor.run_once()
        self.assertGreaterEqual(self.published[-1][1]['latency_ms'],1250.)
        self.assertEqual(self.published[-1][1]['quality_score'],.85)
    def test_missing_calibration_never_publishes_or_calls_model(self):
        monitor=self.monitor();monitor.calibration.clear()
        report=monitor.run_once()[0]
        self.assertFalse(report['published']);self.assertEqual(self.backend.calls,0);self.assertEqual(self.published,[])
    def test_redirect_is_not_followed(self):
        self.server.redirect=True;report=self.monitor().run_once()[0]
        self.assertFalse(report['reachable']);self.assertNotIn('/target',self.server.requests);self.assertEqual(self.backend.calls,0)
    def test_resources_sampled_once_per_tick_for_all_models_and_unknown_preserved(self):
        class Provider:
            calls=0
            def collect(self,names):
                self.calls+=1
                return {'resources':{name:{'queue_ms':None,'free_mem_mb':None,'inflight':0,
                    'source':'fixture','observed_at':1000.} for name in names}}
        provider=Provider();monitor=self.monitor(resource_provider=provider)
        monitor.run_once();self.assertEqual(provider.calls,1)
        self.assertIsNone(self.published[-1][1]['resources']['queue_ms'])
    def test_future_latency_evidence_and_nonfinite_limits_are_rejected(self):
        with self.assertRaises(ValueError):LatencyEvidence(float('nan'),'a'*64,0)
        with self.assertRaises(ValueError):self.monitor(canary_timeout_s=float('inf'))
        monitor=self.monitor();monitor.latencies['local-model']=replace(self.latency,measured_at=2000.)
        self.assertFalse(monitor.run_once()[0]['published'])

if __name__=='__main__':unittest.main()
