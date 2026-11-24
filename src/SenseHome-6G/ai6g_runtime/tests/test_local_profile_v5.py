"""Local-profile contracts using two HTTP fixtures; no real GPU inference."""
import contextlib
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
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
try:
    profile = importlib.import_module('ai6g_runtime.deploy.local_profile_v5')
except ModuleNotFoundError:
    profile = None


class FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(data)
        body = {'id': 'fixture-full-response', 'model': data['model'],
                'choices': [{'message': {'content': json.dumps({
                    'result_kind': 'clarification', 'actions': [],
                    'explanation': 'fixture probe does not execute actions'})},
                    'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}}
        self._send(body)

    def do_GET(self):
        self._send([{'id': 0, 'is_processing': False, 'prompt': 'omit slot text'}])

    def _send(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class LocalProfileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(profile, 'new local profile entry point must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.servers = []
        services = []
        for name, model in [('local-small', 'fixture-1.5B'), ('local-large', 'fixture-3B')]:
            server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
            server.calls = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            origin = f'http://127.0.0.1:{server.server_port}'
            services.append({'name': name, 'model': model,
                             'endpoint': origin+'/v1/chat/completions',
                             'slots_url': origin+'/slots', 'weight_sha256': 'b'*64})
        self.endpoints = self.base/'endpoints.json'
        self.prompt = self.base/'prompt.txt'
        self.prompt.write_text('Fixture fixed household protocol.\n', encoding='utf-8')
        self.capture = self.base/'calibration_capture.jsonl'
        self.capture.write_text('{"fixture":"original calibration bytes"}\n', encoding='utf-8')
        source_hash = hashlib.sha256(self.capture.read_bytes()).hexdigest()
        self.calibration = self.base/'calibration.json'
        self.calibration.write_text(json.dumps({'schema': 'ai6g.family_calibration.v5',
            'source_sha256': source_hash, 'backends': {
                'local-model': {'quality_score': .8, 'source_sha256': source_hash},
                'cloud-model': {'quality_score': .9, 'source_sha256': source_hash}}}), encoding='utf-8')
        self.manifest = self.base/'calibration_capture.manifest.json'
        prompt_hash = hashlib.sha256(json.dumps(self.prompt.read_text(encoding='utf-8').strip(),
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.manifest.write_text(json.dumps({'manifest': {
            'schema': 'ai6g.model_capture.manifest.v5', 'backends': {
                alias: {'model': item['model'], 'endpoint': item['endpoint'],
                        'prompt_sha256': prompt_hash, 'max_tokens': 384}
                for alias, item in zip(('local-model', 'cloud-model'), services)},
            'decoding': {alias: {'temperature': 0, 'response_format': 'json_object',
                                'seed': 20261104, 'max_tokens': 384}
                         for alias in ('local-model', 'cloud-model')}}}), encoding='utf-8')
        data = json.loads(self.manifest.read_text(encoding='utf-8'))
        fingerprint = hashlib.sha256(json.dumps(data['manifest'], ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        data['fingerprint'] = fingerprint
        self.manifest.write_text(json.dumps(data), encoding='utf-8')
        self.capture.write_text(json.dumps({'capture_fingerprint': fingerprint})+'\n', encoding='utf-8')
        source_hash = hashlib.sha256(self.capture.read_bytes()).hexdigest()
        calibration = json.loads(self.calibration.read_text(encoding='utf-8'))
        calibration['source_sha256'] = source_hash
        for item in calibration['backends'].values():
            item['source_sha256'] = source_hash
        self.calibration.write_text(json.dumps(calibration), encoding='utf-8')
        self.payload = {'schema': 'sensehome.local_endpoints.v5b', 'services': services,
                        'system_prompt': 'OLD PROMPT MUST NOT BE USED'}
        self.write_endpoints()

    def write_endpoints(self, payload=None):
        self.endpoints.write_text(json.dumps(payload or self.payload), encoding='utf-8')

    def load(self):
        return profile.load_profile(self.endpoints, self.prompt, self.calibration)

    def test_explicit_prompt_and_model_mapping_reach_actual_http_requests(self):
        loaded = self.load()
        with profile.LocalProfileServer(loaded, state_dir=self.base/'run', port=0,
                                        probe_interval_s=0) as app:
            app.runtime.backends['local-model'].infer('fixture task', timeout_s=2)
            app.runtime.backends['cloud-model'].infer('fixture task', timeout_s=2)
            self.assertEqual(app.runtime.trusted_backends,
                             {'local-model', 'cloud-model', 'local-rules'})
            self.assertEqual(app.runtime.router.trusted_backends,
                             {'local-model', 'cloud-model', 'local-rules'})
        for server, model in zip(self.servers, ('fixture-1.5B', 'fixture-3B')):
            request = server.calls[-1]
            self.assertEqual(request['model'], model)
            self.assertEqual(request['messages'][0]['content'], 'Fixture fixed household protocol.')
            self.assertEqual(request['max_tokens'], 384)
            self.assertEqual(request['seed'], 20261104)
            self.assertEqual(request['temperature'], 0)

    def test_rejects_unknown_schema_duplicate_or_unknown_service_names(self):
        candidates = []
        unknown = copy.deepcopy(self.payload); unknown['schema'] = 'unknown'
        candidates.append(unknown)
        duplicate = copy.deepcopy(self.payload)
        duplicate['services'][1]['name'] = 'local-small'; candidates.append(duplicate)
        wrong = copy.deepcopy(self.payload)
        wrong['services'][1]['name'] = 'remote'; candidates.append(wrong)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.write_endpoints(candidate)
                with self.assertRaises(ValueError):
                    self.load()

    def test_rejects_nonlocal_or_credential_url_and_mismatched_slots_origin(self):
        for url in ('https://example.com/v1/chat/completions',
                    'http://u:secret@127.0.0.1:1234/v1/chat/completions',
                    'http://127.0.0.1:1234/v1/chat/completions?api_key=secret',
                    'http://127.0.0.1:1234/v1/chat/completions#secret'):
            candidate = copy.deepcopy(self.payload); candidate['services'][0]['endpoint'] = url
            self.write_endpoints(candidate)
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load()
        candidate = copy.deepcopy(self.payload)
        candidate['services'][0]['slots_url'] = 'http://127.0.0.1:1234/slots'
        self.write_endpoints(candidate)
        with self.assertRaises(ValueError):
            self.load()

    def test_missing_profile_prompt_calibration_capture_or_manifest_fails(self):
        for path in (self.endpoints, self.prompt, self.calibration, self.capture, self.manifest):
            temporary = path.with_suffix(path.suffix+'.held')
            path.rename(temporary)
            try:
                with self.subTest(path=path.name), self.assertRaises(OSError):
                    self.load()
            finally:
                temporary.rename(path)

    def test_calibration_source_hash_and_backend_keys_are_verified(self):
        self.capture.write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source.*hash|hash.*source'):
            self.load()

    def test_calibration_backend_keys_are_not_silently_remapped(self):
        calibration = json.loads(self.calibration.read_text(encoding='utf-8'))
        calibration['backends']['wrong-model'] = calibration['backends'].pop('cloud-model')
        self.calibration.write_text(json.dumps(calibration), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.load()

    def test_calibration_prompt_model_and_decoding_must_match(self):
        original = self.manifest.read_text(encoding='utf-8')
        for section, field, value in [('backends', 'prompt_sha256', 'f'*64),
                                       ('backends', 'model', 'wrong'),
                                       ('decoding', 'seed', 7),
                                       ('decoding', 'max_tokens', 3)]:
            data = json.loads(original)
            data['manifest'][section]['local-model'][field] = value
            fingerprint = hashlib.sha256(json.dumps(data['manifest'], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            data['fingerprint'] = fingerprint
            self.manifest.write_text(json.dumps(data), encoding='utf-8')
            self.capture.write_text(json.dumps({'capture_fingerprint': fingerprint})+'\n', encoding='utf-8')
            source_hash = hashlib.sha256(self.capture.read_bytes()).hexdigest()
            calibration = json.loads(self.calibration.read_text(encoding='utf-8'))
            calibration['source_sha256'] = source_hash
            for item in calibration['backends'].values():
                item['source_sha256'] = source_hash
            self.calibration.write_text(json.dumps(calibration), encoding='utf-8')
            message = 'model/endpoint/prompt mismatch' if section == 'backends' else 'decoding mismatch'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                self.load()

    def test_state_directory_is_exclusive_and_host_stays_loopback(self):
        loaded = self.load()
        existing = self.base/'existing'; existing.mkdir()
        with self.assertRaises(FileExistsError):
            profile.LocalProfileServer(loaded, state_dir=existing, port=0)
        for host in ('0.0.0.0', '192.168.1.2', 'example.com'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                profile.LocalProfileServer(loaded, state_dir=self.base/host, host=host, port=0)

    def test_real_monitor_collects_resources_without_queue_invention_or_device_action(self):
        loaded = self.load()
        gpu = subprocess.CompletedProcess([], 0,
            '0, GPU-fixture, Fixture GPU, 2000, 6000\n', '')
        with patch('ai6g_runtime.local_resources.subprocess.run', return_value=gpu):
            with profile.LocalProfileServer(loaded, state_dir=self.base/'probe-run', port=0,
                                            probe_interval_s=.05, probe_timeout_s=2) as app:
                initial = app.runtime.state()
                app.start()
                end = time.monotonic()+4
                while time.monotonic() < end:
                    if (app.runtime.router.cache.get('cloud-model') is not None and
                            all(len(server.calls) >= 2 for server in self.servers)):
                        break
                    time.sleep(.01)
                for name in ('local-model', 'cloud-model'):
                    observed = app.runtime.router.cache.get(name)
                    self.assertIsNotNone(observed)
                    self.assertIsNone(observed.resources.queue_ms)
                    self.assertEqual(observed.resources.inflight, 0)
                    self.assertEqual(observed.resources.free_mem_mb, 2000)
                self.assertEqual(initial, app.runtime.state())
                self.assertTrue(all(len(server.calls) >= 2 for server in self.servers))
                self.assertFalse((app.state_dir/'requests.jsonl').exists())
                probe_thread, http_thread = app.probe_thread, app.server_thread
            self.assertFalse(probe_thread.is_alive())
            self.assertFalse(http_thread.is_alive())
        rows = [json.loads(line) for line in (app.state_dir/'model_calls.jsonl').read_text().splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all(row['scope'] == 'health_probe' for row in rows))
        self.assertEqual(rows[0]['result']['raw_response']['usage']['total_tokens'], 26)
        probes = [json.loads(line) for line in (app.state_dir/'probes.jsonl').read_text().splitlines()]
        self.assertTrue(all(row['published'] for row in probes if not row.get('skipped')))
        metadata = json.loads((app.state_dir/'profile.json').read_text())
        self.assertEqual(metadata['status'], 'stopped')
        # Stopping our runtime must leave independently owned model servers alive.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.payload['services'][0]['slots_url'], timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_task_state_is_persisted_and_idempotent_using_dedicated_directory(self):
        with profile.LocalProfileServer(self.load(), state_dir=self.base/'task-run', port=0,
                                        probe_interval_s=0) as app:
            first = app.runtime.handle('打开客厅的灯', request_id='local-profile-fixture-1')
            second = app.runtime.handle('打开客厅的灯', request_id='local-profile-fixture-1')
            self.assertTrue(first['ok'])
            self.assertTrue(second['ok'])
            self.assertTrue(app.runtime.state()['lights']['客厅']['is_on'])
            self.assertTrue((app.state_dir/'state.json').exists())
            self.assertEqual(sum(len(server.calls) for server in self.servers), 0)

    def test_metadata_retries_transient_windows_reader_lock(self):
        original = Path.replace
        attempts = []
        def reader_lock_then_release(source, destination):
            attempts.append((source, destination))
            if len(attempts) <= 2:
                raise PermissionError(5, 'transient Windows reader lock')
            return original(source, destination)
        with patch.object(Path, 'replace', reader_lock_then_release):
            app = profile.LocalProfileServer(self.load(), state_dir=self.base/'metadata-run', port=0)
        try:
            metadata = json.loads((app.state_dir/'profile.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['status'], 'created')
            self.assertEqual(len(attempts), 3)
        finally:
            app.close()

    def test_metadata_permanent_permission_denial_is_not_silenced(self):
        with profile.LocalProfileServer(self.load(), state_dir=self.base/'denied-run', port=0) as app:
            with patch.object(Path, 'replace', side_effect=PermissionError(5, 'permanent denial')):
                with self.assertRaises(PermissionError):
                    app.close()


if __name__ == '__main__':
    unittest.main()
