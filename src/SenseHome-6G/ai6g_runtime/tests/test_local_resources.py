"""Read-only real telemetry adapter tests; fixtures never claim real samples."""
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class LocalResourceTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('ai6g_runtime.local_resources'),
                             'read-only local resource adapter missing')
        return importlib.import_module('ai6g_runtime.local_resources')

    def runner(self, output, returncode=0):
        return lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode, output, '')

    def test_real_schema_uses_shared_gpu_and_boolean_slots_without_inventing_queue(self):
        m = self.module()
        provider = m.LocalResourceProvider({'local-model': 'http://127.0.0.1:62341/slots',
                                           'cloud-model': 'http://127.0.0.1:62342/slots'},
            runner=self.runner('0, GPU-abc, Test GPU, 1024, 6144\n'),
            fetch_json=lambda url, timeout: [{'id': 0, 'is_processing': False}, {'id': 1, 'is_processing': True}],
            clock=lambda: 1234.)
        snapshot = provider.collect()
        for name in ('local-model', 'cloud-model'):
            resource = snapshot['resources'][name]
            self.assertEqual(resource['free_mem_mb'], 1024)
            self.assertEqual(resource['inflight'], 1)
            self.assertIsNone(resource['queue_ms'])
            self.assertEqual(resource['observed_at'], 1234.)
            self.assertIn('gpu_global_shared', resource['source'])
        self.assertEqual(snapshot['gpu_memory_scope'], 'physical GPU global free; shared by both services and other processes')
        self.assertGreaterEqual(snapshot['collection_duration_ms'], 0)

    def test_timeout_bad_gpu_and_unavailable_slots_stay_unknown(self):
        m = self.module()
        def unavailable(url, timeout):
            raise TimeoutError('fixture slots unavailable')
        provider = m.LocalResourceProvider({'local-model': 'http://127.0.0.1:1/slots'},
            runner=self.runner('N/A, N/A\n', 1), fetch_json=unavailable)
        resource = provider('local-model')
        self.assertTrue(all(resource[k] is None for k in ('free_mem_mb', 'queue_ms', 'inflight')))
        self.assertIn('nvidia_smi', provider.latest['errors'])
        self.assertIn('local-model', provider.latest['errors'])

    def test_missing_or_non_boolean_slot_flags_are_unknown_not_zero(self):
        m = self.module()
        for payload in ([], {}, [{'is_processing': 'false'}], [{'id': 0}],
                        [{'is_processing': True}, {'id': 2}]):
            provider = m.LocalResourceProvider({'local-model': 'http://127.0.0.1:1/slots'},
                runner=self.runner('0, GPU-a, Test, 100, 1000\n'), fetch_json=lambda u, t: payload)
            self.assertIsNone(provider('local-model')['inflight'])

    def test_gpu_selection_does_not_sum_multiple_gpus_and_does_not_parse_bad_memory(self):
        m = self.module()
        data = '0, GPU-a, A, 100, 1000\n1, GPU-b, B, 800, 2000\n'
        self.assertEqual(m.parse_gpu_csv(data, '1')['free_mem_mb'], 800)
        self.assertEqual(m.parse_gpu_csv(data, 'GPU-a')['free_mem_mb'], 100)
        self.assertIsNone(m.parse_gpu_csv('0, GPU-a, A, N/A, 1000', '0'))
        self.assertIsNone(m.parse_gpu_csv(data, '5'))

    def test_raw_evidence_redacts_prompt_content_credentials_and_url_credentials_rejected(self):
        m = self.module()
        payload = {'prompt': 'private input', 'params': {'api_key': 'secret'},
                   'content': 'private result', 'is_processing': True}
        safe = m.redact(payload)
        self.assertNotIn('private', json.dumps(safe))
        self.assertNotIn('secret', json.dumps(safe))
        with self.assertRaises(ValueError):
            m.LocalResourceProvider({'x': 'http://user:password@127.0.0.1:123/slots'})
        with self.assertRaises(ValueError):
            m.LocalResourceProvider({'x': 'https://external.example/slots'})

    def test_adding_resources_preserves_actual_health_values_and_timestamp(self):
        m = self.module()
        original = {'backend': 'local-model', 'reachable': False, 'quality_score': .25,
            'latency_ms': 123, 'checked_at': 12, 'error_rate': 1, 'detail': 'actual probe failed'}
        resource = {'free_mem_mb': 100, 'inflight': None, 'queue_ms': None,
                    'source': 'local_real_readonly_gpu_global_shared', 'observed_at': 20}
        merged = m.merge_measured_health(original, resource)
        for key, value in original.items():
            self.assertEqual(merged[key], value)
        self.assertEqual(merged['resources'], resource)
        self.assertNotIn('resources', original)
        with self.assertRaisesRegex(ValueError, 'measured health'):
            m.merge_measured_health({'backend': 'local-model'}, resource)

    def test_failed_resource_sources_do_not_destroy_model_health_and_publish_valid_fields(self):
        m = self.module()
        from ai6g_runtime.health_monitor import HealthMonitor
        from ai6g_runtime.models import ModelResult
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            def log_message(self, *args):
                pass
        class FixtureBackend:
            def infer(self, task, timeout_s):
                # Test fixture only; this flag exercises the monitor's boundary.
                return ModelResult(True, {'result_kind': 'clarification', 'actions': [], 'explanation': 'fixture'},
                                   model_real=True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def unavailable(url, timeout):
                raise TimeoutError('fixture')
            provider = m.LocalResourceProvider({'local-model': 'http://127.0.0.1:1/slots'},
                runner=self.runner('', 1), fetch_json=unavailable)
            monitor = HealthMonitor(health_url=f'http://127.0.0.1:{server.server_port}/v1/health',
                backends={'local-model': FixtureBackend()},
                calibration={'local-model': {'quality_score': .5, 'source_sha256': 'a'*64}},
                resource_provider=provider)
            report = monitor.run_once()[0]
            self.assertTrue(report.reachable)
            self.assertTrue(report.published)
            self.assertTrue(received[0]['reachable'])
            self.assertNotIn('resources', received[0])
            health = {'local-model': {'backend': 'local-model', 'reachable': False,
                'latency_ms': 13., 'quality_score': .5, 'checked_at': 123.}}
            publication = m.publish_snapshot(f'http://127.0.0.1:{server.server_port}/v1/health', provider.latest, health)
            self.assertTrue(publication['results'][0]['published'])
            self.assertEqual(received[-1]['checked_at'], 123.)
            self.assertFalse(received[-1]['reachable'])
            self.assertIsNone(received[-1]['resources']['free_mem_mb'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_slots_and_publisher_never_follow_redirects_and_require_explicit_ack(self):
        m = self.module()
        received = []
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append('redirect_get_followed')
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'[]')
            def log_message(self, *args):
                pass
        target = ThreadingHTTPServer(('127.0.0.1', 0), Target)
        class Source(BaseHTTPRequestHandler):
            ack = b'{"ok":false,"error":"fixture"}'
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{target.server_port}/outside')
                self.end_headers()
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(type(self).ack)
            def log_message(self, *args):
                pass
        source = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, source)]
        for thread in threads:
            thread.start()
        try:
            with self.assertRaises(Exception):
                m._fetch_json(f'http://127.0.0.1:{source.server_port}/slots', 1)
            self.assertEqual(received, [])
            health = {'local-model': {'backend': 'local-model', 'reachable': False,
                'latency_ms': 13., 'quality_score': .5, 'checked_at': 123.}}
            resource = {'free_mem_mb': None, 'inflight': None, 'queue_ms': None,
                        'source': 'fixture_only', 'observed_at': 124.}
            snapshot = {'resources': {'local-model': resource}}
            for ack in (b'{"ok":false}', b'{}', b'not-json', b'{"ok":1}'):
                Source.ack = ack
                result = m.publish_snapshot(f'http://127.0.0.1:{source.server_port}/v1/health', snapshot, health)
                self.assertFalse(result['results'][0]['published'])
            Source.ack = b'{"ok":true}'
            result = m.publish_snapshot(f'http://127.0.0.1:{source.server_port}/v1/health', snapshot, health)
            self.assertTrue(result['results'][0]['published'])
        finally:
            for server, thread in zip((target, source), threads):
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
