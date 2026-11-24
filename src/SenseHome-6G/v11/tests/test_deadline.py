"""Absolute budgets use only local fixtures and read/planning callables."""
import copy
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routing import RoutedPlanner
from ai6g_runtime.router import TaskRouter


VIEW = dict(request='打开阅读灯', catalog=[dict(entity_id='light.reading', kind='light',
    room='书房', name='阅读灯', aliases=[], available=True)], states={
    'light.reading': dict(state='off', attributes={}, last_updated='v0')})
ON = dict(result_kind='proposal', actions=[dict(entity_id='light.reading',
    service='light.turn_on', data={})], reason='local fixture')


def deadline_module(test):
    test.assertIsNotNone(importlib.util.find_spec('deadline'),
                         'absolute deadline / bounded isolation helper is missing')
    import deadline
    return deadline


def wait_until(predicate, timeout=1):
    until = time.monotonic() + timeout
    while not predicate() and time.monotonic() < until:
        threading.Event().wait(.005)
    return predicate()


class BoundedCallTests(unittest.TestCase):
    def test_timeout_keeps_actual_worker_capacity_until_callable_finishes(self):
        mod = deadline_module(self)
        pool = mod.BoundedCallPool(max_workers=1)
        entered = threading.Event(); release = threading.Event(); finished = threading.Event()
        calls = []
        def blocked():
            entered.set()
            try:
                release.wait(2)
                return {'late': 'must be discarded'}
            finally:
                finished.set()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                mod.call_with_timeout(blocked, .05, pool=pool)
            self.assertLess(time.monotonic() - started, .25)
            self.assertTrue(entered.is_set())
            for _ in range(5):
                with self.assertRaises(mod.CapacityExceeded):
                    pool.submit(lambda: calls.append('should not run'))
            self.assertEqual(calls, [])
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertTrue(wait_until(lambda: pool.active_count == 0))
        self.assertEqual(mod.call_with_timeout(lambda: 'fresh', .2, pool=pool), 'fresh')

    def test_submit_result_and_error_are_real_callable_results(self):
        mod = deadline_module(self)
        pool = mod.BoundedCallPool(max_workers=1)
        self.assertEqual(pool.submit(lambda a: a + 1, 3).result(timeout=1), 4)
        def broken():
            raise ValueError('fixture error')
        with self.assertRaisesRegex(ValueError, 'fixture error'):
            mod.call_with_timeout(broken, .2, pool=pool)


class HTTPDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.disconnected = threading.Event()
        self.request_received = threading.Event()
        self.received = []
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                test.received.append(self.path)
                test.request_received.set()
                if self.path == '/error':
                    self.send_response(503); self.send_header('Content-Length', '0'); self.end_headers()
                    return
                if self.path == '/ok':
                    self.send_response(200); self.send_header('Content-Length', '5'); self.end_headers()
                    self.wfile.write(b'hello')
                    return
                try:
                    if self.path == '/headers':
                        for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n':
                            self.connection.sendall(bytes([byte])); time.sleep(.025)
                    else:
                        self.send_response(200); self.send_header('Content-Length', '100'); self.end_headers()
                    for _ in range(100):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.025)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    test.disconnected.set()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(1)

    def test_slow_bytes_cannot_extend_total_body_budget(self):
        mod = deadline_module(self)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            mod.request_bytes(Request(self.url + '/slow'), timeout=.09, max_bytes=1024)
        self.assertLess(time.monotonic() - started, .30)
        self.assertTrue(self.disconnected.wait(.5), 'expired response socket stayed open')

    def test_slow_headers_are_also_cut_off_at_total_deadline(self):
        mod = deadline_module(self)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            mod.request_bytes(Request(self.url + '/headers'), timeout=.09, max_bytes=1024)
        self.assertLess(time.monotonic() - started, .30)
        self.assertTrue(self.disconnected.wait(.5), 'expired header socket stayed open')

    def test_connection_wait_is_bounded_and_late_connect_sends_no_request(self):
        mod = deadline_module(self)
        original = socket.create_connection
        entered = threading.Event(); release = threading.Event(); ended = threading.Event()
        completed = threading.Event(); outcomes = []
        budget = .8
        def delayed_connect(*args, **kwargs):
            entered.set()
            release.wait(2)
            try:
                return original(*args, **kwargs)
            finally:
                ended.set()
        def request():
            started = time.monotonic()
            try:
                mod.request_bytes(Request(self.url + '/ok'), timeout=budget, max_bytes=1024)
                outcomes.append(('success', time.monotonic() - started))
            except Exception as exc:
                outcomes.append((exc, time.monotonic() - started))
            finally:
                completed.set()
        worker = threading.Thread(target=request, daemon=True)
        try:
            with patch('socket.create_connection', delayed_connect):
                worker.start()
                # SSL/opener initialization is inside the real HTTP deadline.
                # Observe entry into the connection phase explicitly instead
                # of assuming that initialization always takes under 50ms.
                self.assertTrue(entered.wait(.6), 'connection phase not reached within setup allowance')
                self.assertFalse(completed.is_set(), 'connection fixture did not actually block')
                self.assertTrue(completed.wait(1.0), 'HTTP wait exceeded its total deadline')
                error, elapsed = outcomes[0]
                self.assertIsInstance(error, TimeoutError)
                self.assertGreaterEqual(elapsed, budget * .8)
                self.assertLess(elapsed, budget + .4)
                release.set()
                self.assertTrue(ended.wait(1))
            self.assertFalse(self.request_received.wait(.05), 'late connection sent an HTTP request')
            self.assertEqual(self.received, [])
        finally:
            release.set()
            worker.join(2)

    def test_success_and_response_size_limit(self):
        mod = deadline_module(self)
        self.assertEqual(mod.request_bytes(Request(self.url + '/ok'), timeout=.3, max_bytes=5), b'hello')
        with self.assertRaisesRegex(ValueError, 'large|size'):
            mod.request_bytes(Request(self.url + '/ok'), timeout=.3, max_bytes=4)

    def test_http_error_status_keeps_urllib_error_contract(self):
        mod = deadline_module(self)
        with self.assertRaises(HTTPError) as raised:
            mod.request_bytes(Request(self.url + '/error'), timeout=.3, max_bytes=1024)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()


class RoutedDeadlineTests(unittest.TestCase):
    def observe(self, planner):
        planner.observe('local-model', reachable=True, latency_ms=1, quality_score=.95,
                        calibration_source_sha256='a' * 64)

    def test_blocked_health_probe_cannot_outlive_route_budget_or_publish_late_health(self):
        release = threading.Event(); ended = threading.Event()
        def backend(view):
            release.wait(2)
            try:
                return copy.deepcopy(ON)
            finally:
                ended.set()
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False, deadline_ms=70,
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        started = time.monotonic()
        try:
            result = planner(copy.deepcopy(VIEW))
            self.assertLess(time.monotonic() - started, .25)
            self.assertEqual(result['actions'], ON['actions'])
            observed = planner.router.cache.get('local-model')
            self.assertFalse(observed.reachable)
            route = planner.last_route
        finally:
            release.set()
        self.assertTrue(ended.wait(1))
        self.assertFalse(planner.router.cache.get('local-model').reachable)
        self.assertEqual(planner.last_route, route)

    def test_blocked_resource_provider_is_in_same_total_route_budget(self):
        release = threading.Event()
        def resources(name, remaining):
            release.wait(2)
            return dict(source='too late', observed_at=time.time(), inflight=0)
        planner = RoutedPlanner({'local-model': lambda view: copy.deepcopy(ON)}, prefer_rules=False,
            deadline_ms=70, resource_provider=resources,
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        started = time.monotonic()
        try:
            planner(copy.deepcopy(VIEW))
            self.assertLess(time.monotonic() - started, .25)
            self.assertIsNone(planner.router.cache.get('local-model').resources)
        finally:
            release.set()

    def test_probe_lock_wait_has_total_budget(self):
        held = threading.Event(); release = threading.Event()
        planner = RoutedPlanner({'local-model': lambda view: copy.deepcopy(ON)},
                                prefer_rules=False, deadline_ms=70)
        def hold():
            with planner._probe_lock:
                held.set(); release.wait(2)
        thread = threading.Thread(target=hold, daemon=True); thread.start()
        self.assertTrue(held.wait(1))
        started = time.monotonic()
        try:
            planner(copy.deepcopy(VIEW))
            self.assertLess(time.monotonic() - started, .25)
        finally:
            release.set(); thread.join(1)

    def test_timed_out_model_keeps_runtime_reservation_until_actual_completion(self):
        calls = []; release = threading.Event(); ended = threading.Event()
        def backend(view):
            calls.append(view['request'])
            try:
                release.wait(2)
                return copy.deepcopy(ON)
            finally:
                ended.set()
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False, deadline_ms=70,
                                router=TaskRouter(max_inflight=1))
        self.observe(planner)
        started = time.monotonic()
        try:
            planner(copy.deepcopy(VIEW))
            self.assertLess(time.monotonic() - started, .25)
            self.assertEqual(planner.router.inflight_snapshot(), {'local-model': 1})
            planner(copy.deepcopy(VIEW))
            self.assertEqual(len(calls), 1)
            route = planner.last_route
        finally:
            release.set()
        self.assertTrue(ended.wait(1))
        self.assertTrue(wait_until(lambda: planner.router.inflight_snapshot() == {}))
        self.assertEqual(planner.last_route, route)

    def test_late_user_call_blocks_stale_health_probe_at_same_runtime_limit(self):
        clock = [1000.0]; calls = []; release = threading.Event()
        def backend(view):
            calls.append('probe' if not view['catalog'] else 'user')
            release.wait(2)
            return copy.deepcopy(ON)
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False, deadline_ms=70,
            router=TaskRouter(max_inflight=1), now=lambda: clock[0],
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        self.observe(planner)
        try:
            planner(copy.deepcopy(VIEW))
            self.assertEqual(planner.router.inflight_snapshot(), {'local-model': 1})
            clock[0] += 6  # Expire the observation, not the running inference.
            planner(copy.deepcopy(VIEW))
            self.assertEqual(calls, ['user'], 'health probe bypassed occupied backend capacity')
            self.assertEqual(planner.router.inflight_snapshot(), {'local-model': 1})
        finally:
            release.set()
        self.assertTrue(wait_until(lambda: planner.router.inflight_snapshot() == {}))

    def test_late_health_probe_reserves_runtime_capacity_until_it_actually_exits(self):
        calls = []; release = threading.Event()
        def backend(view):
            calls.append('probe' if not view['catalog'] else 'user')
            release.wait(2)
            return copy.deepcopy(ON)
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False, deadline_ms=70,
            router=TaskRouter(max_inflight=1),
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        try:
            planner(copy.deepcopy(VIEW))
            self.assertEqual(calls, ['probe'])
            self.assertEqual(planner.router.inflight_snapshot(), {'local-model': 1})
            self.observe(planner)  # A fresh observation cannot free a late worker.
            planner(copy.deepcopy(VIEW))
            self.assertEqual(calls, ['probe'], 'user inference bypassed an occupied probe slot')
        finally:
            release.set()
        self.assertTrue(wait_until(lambda: planner.router.inflight_snapshot() == {}))

    def test_probe_releases_reserved_token_when_no_worker_is_admitted(self):
        mod = deadline_module(self)
        pool = mod.BoundedCallPool(max_workers=1)
        entered = threading.Event(); release = threading.Event(); calls = []
        def occupy():
            entered.set(); release.wait(2)
        occupied = pool.submit(occupy)
        self.assertTrue(entered.wait(1))
        planner = RoutedPlanner({'local-model': lambda view: calls.append('probe')},
            prefer_rules=False, deadline_ms=70, router=TaskRouter(max_inflight=1),
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        try:
            with patch('routing._ROUTE_CALL_POOL', pool):
                planner(copy.deepcopy(VIEW))
            self.assertEqual(calls, [])
            self.assertEqual(planner.router.inflight_snapshot(), {})
            self.assertIsNone(planner.router.cache.get('local-model'))
        finally:
            release.set()
            occupied.result(timeout=1)

    def test_public_router_without_legacy_capacity_fields_skips_probe_and_routes_fresh_health(self):
        class PublicRouter:
            def __init__(self):
                self.inner = TaskRouter(max_inflight=1)
                self.cache = self.inner.cache
                self.trusted_backends = self.inner.trusted_backends
            def choose_and_reserve(self, *args, **kwargs):
                return self.inner.choose_and_reserve(*args, **kwargs)
            def release(self, token):
                return self.inner.release(token)
        calls = []
        def backend(view):
            calls.append('probe' if not view['catalog'] else 'user')
            return copy.deepcopy(ON)
        planner = RoutedPlanner({'local-model': backend}, prefer_rules=False, router=PublicRouter(),
            calibrations={'local-model': dict(quality_score=.95, source_sha256='a' * 64)})
        planner(copy.deepcopy(VIEW))
        self.assertEqual(calls, [])
        self.observe(planner)
        self.assertEqual(planner(copy.deepcopy(VIEW))['actions'], ON['actions'])
        self.assertEqual(calls, ['user'])
        self.assertEqual(planner.router.inner.inflight_snapshot(), {})

    def test_thread_local_model_metrics_survive_worker_boundary(self):
        class Backend:
            def __init__(self): self.local = threading.local()
            @property
            def last_metrics(self): return getattr(self.local, 'metrics', {})
            def __call__(self, view):
                self.local.metrics = {'total_tokens': 17}
                return copy.deepcopy(ON)
        planner = RoutedPlanner({'local-model': Backend()}, prefer_rules=False)
        self.observe(planner)
        planner(copy.deepcopy(VIEW))
        self.assertEqual(planner.last_route['metrics']['total_tokens'], 17)

    def test_explicit_model_extension_can_pass_rule_clarification_to_model(self):
        calls = []
        def backend(view):
            calls.append(view['request']); return copy.deepcopy(ON)
        view = copy.deepcopy(VIEW); view['request'] = '阅读灯安排成适合看书的样子'
        strict = RoutedPlanner({'local-model': backend}, prefer_rules=False)
        self.observe(strict)
        self.assertEqual(strict(view)['result_kind'], 'clarification')
        self.assertEqual(calls, [])
        extended = RoutedPlanner({'local-model': backend}, allow_model_proposals=True)
        self.observe(extended)
        self.assertEqual(extended(view)['actions'], ON['actions'])
        self.assertEqual(calls, [view['request']])


if __name__ == '__main__':
    unittest.main()
