"""v6 production wiring over real loopback HTTP fixtures; never loads GPU models."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
try:
    profile = importlib.import_module('ai6g_runtime.deploy.local_profile_v6')
except ModuleNotFoundError:
    profile = None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send({'status': 'ok'} if self.path == '/health' else
                   [{'id': 0, 'is_processing': False}])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(body)
        text = body['messages'][-1]['content']
        if 'AI6G_HEALTH_PROBE' in text:
            payload = self.server.canary
        else:
            time.sleep(self.server.business_delay)
            payload = self.server.business
        self._send({'id': 'v6-http-fixture', 'model': body['model'],
                    'choices': [{'message': {'content': json.dumps(payload)}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}})

    def _send(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class LocalProfileV6Tests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(profile, 'v6 production deployment entry point must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.servers, services = [], []
        for name, model in [('local-small', 'fixture-1.5B'), ('local-large', 'fixture-4B')]:
            server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
            server.calls, server.business_delay = [], 0.
            server.canary = {'result_kind': 'clarification', 'actions': [], 'explanation': 'fixture canary'}
            server.business = {'result_kind': 'action', 'actions': [
                {'type': 'set_brightness', 'location': '客厅', 'brightness': 37}], 'explanation': 'fixture'}
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            self.servers.append(server)
            origin = f'http://127.0.0.1:{server.server_port}'
            services.append(dict(name=name, model=model, endpoint=origin+'/v1/chat/completions',
                                 health_url=origin+'/health', slots_url=origin+'/slots',
                                 weight_sha256='b'*64, parallel=1, license='Apache-2.0'))
        self.prompt = self.base/'prompt.txt'
        self.prompt.write_text('Fixed fixture household protocol.\n', encoding='utf8')
        self.endpoints = self.base/'endpoints.json'
        endpoints = dict(schema='sensehome.deployment_identity.v5', project_version=6, services=services)
        self.endpoints.write_text(json.dumps(endpoints), encoding='utf8')
        manifest = dict(schema='ai6g.model_capture.manifest.v5', deployment_identity=endpoints,
                        deployment_identity_sha256=sha(self.endpoints), backends={}, decoding={})
        for alias, service in zip(('local-model', 'cloud-model'), services):
            manifest['backends'][alias] = dict(model=service['model'], endpoint=service['endpoint'],
                prompt_sha256=canonical(self.prompt.read_text().strip()), max_tokens=384)
            manifest['decoding'][alias] = dict(temperature=0, response_format='json_object', seed=20261104, max_tokens=384)
        self.sidecar = dict(fingerprint=canonical(manifest), manifest=manifest)
        self.manifest = self.base/'calibration_capture.manifest.json'
        self.manifest.write_text(json.dumps(self.sidecar), encoding='utf8')
        self.capture = self.base/'calibration_capture.jsonl'
        self.capture.write_text(json.dumps(dict(capture_fingerprint=self.sidecar['fingerprint']))+'\n', encoding='utf8')
        source = sha(self.capture)
        self.calibration = self.base/'calibration_runtime.json'
        self.payload = dict(schema='ai6g.family_calibration.v5', project_version=6, source_sha256=source,
            capture_manifest=self.sidecar, measured_at=time.time()-10, backends={alias: dict(
                quality_score=.95, source_sha256=source, quality_by_family={'lighting': .95},
                latency_p95_ms=700.) for alias in ('local-model', 'cloud-model')},
            scoring_pipeline=dict(kind='offline_projection_of_captured_output_through_shared_grounding_guard',
                guard_sha256=sha(ROOT/'grounding_v6.py'), script_sha256='c'*64,
                quality_population='public model-eligible classifier subset',
                actual_model_dispatches_in_this_projection=0, capture_original_unchanged=True))
        self.write_calibration()

    def write_calibration(self):
        self.calibration.write_text(json.dumps(self.payload), encoding='utf8')

    def load(self):
        return profile.load_profile(self.endpoints, self.prompt, self.calibration)

    def app(self, directory='run', **kwargs):
        app = profile.LocalProfileServer(self.load(), state_dir=self.base/directory, port=0, **kwargs)
        self.addCleanup(app.close)
        return app

    @staticmethod
    def wait_for(predicate, seconds=4):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('fixture observation deadline exceeded')

    def test_binds_runtime_guard_latency_and_original_capture_identity(self):
        loaded = self.load()
        self.assertEqual(loaded.latencies['cloud-model'].latency_ms, 700.)
        self.assertEqual(loaded.latencies['cloud-model'].source_sha256, sha(self.capture))
        self.assertEqual(loaded.services['local-model']['health_url'], self.endpoints_payload()['services'][0]['health_url'])
        self.assertEqual(loaded.metadata['guard_sha256'], sha(ROOT/'grounding_v6.py'))

    def endpoints_payload(self):
        return json.loads(self.endpoints.read_text(encoding='utf8'))

    def test_stale_guard_missing_pipeline_or_invalid_latency_is_rejected(self):
        original = copy.deepcopy(self.payload)
        for mutate in [lambda p: p['scoring_pipeline'].update(guard_sha256='f'*64),
                       lambda p: p.pop('scoring_pipeline'),
                       lambda p: p['backends']['local-model'].pop('latency_p95_ms'),
                       lambda p: p['backends']['local-model'].update(latency_p95_ms=float('nan')),
                       lambda p: p.update(measured_at=time.time()+60),
                       lambda p: p['capture_manifest'].update(fingerprint='f'*64)]:
            self.payload = copy.deepcopy(original)
            mutate(self.payload)
            self.write_calibration()
            with self.subTest(payload=self.payload), self.assertRaises(ValueError):
                self.load()

    def test_changed_endpoint_identity_cannot_borrow_old_calibration(self):
        payload = self.endpoints_payload()
        payload['services'][0]['parallel'] = 2
        self.endpoints.write_text(json.dumps(payload), encoding='utf8')
        with self.assertRaises(ValueError):
            self.load()

    def test_real_budget_monitor_has_one_canary_per_model_and_keeps_inference_p95(self):
        gpu = subprocess.CompletedProcess([], 0, '0, GPU-fixture, Fixture GPU, 2000, 6000\n', '')
        with patch('ai6g_runtime.local_resources.subprocess.run', return_value=gpu), \
             patch('ai6g_runtime.serve.HealthMonitor', side_effect=AssertionError('legacy monitor forbidden')):
            app = self.app(probe_interval_s=.03, canary_ttl_s=60., min_canary_interval_s=30.)
            app.start()
            self.wait_for(lambda: app.monitor.totals['ticks'] >= 4 and app.monitor.totals['health_http_requests'] >= 8)
            self.assertEqual([len(server.calls) for server in self.servers], [1, 1])
            self.assertEqual(app.monitor.totals['generated_canary_requests'], 2)
            self.assertGreaterEqual(app.monitor.totals['health_http_requests'], 8)
            self.assertEqual(app.runtime.router.max_inflight, 1)
            for name in ('local-model', 'cloud-model'):
                obs = app.runtime.router.cache.get(name)
                self.assertTrue(obs.reachable)
                self.assertEqual(obs.latency_ms, 700.)
                self.assertEqual(obs.resources.free_mem_mb, 2000.)
                self.assertIsNone(obs.resources.queue_ms)
            app.close()
        rows = [json.loads(line) for line in (app.state_dir/'model_calls.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['scope'] == 'health_probe' for row in rows))
        self.assertEqual(rows[0]['result']['raw_response']['usage']['total_tokens'], 26)
        self.assertFalse(app.probe_thread.is_alive())
        self.assertFalse(app.server_thread.is_alive())

    def test_guard_cannot_turn_an_invalid_raw_canary_into_a_pass(self):
        for server in self.servers:
            server.canary = dict(result_kind='action', actions=[dict(type='light_on', location='卧室')])
        app = self.app(probe_interval_s=0)
        app.monitor.run_once()
        self.assertTrue(all(not app.runtime.router.cache.get(name).reachable for name in ('local-model', 'cloud-model')))

    def test_business_guard_preserves_raw_audit_and_updates_passive_latency_only(self):
        from ai6g_runtime.protocol import classify_task
        for server in self.servers:
            server.business['actions'][0]['brightness'] = 99
            server.business_delay = .04
        app = self.app(probe_interval_s=0)
        result = app.runtime.backends['local-model'].infer(classify_task('客厅的灯亮度调到37'), timeout_s=2)
        self.assertEqual(result.payload['result_kind'], 'clarification')
        event = app.monitor.business_events.get_nowait()
        self.assertEqual(event[0], 'local-model')
        self.assertGreaterEqual(event[2], 40.)
        self.assertEqual(app.monitor.totals['generated_canary_requests'], 0)
        row = json.loads((app.state_dir/'model_calls.jsonl').read_text(encoding='utf8'))
        self.assertEqual(row['scope'], 'task')
        self.assertEqual(row['result']['payload']['actions'][0]['brightness'], 99)
        self.assertEqual(row['result']['raw_response']['model'], 'fixture-1.5B')

    def test_exclusive_state_and_close_leave_independent_model_services_alive(self):
        app = self.app(probe_interval_s=0)
        with self.assertRaises(FileExistsError):
            profile.LocalProfileServer(self.load(), state_dir=app.state_dir, port=0)
        app.start()
        app.close()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for service in self.endpoints_payload()['services']:
            with opener.open(service['health_url'], timeout=2) as response:
                self.assertEqual(json.load(response), {'status': 'ok'})
        self.assertEqual(json.loads((app.state_dir/'profile.json').read_text())['status'], 'stopped')

    def test_fresh_queue_is_latency_lower_bound_without_mutating_cached_measurement(self):
        from dataclasses import replace
        from ai6g_runtime.protocol import classify_task
        from ai6g_runtime.router import HealthObservation, ResourceObservation
        app = self.app(probe_interval_s=0)
        router = app.runtime.router
        router.max_queue_ms = 2000.
        now = time.time()
        original = HealthObservation('local-model', True, 500., .95, now, resources=ResourceObservation(
            queue_ms=900., free_mem_mb=2000., inflight=0, observed_at=now))
        router.cache.put(original)
        router.cache.put(HealthObservation('cloud-model', True, 700., .95, now))
        task = replace(classify_task('客厅灯亮度调到37'), rule_eligible=False, deadline_ms=850)
        self.assertEqual(router.choose(task, now=now).backend, 'cloud-model')
        self.assertEqual(router.cache.get('local-model').latency_ms, 500.)
        router.cache.put(HealthObservation('cloud-model', False, 700., .95, now))
        decision = router.choose(task, now=now)
        self.assertEqual(decision.estimated_latency_ms, 900.)
        for stamp in (now-10., now+1.):
            router.cache.put(replace(original, resources=replace(original.resources, observed_at=stamp)))
            self.assertEqual(router.choose(task, now=now).estimated_latency_ms, 500.)

    def test_external_health_write_is_forbidden_and_cannot_replace_monitor_observation(self):
        app = self.app(probe_interval_s=0)
        app.start()
        self.wait_for(lambda: not app.probe_thread.is_alive())
        before = app.runtime.router.cache.get('local-model')
        self.assertIsNotNone(before)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        forged = dict(backend='local-model', reachable=True, latency_ms=.001,
                      quality_score=1., quality_by_family={'lighting': 1.},
                      checked_at=time.time(), detail='untrusted external write')
        request = urllib.request.Request(app.metadata['url']+'/v1/health',
            data=json.dumps(forged).encode('utf8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            response = opener.open(request, timeout=2)
        except urllib.error.HTTPError as response:
            status, payload = response.code, json.load(response)
        else:
            with response:
                status, payload = response.status, json.load(response)
        self.assertEqual(app.runtime.router.cache.get('local-model'), before,
                         'external HTTP health writes must never overwrite bound observations')
        self.assertEqual(status, 403)
        self.assertEqual(payload['error_code'], 'health_read_only')
        self.assertIn('monitor', payload['error'].lower())

    def test_read_only_health_and_business_http_routes_remain_available(self):
        app = self.app(probe_interval_s=0)
        app.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(app.metadata['url']+'/v1/health', timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('local-model', json.load(response)['backends'])
        request = urllib.request.Request(app.metadata['url']+'/v1/tasks',
            data=json.dumps({'text': '打开客厅灯', 'request_id': 'v6-http-rule'}).encode('utf8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with opener.open(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)['ok'])

    def test_stop_file_shuts_down_cli_without_stopping_model_servers(self):
        directory = self.base/'cli-run'
        command = [sys.executable, '-X', 'utf8', '-m', 'ai6g_runtime.deploy.local_profile_v6',
                   '--endpoints-file', str(self.endpoints), '--prompt-file', str(self.prompt),
                   '--calibration', str(self.calibration), '--state-dir', str(directory),
                   '--port', '0', '--probe-interval', '0', '--nvidia-smi', 'missing-fixture-nvidia-smi']
        process = subprocess.Popen(command, cwd=ROOT.parent, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf8')
        try:
            self.wait_for(lambda: (directory/'profile.json').exists())
            (directory/'STOP').touch()
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr+stdout)
            self.assertEqual(json.loads((directory/'profile.json').read_text())['status'], 'stopped')
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=4)


if __name__ == '__main__':
    unittest.main()
