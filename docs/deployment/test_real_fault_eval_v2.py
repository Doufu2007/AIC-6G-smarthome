"""Local HTTP fixtures test proxy behavior; these are not model evidence."""
from __future__ import annotations
import http.server
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request


def load_runner(test):
    path = Path(__file__).with_name('real_fault_eval_v2.py')
    test.assertTrue(path.is_file(), 'HTTP fault proxy is not implemented')
    spec = importlib.util.spec_from_file_location('real_fault_v2_test_module', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


class Fixture(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.calls.append(('GET', self.path))
        self.send({'data': [{'id': 'fixture-not-real-model'}]})

    def do_POST(self):
        self.calls.append(('POST', self.path))
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send({'object': 'fixture-not-real-model', 'received': body,
                   'choices': [{'message': {'content': '{"actions": []}'}}]})


class ProxyTests(unittest.TestCase):
    def setUp(self):
        Fixture.calls = []
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.upstream = f'http://127.0.0.1:{self.server.server_port}/v1'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, url, body=None):
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Content-Type': 'application/json'})
        try:
            # Windows user proxy configuration must not divert this loopback fixture.
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_actual_http_503_prevents_upstream_forwarding_and_health_is_not_ready(self):
        module = load_runner(self)
        with module.ControlledProxy(self.upstream, mode='http_503') as proxy:
            status, body = self.request(proxy.url + '/chat/completions', {'messages': []})
            self.assertEqual(status, 503)
            self.assertFalse(body['_sensehome_proxy_trace']['upstream_forwarded'])
            self.assertEqual(Fixture.calls, [])
            status, health = self.request(proxy.base_url + '/health')
            self.assertEqual(status, 200)
            self.assertFalse(health['ready'])
            self.assertEqual(health['source'], 'controlled_proxy_not_gpu_queue')

    def test_real_dispatch_wait_is_measured_and_not_claimed_as_gpu_queue(self):
        module = load_runner(self)
        with module.ControlledProxy(self.upstream, delay_ms=40) as proxy:
            started = time.perf_counter()
            status, body = self.request(proxy.url + '/chat/completions', {'messages': []})
            elapsed = (time.perf_counter() - started) * 1000
            self.assertEqual(status, 200)
            self.assertGreaterEqual(elapsed, 35)
            status, health = self.request(proxy.base_url + '/health')
            self.assertGreaterEqual(health['last_admission_wait_ms'], 35)
            self.assertIsNotNone(health['admission_sample_age_ms'])
            self.assertEqual(health['source'], 'controlled_proxy_not_gpu_queue')
            self.assertEqual(Fixture.calls, [('POST', '/v1/chat/completions')])
            self.assertTrue(body['_sensehome_proxy_trace']['upstream_forwarded'])

    def test_success_response_keeps_model_content_and_records_forwarded_body(self):
        module = load_runner(self)
        with module.ControlledProxy(self.upstream) as proxy:
            request = {'messages': [{'role': 'user', 'content': 'test'}]}
            status, body = self.request(proxy.url + '/chat/completions', request)
            self.assertEqual(status, 200)
            self.assertEqual(body['received'], request)
            self.assertEqual(body['choices'][0]['message']['content'], '{"actions": []}')
            trace = body['_sensehome_proxy_trace']['trace_id']
            recorded = next(row for row in proxy.records if row['trace_id'] == trace)
            self.assertEqual(recorded['upstream_request'], request)
            self.assertEqual(recorded['upstream_status'], 200)

    def test_proxy_refuses_non_loopback_upstreams(self):
        module = load_runner(self)
        with self.assertRaises(ValueError):
            module.ControlledProxy('https://example.com/v1')


if __name__ == '__main__':
    unittest.main()
