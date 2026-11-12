import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bridge_server
from offload_gateway import HttpJsonBackend, LinkSnapshot, OffloadGateway, RuleBackend, SubprocessPolicyClient


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.dumps({'answer': '已调到阅读灯光', 'actions': [
            {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 75}},
            {'name': 'light_control', 'params': {'location': '卧室', 'brightness': 20}},
        ], 'semantic_complete': True, 'source': 'real-model',
            'upstream_model': 'fake-vllm-model', 'model_ms': 12.5,
            'model_ms_kind': 'upstream_http_wall_clock',
            'gpu_info_source': 'environment_declared'}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class RemoteExecutionTests(unittest.TestCase):
    def test_state_persistence_retries_transient_replace_permission_denial(self):
        original_file = bridge_server.STATE_FILE
        original_replace = bridge_server.os.replace
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / 'device_state.json')
                attempts = []

                def replace_once(source, target):
                    attempts.append((source, target))
                    if len(attempts) == 1:
                        raise PermissionError('temporary destination lock')
                    return original_replace(source, target)

                expected = bridge_server.default_state()
                expected['lights']['客厅'] = {'is_on': True, 'brightness': 100}
                with mock.patch.object(bridge_server.os, 'replace', side_effect=replace_once):
                    bridge_server.save_state(expected)

                self.assertEqual(len(attempts), 2)
                self.assertEqual(bridge_server.load_state(), expected)
        finally:
            bridge_server.STATE_FILE = original_file

    def test_concurrent_local_commands_persist_without_writer_races(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / 'device_state.json')
                bridge_server.save_state(bridge_server.default_state())
                with ThreadPoolExecutor(max_workers=32) as pool:
                    results = list(pool.map(
                        bridge_server._gateway_local_handler,
                        ['打开客厅的灯'] * 128,
                    ))

                self.assertEqual(len(results), 128)
                self.assertTrue(all(result['semantic_complete'] for result in results))
                self.assertEqual(bridge_server.load_state()['lights']['客厅']['brightness'], 100)
        finally:
            bridge_server.STATE_FILE = original_file

    def test_local_fallback_never_executes_only_part_of_a_compound_command(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / 'device_state.json')
                bridge_server.save_state(bridge_server.default_state())
                before = bridge_server.load_state()
                for command in ('打开客厅灯然后把卧室灯调亮一点', '打开客厅和卧室的灯',
                                '如果卧室温度超过28度就打开窗帘', '不要关闭客厅的灯'):
                    with self.subTest(command=command):
                        result = bridge_server._gateway_local_handler(command)
                        self.assertFalse(result['semantic_complete'])
                        self.assertEqual(result['actions'], [])
                        self.assertEqual(bridge_server.load_state(), before)
        finally:
            bridge_server.STATE_FILE = original_file

    def test_safety_intent_does_not_claim_success_from_partial_keyword_match(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / 'device_state.json')
                bridge_server.save_state(bridge_server.default_state())
                result = bridge_server._gateway_local_handler('打开客厅灯并关闭燃气阀门')
                self.assertFalse(result['semantic_complete'])
                self.assertEqual(result['actions'], [])
                self.assertFalse(bridge_server.load_state()['lights']['客厅']['is_on'])
        finally:
            bridge_server.STATE_FILE = original_file

    def test_device_persistence_failure_is_not_reported_as_success(self):
        original_file = bridge_server.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = folder
                with self.assertRaises(OSError):
                    bridge_server._gateway_action_executor([
                        {'name': 'light_control', 'params': {'location': '客厅', 'action': 'on'}},
                    ], {'_receipt_command': '打开客厅灯'})
        finally:
            bridge_server.STATE_FILE = original_file

    def test_real_policy_http_response_executes_validated_device_action(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cli = Path(bridge_server.NET_OFFLOAD_DIR) / ('policy_cli.exe' if os.name == 'nt' else 'policy_cli')
        policy = SubprocessPolicyClient(str(cli))
        original_gateway, original_file = bridge_server.gateway, bridge_server.STATE_FILE
        original_monitor = bridge_server.backend_monitor
        try:
            # This test injects a complete gateway fixture, not the module's
            # independently initialised default backend monitor.
            bridge_server.backend_monitor = None
            with tempfile.TemporaryDirectory() as folder:
                bridge_server.STATE_FILE = str(Path(folder) / 'device_state.json')
                bridge_server.save_state(bridge_server.default_state())
                bridge_server.gateway = OffloadGateway(
                    policy=policy,
                    backends={'cloud-model': HttpJsonBackend(f'http://127.0.0.1:{server.server_port}', 'cloud-model'),
                              'local-rules': RuleBackend(handler=bridge_server._gateway_local_handler)},
                    link_snapshot=LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=0),
                    action_executor=bridge_server._gateway_action_executor,
                )
                # Exercise the lower planner/executor boundary deliberately;
                # production process_command compiles this explicit plan.
                event = bridge_server.gateway.handle('把客厅灯光设为75%，再把卧室灯光设为20%')
                response = {**event, **event['response']}
                state = bridge_server.load_state()
                self.assertEqual(response['backend'], 'cloud-model')
                self.assertTrue(response['semantic_complete'])
                self.assertEqual(response['source'], 'real-model')
                self.assertEqual(response['upstream_model'], 'fake-vllm-model')
                self.assertEqual(response['model_ms_kind'], 'upstream_http_wall_clock')
                self.assertEqual(response['gpu_info_source'], 'environment_declared')
                self.assertEqual(state['lights']['客厅']['brightness'], 75)
                self.assertIn('75', response['observation'])
        finally:
            bridge_server.gateway, bridge_server.STATE_FILE = original_gateway, original_file
            bridge_server.backend_monitor = original_monitor
            policy.close()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_partial_wrapper_health_drives_real_policy_before_inference(self):
        class Monitor:
            ready, inflight = False, 0

            def snapshot(self):
                return {'age_ms': 1, 'backends': {'cloud-model': {
                    'ready': self.ready, 'inflight': self.inflight,
                    'queue_ms': 0, 'queue_scope': 'wrapper_fail_fast_admission',
                    'upstream_queue_ms': None,
                }}}

        cli = Path(bridge_server.NET_OFFLOAD_DIR) / ('policy_cli.exe' if os.name == 'nt' else 'policy_cli')
        policy = SubprocessPolicyClient(str(cli))
        monitor = Monitor()
        original_gateway, original_monitor = bridge_server.gateway, bridge_server.backend_monitor
        try:
            bridge_server.backend_monitor = monitor
            bridge_server.gateway = OffloadGateway(
                policy=policy,
                backends={name: RuleBackend(handler=lambda command, context: {'answer': 'model fixture', 'actions': []})
                          for name in ('cloud-model', 'local-model', 'local-rules')},
                link_snapshot=LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True, lan_rtt_ms=0),
            )
            for ready, inflight, expected_backend, expected_reason in (
                (False, 0, 'local-model', 'MODEL_UNAVAILABLE'),
                (True, 9, 'local-model', 'COMPUTE_OVERLOADED'),
                (True, 0, 'cloud-model', 'WAN_OK'),
            ):
                with self.subTest(ready=ready, inflight=inflight):
                    monitor.ready, monitor.inflight = ready, inflight
                    result = bridge_server.process_command('请把客厅调整为阅读模式')
                    self.assertEqual(result['backend'], expected_backend)
                    self.assertEqual(result['reason'], expected_reason)
                    self.assertIsNone(result['compute']['free_mem_mb'])
                    self.assertIsNone(result['compute']['queue_ms'])
                    self.assertIn('free_mem_mb', result['compute']['unknown_fields'])
                    self.assertEqual(result['compute']['field_sources']['inflight'], 'wrapper_inflight')
        finally:
            bridge_server.gateway, bridge_server.backend_monitor = original_gateway, original_monitor
            policy.close()


if __name__ == '__main__':
    unittest.main()
