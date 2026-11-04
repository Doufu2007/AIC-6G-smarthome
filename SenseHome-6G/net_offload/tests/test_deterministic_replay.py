import copy
import hashlib
import http.client
import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from evidence import validate_results_schema
from link_emulator import LinkEmulator
from network_trace import NetworkTrace
import offload_bench


class EchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DeterministicReplayTests(unittest.TestCase):
    def test_legacy_trace_without_outcomes_remains_readable_but_not_replayable(self):
        data = NetworkTrace.generate(seed=1, steps=[30], probes_per_step=1).to_dict()
        for sample in data['samples']:
            sample.pop('outcomes')
        canonical = json.dumps({'seed': data['seed'], 'samples': data['samples']},
                               sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        data['trace_id'] = hashlib.sha256(canonical).hexdigest()[:16]
        trace = NetworkTrace.from_dict(data)
        self.assertEqual(trace.to_dict(), data)
        with self.assertRaisesRegex(ValueError, 'LEGACY_TRACE'):
            trace.samples[0].outcome('infer')
    def test_generated_trace_records_separate_actual_outcomes_for_every_link_and_role(self):
        trace = NetworkTrace.generate(seed=19, steps=[30, None], probes_per_step=2)
        first = trace.samples[0]
        self.assertIsInstance(first.outcome('infer', 'wan')['refused'], bool)
        self.assertGreaterEqual(first.outcome('infer', 'wan')['rtt_ms'], 0)
        self.assertNotEqual(first.outcome('probe', 'wan'), first.outcome('infer', 'wan'))
        self.assertIn('rtt_ms', first.outcome('infer', 'lan'))
        self.assertTrue(trace.samples[-1].outcome('infer', 'wan')['refused'])
        self.assertEqual(NetworkTrace.from_dict(json.loads(json.dumps(trace.to_dict()))), trace)

    def test_replay_actual_outcome_controls_tcp_connection_without_random_draws(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        emu = LinkEmulator(0, '127.0.0.1', server.server_port, loss_pct=100).start()
        port = emu._srv.getsockname()[1]
        sample = NetworkTrace.generate(seed=2, steps=[1], probes_per_step=1, loss_max_pct=0).samples[0]
        try:
            emu.set_replay(sample, 'infer', 'wan')
            with mock.patch('link_emulator.random.random', side_effect=AssertionError('unexpected random')), mock.patch('link_emulator.random.uniform', side_effect=AssertionError('unexpected jitter')):
                for _ in range(2):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    try:
                        connection.request('GET', '/')
                        response = connection.getresponse()
                        self.assertEqual(response.read(), b'ok')
                    finally:
                        connection.close()
            blackout = NetworkTrace.generate(seed=2, steps=[None], probes_per_step=1).samples[0]
            emu.set_replay(blackout, 'infer', 'wan')
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                with self.assertRaises((ConnectionError, http.client.HTTPException)):
                    connection.request('GET', '/')
                    connection.getresponse()
            finally:
                connection.close()
        finally:
            emu.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class StrictEvidenceTests(unittest.TestCase):
    def valid_evidence(self):
        trace = NetworkTrace.generate(seed=2, steps=[30], probes_per_step=1)
        row = {'task': 'complex', 'backend': 'cloud-model', 'trace_id': trace.trace_id,
               'trace_index': 0, 'source': 'stub-assumption', 'probe_ms': 1.0,
               'policy_ms': 0.1, 'backend_ms': 3.0, 'total_user_ms': 4.1}
        return {'meta': {'generated_at': 'now', 'note': 'stub'},
                'stub_parameters': {'cloud_compute_ms': 300.0},
                'harness': {'infer_n_per_task_per_phase': 1},
                'summaries': [{'phase': 'P1', 'arm': arm, 'overall': {
                    'probe_median_ms': 1.0, 'policy_median_ms': 0.1,
                    'backend_median_ms': 3.0, 'total_user_median_ms': 4.1}}
                    for arm in ('adaptive', 'static-cloud')],
                'paired_traces': {'P1': trace.to_dict()},
                'raw_runs': [{'phase': 'P1', 'arm': arm, 'rows': [copy.deepcopy(row)]}
                             for arm in ('adaptive', 'static-cloud')]}

    def test_valid_raw_rows_and_trace_hash_pass(self):
        self.assertEqual(validate_results_schema(self.valid_evidence()), [])

    def test_missing_raw_rows_and_source_are_rejected(self):
        data = self.valid_evidence()
        del data['raw_runs']
        self.assertTrue(any('raw_runs' in e for e in validate_results_schema(data)))
        data = self.valid_evidence()
        del data['raw_runs'][0]['rows'][0]['source']
        self.assertTrue(any('source' in e for e in validate_results_schema(data)))

    def test_trace_tampering_empty_trace_and_out_of_range_reference_are_rejected(self):
        data = self.valid_evidence()
        data['paired_traces']['P1']['samples'][0]['jitter_ms'] = 999
        self.assertTrue(any('TRACE_ID_MISMATCH' in e for e in validate_results_schema(data)))
        data = self.valid_evidence()
        data['paired_traces']['P1']['samples'] = []
        self.assertTrue(validate_results_schema(data))
        data = self.valid_evidence()
        data['raw_runs'][0]['rows'][0]['trace_index'] = 1
        self.assertTrue(any('trace_index' in e for e in validate_results_schema(data)))

    def test_nonfinite_latency_and_wrong_sum_are_rejected(self):
        data = self.valid_evidence()
        data['raw_runs'][0]['rows'][0]['total_user_ms'] = 42
        self.assertTrue(any('total_user_ms' in e for e in validate_results_schema(data)))
        data = self.valid_evidence()
        data['raw_runs'][0]['rows'][0]['probe_ms'] = float('nan')
        self.assertTrue(any('probe_ms' in e for e in validate_results_schema(data)))

    def test_missing_paired_arm_is_rejected(self):
        data = self.valid_evidence()
        data['raw_runs'].pop()
        self.assertTrue(any('paired' in e for e in validate_results_schema(data)))

    def test_malformed_trace_object_is_reported_without_validator_crash(self):
        data = self.valid_evidence()
        data['paired_traces']['P1'] = 'invalid object'
        self.assertTrue(validate_results_schema(data))


class BenchReplayTests(unittest.TestCase):
    def test_inference_row_names_source_and_exact_replay_outcome(self):
        class Driver:
            def decide(self, task):
                return 'cloud-model', 'WAN_OK', 'test'

        class ReplayRecorder:
            def set_replay(self, sample, role, link='wan'):
                self.outcome = sample.outcome(role, link)

        bench = offload_bench.Bench.__new__(offload_bench.Bench)
        bench.args = types.SimpleNamespace(wan_port=1, lan_port=2)
        bench.timeout = 1
        bench.wan = ReplayRecorder()
        bench.lan = ReplayRecorder()
        bench.probe_links = lambda drv, do_lan=True: {
            'wan': {'ok': True, 'rtt_ms': 4}, 'lan': {'ok': True, 'rtt_ms': 1}}
        trace = NetworkTrace.generate(seed=2, steps=[30], probes_per_step=1, loss_max_pct=0)
        with mock.patch.object(offload_bench, 'http_post', return_value=(True, 4, {'stub_compute_ms': 1, 'handler_ms': 1})):
            row = bench.infer_once(Driver(), 'complex', 'adaptive', sample=trace.samples[0],
                                   trace_id=trace.trace_id, trace_index=0)
        self.assertEqual(row['source'], 'stub-assumption')
        self.assertEqual(row['replay_outcome'], trace.samples[0].outcome('infer', 'wan'))
        self.assertEqual(bench.wan.outcome, row['replay_outcome'])


if __name__ == '__main__':
    unittest.main()
