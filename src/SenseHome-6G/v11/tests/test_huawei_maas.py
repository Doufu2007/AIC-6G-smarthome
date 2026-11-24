"""Huawei MaaS OpenAI-compatible HTTP contract tests using localhost only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stdout
from argparse import Namespace
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli


class HuaweiMaaSContractTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response_delay = 0
        self.response_status = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.requests.append((self.path, self.headers.get('Authorization'), body))
                content = json.dumps({
                    'result_kind': 'proposal',
                    'actions': [{'entity_id': 'light.reading', 'service': 'light.turn_on', 'data': {}}],
                    'reason': 'MaaS 测试提案',
                }, ensure_ascii=False)
                data = json.dumps({
                    'choices': [{'message': {'content': content}}],
                    'usage': {'prompt_tokens': 24, 'completion_tokens': 18, 'total_tokens': 42},
                }).encode('utf-8')
                time.sleep(test.response_delay)
                self.send_response(test.response_status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = 'http://127.0.0.1:%d/openai/v1/chat/completions' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @staticmethod
    def view():
        return {
            'request': '打开书房阅读灯',
            'catalog': [{'entity_id': 'light.reading', 'kind': 'light',
                         'room': '书房', 'name': '阅读灯', 'aliases': [], 'available': True}],
            'states': {'light.reading': {'state': 'off', 'attributes': {}, 'last_updated': 'initial'}},
        }

    def routes_file(self, temporary, *, privacy='standard', direct=True, deadline_ms=None,
                    require_api_key=False, calibration_file=None):
        routes = Path(temporary) / 'huawei-maas.routes.json'
        config = {
            'direct_model': direct,
            'prefer_rules': False,
            'allow_model_proposals': True,
            'privacy': privacy,
            'backends': {
                'cloud-model': {
                    'endpoint': self.endpoint,
                    'model': 'openpangu-2.1-pro',
                    'api_key_env': 'MAAS_API_KEY',
                    'timeout_seconds': 3,
                    'json_mode': False,
                    'seed': None,
                    'require_api_key': require_api_key,
                },
            },
        }
        if deadline_ms is not None:
            config['deadline_ms'] = deadline_ms
        if calibration_file is not None:
            config['calibration_file'] = str(calibration_file)
        routes.write_text(json.dumps(config), encoding='utf-8')
        return routes

    @staticmethod
    def args(routes, temporary=None):
        return Namespace(routes=str(routes), model_endpoint=None, model_name=None,
                         calibration_file=None, allow_model_proposals=False,
                         home=str(temporary), ha_url=None, ha_native=False, ha_map=None)

    def app(self, temporary, routes):
        with redirect_stdout(io.StringIO()):
            cli.main(['--home', temporary, 'init'])
        return cli.make_app(self.args(routes, temporary))

    def test_single_backend_config_sends_maas_contract_without_unconfirmed_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary)
            with patch.dict('os.environ', {'MAAS_API_KEY': 'localhost-fixture-key'}):
                planner = cli.make_planner(self.args(routes))
                result = planner.generate(self.view())

        self.assertEqual(result['result_kind'], 'proposal')
        self.assertEqual(len(self.requests), 1)
        path, authorization, body = self.requests[0]
        self.assertEqual(path, '/openai/v1/chat/completions')
        self.assertEqual(authorization, 'Bearer localhost-fixture-key')
        self.assertEqual(body['model'], 'openpangu-2.1-pro')
        self.assertEqual(body['temperature'], 0)
        self.assertEqual(body['max_tokens'], 512)
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user'])
        body = self.requests[0][2]
        self.assertNotIn('seed', body)
        self.assertNotIn('response_format', body)
        self.assertEqual(json.loads(body['messages'][1]['content'])['request'], '打开书房阅读灯')

    def test_single_backend_without_calibration_does_not_bypass_strict_privacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, privacy='strict', direct=False)
            with patch.dict('os.environ', {'MAAS_API_KEY': 'localhost-fixture-key'}):
                planner = cli.make_planner(self.args(routes))
                result = planner(self.view())
        self.assertIn(result['result_kind'], ('proposal', 'clarification'))
        self.assertEqual(self.requests, [])

    def test_legacy_model_planner_defaults_keep_seed_and_json_mode(self):
        from controller import ModelPlanner
        planner = ModelPlanner(self.endpoint, 'fixture')
        result = planner.generate(self.view())
        self.assertEqual(result['result_kind'], 'proposal')
        body = self.requests[0][2]
        self.assertEqual(body['seed'], 20261111)
        self.assertEqual(body['response_format'], {'type': 'json_object'})

    def test_model_proposal_requires_confirmation_before_software_device_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary)
            with patch.dict('os.environ', {'MAAS_API_KEY': 'localhost-fixture-key'}):
                app = self.app(temporary, routes)
                card = app.propose('打开书房阅读灯')
            self.assertIn('proposal_id', card)
            self.assertEqual(card['payload']['actions'], [
                {'entity_id': 'light.reading', 'service': 'light.turn_on', 'data': {}}])
            self.assertEqual(app.client.get_state('light.reading')['state'], 'off')
            self.assertEqual(app.client.calls, 0)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(card['metadata']['route']['metrics']['total_tokens'], 42)
            confirmed = app.confirm(dict(proposal_id=card['proposal_id'], digest=card['digest'],
                                         request_id='maas-local-fixture', confirmed=True))
            self.assertEqual(confirmed['status'], 'succeeded')
            self.assertEqual(app.client.get_state('light.reading')['state'], 'on')
            self.assertEqual(app.client.calls, 1)

    def test_routes_need_explicit_direct_opt_in_without_calibration(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, direct=False)
            planner = cli.make_planner(self.args(routes))
            planner(self.view())
        self.assertEqual(self.requests, [])

    def test_direct_proposal_respects_local_only_request_without_http(self):
        for prefix in ('只在本地，', '仅在本地，'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as temporary:
                routes = self.routes_file(temporary)
                app = self.app(temporary, routes)
                app.propose(prefix + '打开书房阅读灯')
                self.assertEqual(self.requests, [])
                self.assertEqual(app.client.calls, 0)

    def test_direct_model_keeps_configured_deadline(self):
        self.response_delay = .2
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, deadline_ms=20)
            app = self.app(temporary, routes)
            card = app.propose('打开书房阅读灯')
            self.assertEqual(card['status'], 'clarification')
            self.assertEqual(app.client.calls, 0)

    def test_direct_model_rejects_mixed_calibration_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, calibration_file=Path(temporary) / 'missing.json')
            with self.assertRaisesRegex(ValueError, 'calibration'):
                cli.make_planner(self.args(routes))
        self.assertEqual(self.requests, [])

    def test_missing_required_key_fails_before_any_model_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, require_api_key=True)
            with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(ValueError, 'MAAS_API_KEY'):
                cli.make_planner(self.args(routes))
        self.assertEqual(self.requests, [])

    def test_auth_error_is_visible_and_rule_fallback_never_auto_executes(self):
        self.response_status = 401
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary)
            app = self.app(temporary, routes)
            card = app.propose('打开书房阅读灯')
            self.assertEqual(card['metadata']['route']['reason'], 'PLANNER_ERROR_RULE_FALLBACK')
            self.assertEqual(card['metadata']['route']['error_type'], 'HTTPError')
            self.assertEqual(app.client.calls, 0)
            self.assertEqual(app.client.get_state('light.reading')['state'], 'off')
            self.assertNotIn('total_tokens', card['metadata']['route']['metrics'])

    def test_direct_model_rejects_strict_privacy_config_instead_of_ignoring_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, privacy='strict')
            with self.assertRaisesRegex(ValueError, 'privacy'):
                cli.make_planner(self.args(routes))
        self.assertEqual(self.requests, [])

    def test_routes_reject_cli_calibration_override_instead_of_dropping_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary)
            args = self.args(routes)
            args.calibration_file = str(Path(temporary) / 'cli-calibration.json')
            with self.assertRaisesRegex(ValueError, 'calibration'):
                cli.make_planner(args)
        self.assertEqual(self.requests, [])

    def test_deadline_does_not_coerce_boolean_backend_timeout_into_valid_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes = self.routes_file(temporary, deadline_ms=20)
            config = json.loads(routes.read_text(encoding='utf-8'))
            config['backends']['cloud-model']['timeout_seconds'] = True
            routes.write_text(json.dumps(config), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'timeout'):
                cli.make_planner(self.args(routes))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
