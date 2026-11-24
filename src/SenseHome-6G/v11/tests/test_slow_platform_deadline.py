"""Slow local HTTP fixture for HA deadline and cross-entity progress regressions."""
import copy
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller
from native_ha_adapter import NativeHAAdapter
from platform_execution import HAClient


class SlowPlatformDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.states = {
            'light.a': dict(entity_id='light.a', state='off', attributes={'supported_color_modes': ['onoff']}, last_updated='v0'),
            'light.b': dict(entity_id='light.b', state='off', attributes={'supported_color_modes': ['onoff']}, last_updated='v0'),
        }
        self.calls = []
        self.slow_get = False
        self.slow_post_started = threading.Event()
        self.slow_post_finished = threading.Event()
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value, slow=False):
                body = json.dumps(value).encode() + (b' ' * 36 if slow else b'')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    if slow:
                        test.slow_post_started.set()
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.02)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    if slow:
                        test.slow_post_finished.set()

            def do_GET(self):
                entity = self.path.removeprefix('/api/states/')
                test.calls.append(('GET', entity))
                self.send_json(copy.deepcopy(test.states[entity]), slow=test.slow_get and entity == 'light.a')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                entity = body['entity_id']
                test.calls.append(('POST', entity))
                # Deliberately leave light.a unchanged so the timed-out POST is
                # uncertain and must not be retried without reconciliation.
                if entity == 'light.b':
                    test.states[entity].update(state='on', last_updated='v1')
                self.send_json([], slow=entity == 'light.a')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.temp.cleanup()

    def test_slow_streaming_response_obeys_absolute_ha_timeout(self):
        self.slow_get = True
        client = HAClient(self.url, 'fixture-token', timeout=.09)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.get_state_with_timeout('light.a', .09)
        self.assertLess(time.monotonic() - started, .35)

    def test_slow_post_does_not_block_other_entity_or_get_retried(self):
        client = NativeHAAdapter(HAClient(self.url, 'fixture-token', timeout=.12))
        catalog = [
            dict(entity_id='light.a', kind='light', room='书房', name='阅读灯', aliases=[], available=True),
            dict(entity_id='light.b', kind='light', room='客厅', name='客厅灯', aliases=[], available=True),
        ]
        app = Controller(self.temp.name, catalog, client,
                         execution_options={'verify_timeout_seconds': .06, 'poll_interval_seconds': .01})
        cards = [app.propose('打开阅读灯'), app.propose('打开客厅灯')]
        self.assertTrue(all(card['status'] == 'awaiting_confirmation' for card in cards))

        def confirm(index):
            card = cards[index]
            return app.confirm(dict(proposal_id=card['proposal_id'], digest=card['digest'],
                                    request_id='slow-' + str(index), confirmed=True))

        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(confirm, 0)
            self.assertTrue(self.slow_post_started.wait(2))
            unrelated = pool.submit(confirm, 1)
            other_result = unrelated.result(timeout=.6)
            slow_result = slow.result(timeout=1)
        self.assertEqual(other_result['status'], 'succeeded')
        self.assertEqual(slow_result['status'], 'unknown')
        self.assertEqual(sum(method == 'POST' and entity == 'light.a' for method, entity in self.calls), 1)
        self.assertTrue(self.slow_post_finished.wait(.5))
        self.assertEqual(app.reconcile('slow-0')['status'], 'unknown')
        self.assertEqual(sum(method == 'POST' and entity == 'light.a' for method, entity in self.calls), 1)


if __name__ == '__main__':
    unittest.main()
