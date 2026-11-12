from copy import deepcopy
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


def load(name):
    path = Path(__file__).with_name(name + '.py')
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def inventory_fixture(*, redirect=None, model='expected-live-model'):
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            hits.append(self.path)
            if redirect:
                raw = b''
                self.send_response(302)
                self.send_header('Location', redirect)
            else:
                raw = json.dumps({'data':[{'id':model}]}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}',hits
    finally:
        server.shutdown();server.server_close();thread.join(2)


def probe_cli(origin, env=None):
    with tempfile.TemporaryDirectory() as tmp:
        config=Path(tmp)/'config.json';out=Path(tmp)/'result.json'
        config.write_text(json.dumps({'status':'test-fixture','endpoints':{
            'local':origin+'/v1','cloud':origin+'/v1'}}),encoding='utf-8')
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('deployment_check.py')),
            '--config',str(config),'--probe-loopback','--out',str(out)],
            env=env,capture_output=True,text=True,timeout=15)
        if result.returncode:
            raise AssertionError(result.stdout+result.stderr)
        return json.loads(out.read_text(encoding='utf-8'))


class EvidenceToolsTests(unittest.TestCase):
    def tool(self, name):
        module = load(name)
        self.assertIsNotNone(module, 'Missing evidence tool: ' + name)
        return module

    def report(self, usage=None):
        return {'endpoints': {'local': {'endpoint': 'http://127.0.0.1:12/v1', 'evidence_kind': 'real-model'}},
                'rows': [{'id': 'C1', 'pair_id': 'C1:r0', 'arm': 'joint', 'model_calls': 1,
                          'gateway_event': {'selected_backend': 'local-model'},
                          'backend_attempts': [{'upstream_exchanges': [
                              {'method': 'GET', 'response': {'usage': {'prompt_tokens': 999}}},
                              {'method': 'POST', 'http_status': 200, 'wall_ms': 50,
                               'request': {'model': 'test-model'},
                               'response': {'model': 'test-model', 'usage': usage}}]}]}]}

    def test_raw_post_is_counted_once_without_duplicate_adapter_or_get(self):
        mod = self.tool('usage_audit')
        report = self.report({'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13})
        report['rows'][0]['backend_attempts'][0]['response'] = {'upstream_usage': {'prompt_tokens': 500}}
        records = mod.extract_calls(report, 'report.json', 'a' * 64, 'matrix')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['prompt_tokens'], 10)
        self.assertEqual(records[0]['source']['exchange_index'], 1)
        self.assertEqual(mod.summarize(records)[0]['observed_nonloopback_posts'], 0)

    def test_missing_usage_remains_null_instead_of_zero(self):
        mod = self.tool('usage_audit')
        records = mod.extract_calls(self.report(), 'r.json', 'a' * 64, 'm')
        summary = mod.summarize(records)[0]
        self.assertIsNone(summary['prompt_tokens_total'])
        self.assertEqual(summary['prompt_tokens_known_subtotal'], 0)
        self.assertEqual(summary['calls_missing_or_invalid_usage'], 1)

    def test_invalid_numeric_usage_is_flagged_and_external_endpoint_separate(self):
        mod = self.tool('usage_audit')
        report = self.report({'prompt_tokens': True, 'completion_tokens': -2, 'total_tokens': 3.2})
        report['endpoints']['local']['endpoint'] = 'https://example.test/v1'
        row = mod.extract_calls(report, 'r.json', 'a' * 64, 'm')[0]
        self.assertIsNone(row['prompt_tokens'])
        self.assertTrue(row['usage_issues'])
        self.assertFalse(row['loopback_endpoint'])

    def ledger(self):
        return {'schema': 'sensehome.actual-ledger.v2', 'currency': 'CNY',
                'period': 'test', 'scope': 'test scope',
                'entries': [{ 'category': key, 'amount': None, 'status': 'unknown',
                             'currency': 'CNY', 'source': 'not collected', 'allocation_fraction': 1}
                            for key in ['hardware_amortization', 'electricity', 'cloud_inference', 'operations', 'network']]}

    def test_unknown_ledger_total_is_null(self):
        mod = self.tool('cost_ledger')
        with tempfile.TemporaryDirectory() as tmp:
            result = mod.validate_ledger(self.ledger(), Path(tmp))
        self.assertIsNone(result['actual_total'])
        self.assertIsNone(result['scenario_total'])

    def test_actual_entry_requires_matching_receipt_and_allocation(self):
        mod = self.tool('cost_ledger')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); receipt = root / 'receipt.txt';receipt.write_bytes(b'actual invoice')
            ledger = self.ledger();entry = ledger['entries'][0]
            entry.update(amount=120, status='actual', allocation_fraction=.25,
                         receipt={'path': 'receipt.txt', 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
                                  'kind': 'invoice'})
            result = mod.validate_ledger(ledger, root)
            self.assertEqual(result['known_actual_subtotal'], 30)
            self.assertIsNone(result['actual_total'])
            receipt.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                mod.validate_ledger(ledger, root)

    def test_quote_cannot_become_actual_total_and_currency_must_match(self):
        mod = self.tool('cost_ledger')
        ledger = self.ledger()
        for entry in ledger['entries']:entry.update(amount=10,status='quote')
        with tempfile.TemporaryDirectory() as tmp:
            result = mod.validate_ledger(ledger,Path(tmp))
            self.assertEqual(result['scenario_total'],50)
            self.assertIsNone(result['actual_total'])
            ledger['entries'][0]['currency']='USD'
            with self.assertRaisesRegex(ValueError,'currency'):
                mod.validate_ledger(ledger,Path(tmp))

    def test_receipt_path_cannot_escape_workspace(self):
        mod = self.tool('cost_ledger')
        ledger = self.ledger();ledger['entries'][0].update(amount=1,status='actual',
            receipt={'path':'../outside.txt','sha256':'a'*64,'kind':'invoice'})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'receipt'):
                mod.validate_ledger(ledger,Path(tmp))

    def test_loopback_slots_do_not_prove_two_host_deployment(self):
        mod = self.tool('deployment_check')
        result = mod.inspect_config({'status':'current_local_only','endpoints':{
            'local':'http://127.0.0.1:62352/v1','cloud':'http://127.0.0.1:42354/v1'}})
        self.assertTrue(result['both_slots_loopback'])
        self.assertFalse(result['two_host_deployment_verified'])

    def test_endpoint_credentials_and_query_are_rejected(self):
        mod = self.tool('deployment_check')
        for url in ['https://user:secret@example.test/v1','https://example.test/v1?key=secret']:
            with self.assertRaises(ValueError):mod.inspect_config({'status':'template_not_deployed',
                'endpoints':{'local':url,'cloud':'https://example.org/v1'}})

    def test_live_probe_requires_literal_loopback_without_dns_resolution(self):
        mod = self.tool('deployment_check')
        for url in ['http://localhost:1234/v1', 'http://example.test/v1', 'http://192.0.2.1/v1']:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'literal loopback'):
                mod.validate_probe_target(url)

    def test_loopback_probe_ignores_environment_proxy_and_reaches_declared_target(self):
        with inventory_fixture() as (target,target_hits), inventory_fixture(model='fake-proxy-model') as (proxy,proxy_hits):
            env=os.environ.copy()
            for name in ('http_proxy','HTTP_PROXY','https_proxy','HTTPS_PROXY','all_proxy','ALL_PROXY'):
                env[name]=proxy
            env['no_proxy']='';env['NO_PROXY']=''
            result=probe_cli(target,env)
            self.assertEqual(proxy_hits,[],'Loopback inventory request reached environment proxy')
            self.assertEqual(target_hits,['/v1/models','/v1/models'])
            self.assertTrue(all(entry['models']==['expected-live-model']
                                for entry in result['read_only_loopback_probes'].values()))
            self.assertTrue(all(entry['requested_url']==target+'/v1/models'
                                and entry['connected_peer']['address']=='127.0.0.1'
                                and entry['transport']=='direct-http.client-no-proxy-no-redirect'
                                for entry in result['read_only_loopback_probes'].values()))

    def test_loopback_probe_refuses_redirect_without_contacting_redirect_target(self):
        with inventory_fixture(model='redirected-model') as (sink,sink_hits):
            with inventory_fixture(redirect=sink+'/v1/models') as (origin,origin_hits):
                result=probe_cli(origin)
                self.assertEqual(sink_hits,[],'Inventory probe followed an HTTP redirect')
                self.assertEqual(origin_hits,['/v1/models','/v1/models'])
                self.assertTrue(all(entry['error']=='HTTP_REDIRECT_REFUSED' and entry['http_status']==302
                                    for entry in result['read_only_loopback_probes'].values()))


if __name__ == '__main__':
    unittest.main()
