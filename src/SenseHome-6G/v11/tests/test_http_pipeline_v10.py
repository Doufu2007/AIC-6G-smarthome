"""Actual localhost HTTP contract, not a real model or physical HA device."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, ModelPlanner
from platform_execution import HAClient
from routing import RoutedPlanner


class HTTPPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.requests = []
        self.state = dict(entity_id='light.reading', state='off', last_updated='v0',
                          attributes=dict(friendly_name='阅读灯', supported_color_modes=['brightness']))
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                test.requests.append(('GET', self.path, None))
                if self.path == '/api/states':
                    self.reply([copy.deepcopy(test.state)])
                else:
                    self.reply(copy.deepcopy(test.state))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.requests.append(('POST', self.path, body))
                if self.path == '/v1/chat/completions':
                    view = json.loads(body['messages'][-1]['content'])
                    payload = dict(result_kind='clarification', actions=[], reason='health fixture')
                    if view['catalog']:
                        payload = dict(result_kind='proposal', actions=[dict(entity_id='light.reading',
                            service='light.turn_on', data={})], reason='HTTP model fixture')
                    self.reply(dict(choices=[dict(message=dict(content=json.dumps(payload)))],
                                    usage=dict(prompt_tokens=20, completion_tokens=10, total_tokens=30)))
                else:
                    test.state['state'] = 'on'
                    test.state['last_updated'] = 'v1'
                    self.reply([])
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def native(self):
        path = Path(__file__).resolve().parents[1] / 'native_ha_adapter.py'
        self.assertTrue(path.exists(), 'native HA adapter not implemented')
        from native_ha_adapter import NativeHAAdapter
        return NativeHAAdapter(HAClient(self.url, 'localhost-fixture-token'))

    def test_probe_route_guard_confirm_service_readback_and_restart_receipt(self):
        native = self.native()
        catalog = [dict(entity_id='light.reading', kind='light', room='书房',
                        name='阅读灯', aliases=[], available=True)]
        backend = ModelPlanner(self.url + '/v1/chat/completions', 'HTTP-FIXTURE')
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False,
            calibrations={'local-model': dict(quality_score=.95, source_sha256='b' * 64)})
        app = Controller(self.temp.name, catalog, native, planner=planner, mode='localhost-http-fixture')
        card = app.propose('打开阅读灯')
        self.assertEqual(self.state['state'], 'off')
        self.assertFalse(any(path.startswith('/api/services') for _, path, _ in self.requests))
        route = card['metadata']['route']
        self.assertEqual(route['backend'], 'local-model')
        self.assertEqual(route['metrics']['total_tokens'], 30)
        self.assertEqual(len(route['probes']), 1)
        self.assertEqual(route['probes'][0]['metrics']['total_tokens'], 30)
        model_calls = [body for _, path, body in self.requests if path == '/v1/chat/completions']
        self.assertEqual(len(model_calls), 2)
        self.assertEqual(json.loads(model_calls[0]['messages'][-1]['content'])['catalog'], [])
        body = dict(proposal_id=card['proposal_id'], digest=card['digest'],
                    request_id='http-chain', confirmed=True)
        result = app.confirm(body)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.state['state'], 'on')
        services = [(path, payload) for _, path, payload in self.requests if path.startswith('/api/services')]
        self.assertEqual(services, [('/api/services/light/turn_on', {'entity_id': 'light.reading'})])
        self.assertEqual(self.requests[-1][:2], ('GET', '/api/states/light.reading'))
        reopened = Controller(self.temp.name, catalog, native, mode='localhost-http-fixture')
        self.assertEqual(reopened.receipt('http-chain')['metadata']['route']['backend'], 'local-model')
        self.assertTrue(reopened.confirm(body)['duplicate'])
        self.assertEqual(len([p for _, p, _ in self.requests if p.startswith('/api/services')]), 1)

    def test_cli_native_discovery_is_read_only(self):
        import cli
        import os
        from unittest.mock import patch
        output = io.StringIO()
        with patch.dict(os.environ, {'SENSEHOME_HA_TOKEN': 'fixture-token'}), redirect_stdout(output):
            try:
                rc = cli.main(['--home', self.temp.name, '--ha-url', self.url, '--ha-native', 'discover'])
            except SystemExit:
                self.fail('CLI native discovery is missing')
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['catalog'][0]['entity_id'], 'light.reading')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.requests))

    def test_direct_model_planner_does_not_send_private_request(self):
        planner = ModelPlanner(self.url + '/v1/chat/completions', 'HTTP-FIXTURE')
        result = planner(dict(request='不要上传我的设备信息；打开阅读灯', catalog=[dict(entity_id='light.reading')],
                              states={'light.reading': dict(state='off')}))
        self.assertEqual(result['result_kind'], 'clarification')
        self.assertEqual(self.requests, [])

    def test_offline_request_returns_before_model_or_home_assistant_access(self):
        native = self.native()
        catalog = [dict(entity_id='light.reading', kind='light', room='书房',
                        name='阅读灯', aliases=[], available=True)]
        planner = ModelPlanner(self.url + '/v1/chat/completions', 'HTTP-FIXTURE', allow_model_proposals=True)
        app = Controller(self.temp.name, catalog, native, planner=planner,
                         allow_model_proposals=True, mode='localhost-http-fixture')
        result = app.propose('不要联网，也不要上传我的设备信息；我想看书，帮我点亮适合阅读的那盏灯')
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.requests, [])

    def test_no_upload_request_uses_local_rules_without_model_http(self):
        native = self.native()
        catalog = [dict(entity_id='light.reading', kind='light', room='书房',
                        name='阅读灯', aliases=[], available=True)]
        planner = ModelPlanner(self.url + '/v1/chat/completions', 'HTTP-FIXTURE', allow_model_proposals=True)
        app = Controller(self.temp.name, catalog, native, planner=planner,
                         allow_model_proposals=True, mode='localhost-http-fixture')
        result = app.propose('不要上传我的设备信息；打开阅读灯')
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(result['metadata']['privacy_policy'], 'no_upload')
        self.assertFalse(any(path == '/v1/chat/completions' for _, path, _ in self.requests))


if __name__ == '__main__':
    unittest.main()
