"""Regression tests use software devices and explicit model fixtures only."""
import copy
import importlib
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from controller import Controller, SoftwareClient
from platform_execution import UnknownEntity


CATALOG = [dict(entity_id='light.reading', kind='light', room='书房',
                name='阅读灯', aliases=[], available=True)]
ON = dict(result_kind='proposal', actions=[dict(entity_id='light.reading',
          service='light.turn_on', data={})], reason='fixture candidate')


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = SoftwareClient({'light.reading': dict(state='off', attributes={}, last_updated='v0')})

    def tearDown(self):
        self.temp.cleanup()

    def app(self, **kwargs):
        return Controller(self.temp.name, CATALOG, self.client, **kwargs)

    def confirm(self, app, card):
        return app.confirm(dict(proposal_id=card['proposal_id'], digest=card['digest'],
                                request_id='one', confirmed=True))

    def routing(self):
        source = pathlib.Path(__file__).resolve().parents[1] / 'routing.py'
        self.assertTrue(source.exists(), 'v10 routing integration is missing')
        return importlib.import_module('routing')

    def test_unrelated_unavailable_entity_does_not_block_target(self):
        catalog = copy.deepcopy(CATALOG) + [dict(entity_id='light.missing', kind='light',
            room='卧室', name='卧室灯', aliases=[], available=False)]
        app = Controller(self.temp.name, catalog, self.client)
        try:
            card = app.propose('打开阅读灯')
        except UnknownEntity:
            self.fail('unrelated missing entity prevented the target proposal')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        self.assertEqual(self.confirm(app, card)['status'], 'succeeded')
        self.assertEqual(self.client.calls, 1)

    def test_structurally_valid_but_wrong_model_plan_is_blocked(self):
        app = self.app(planner=lambda view: copy.deepcopy(ON))
        result = app.propose('不要打开阅读灯')
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(result['payload']['actions'], [])
        self.assertEqual(self.client.calls, 0)

    def test_model_failure_falls_back_to_verified_rules(self):
        def broken(view):
            raise TimeoutError('model fixture unavailable')
        app = self.app(planner=broken)
        try:
            card = app.propose('打开阅读灯')
        except TimeoutError:
            self.fail('model failure was not handled by the controller')
        self.assertEqual(card['status'], 'awaiting_confirmation')
        self.assertEqual(card['metadata']['route']['backend'], 'local-rules')
        self.assertEqual(self.confirm(app, card)['status'], 'succeeded')

    def test_related_missing_entity_yields_non_executable_clarification(self):
        app = Controller(self.temp.name, CATALOG, SoftwareClient({}))
        try:
            card = app.propose('打开阅读灯')
        except UnknownEntity:
            self.fail('missing target should be a structured clarification')
        self.assertEqual(card['status'], 'clarification')
        self.assertFalse(card['authorized'])

    def test_persistent_software_instances_do_not_overwrite_other_entities(self):
        import json
        path = pathlib.Path(self.temp.name) / 'states.json'
        states = {name: dict(state='off', attributes={}, last_updated='v0')
                  for name in ('light.a', 'light.b')}
        path.write_text(json.dumps(states), encoding='utf8')
        first = SoftwareClient(states, path)
        second = SoftwareClient(states, path)
        first.call_service('light.a', 'light.turn_on', {})
        second.call_service('light.b', 'light.turn_on', {})
        reopened = SoftwareClient(states, path)
        self.assertEqual(reopened.get_state('light.a')['state'], 'on')
        self.assertEqual(reopened.get_state('light.b')['state'], 'on')
        self.assertEqual(first.get_state('light.b')['state'], 'on')

    def test_parallel_software_instances_preserve_both_updates(self):
        import json
        from concurrent.futures import ThreadPoolExecutor
        path = pathlib.Path(self.temp.name) / 'parallel.json'
        states = {name: dict(state='off', attributes={}, last_updated='v0')
                  for name in ('light.a', 'light.b')}
        path.write_text(json.dumps(states), encoding='utf8')
        clients = [SoftwareClient(states, path), SoftwareClient(states, path)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.call_service, entity, 'light.turn_on', {})
                       for client, entity in zip(clients, states)]
            for future in futures:
                future.result(timeout=5)
        final = SoftwareClient(states, path)
        self.assertEqual([final.get_state(entity)['state'] for entity in states], ['on', 'on'])

    def test_controller_timing_includes_both_state_read_phases(self):
        original = self.client.get_state
        def slow_state(entity):
            time.sleep(.01)
            return original(entity)
        self.client.get_state = slow_state
        card = self.app().propose('打开阅读灯')
        self.assertIn('timing', card['metadata'])
        timing = card['metadata']['timing']
        self.assertGreaterEqual(timing['snapshot_ms'], 9)
        self.assertGreaterEqual(timing['refresh_snapshot_ms'], 9)
        self.assertGreaterEqual(card['handler_total_ms'], 20)

    def make_routed(self, **kwargs):
        module = self.routing()
        calls = []
        def backend(name):
            def call(view):
                calls.append(name)
                return copy.deepcopy(ON)
            return call
        planner = module.RoutedPlanner(
            {'local-model': backend('local-model'), 'cloud-model': backend('cloud-model')},
            prefer_rules=False, **kwargs)
        return planner, calls

    def observe(self, planner, name, *, latency=10, queue=0, checked_at=None):
        planner.observe(name, reachable=True, latency_ms=latency, quality_score=.95,
            calibration_source_sha256='a' * 64, checked_at=checked_at,
            resources=dict(queue_ms=queue, free_mem_mb=2048, inflight=0,
                           source='explicit test fixture', observed_at=time.time()))

    def test_resource_route_is_bound_to_card_and_reaches_execution(self):
        planner, calls = self.make_routed()
        self.observe(planner, 'local-model', latency=5, queue=500)
        self.observe(planner, 'cloud-model', latency=15)
        app = self.app(planner=planner)
        card = app.propose('打开阅读灯')
        self.assertEqual(calls, ['cloud-model'])
        self.assertEqual(card['metadata']['route']['backend'], 'cloud-model')
        self.assertEqual(self.confirm(app, card)['status'], 'succeeded')
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(app.receipt('one')['metadata']['route']['backend'], 'cloud-model')

    def test_stale_observations_prevent_model_call(self):
        planner, calls = self.make_routed()
        self.observe(planner, 'local-model', checked_at=time.time() - 60)
        payload = planner(dict(request='打开阅读灯', catalog=CATALOG,
                               states=self.app().snapshot()))
        self.assertEqual(calls, [])
        self.assertEqual(payload['result_kind'], 'proposal')
        self.assertEqual(planner.last_route['backend'], 'local-rules')

    def test_strict_privacy_never_calls_untrusted_cloud(self):
        planner, calls = self.make_routed(privacy='strict', trusted_backends={'local-model'})
        self.observe(planner, 'local-model', latency=20)
        self.observe(planner, 'cloud-model', latency=1)
        card = self.app(planner=planner).propose('打开阅读灯')
        self.assertEqual(calls, ['local-model'])
        self.assertEqual(card['metadata']['route']['backend'], 'local-model')

    def test_route_that_cannot_meet_deadline_does_not_call_model(self):
        planner, calls = self.make_routed(deadline_ms=5)
        self.observe(planner, 'local-model', latency=50)
        planner(dict(request='打开阅读灯', catalog=CATALOG, states=self.app().snapshot()))
        self.assertEqual(calls, [])
        self.assertEqual(planner.last_route['backend'], 'local-rules')

    def test_wrong_model_candidate_is_replaced_by_safe_clarification(self):
        module = self.routing()
        wrong = copy.deepcopy(ON)
        wrong['actions'][0]['service'] = 'light.turn_off'
        planner = module.RoutedPlanner({'local-model': lambda view: wrong}, prefer_rules=False)
        self.observe(planner, 'local-model')
        result = self.app(planner=planner).propose('打开阅读灯')
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.client.calls, 0)

    def test_failed_model_call_keeps_its_cost_in_fallback_receipt(self):
        module = self.routing()
        class FailedBackend:
            last_metrics = {}
            def __call__(self, view):
                self.last_metrics = dict(request_bytes=512, response_bytes=15,
                                         elapsed_ms=2, error_type='ValueError')
                raise ValueError('invalid JSON model fixture')
        planner = module.RoutedPlanner({'local-model': FailedBackend()}, prefer_rules=False)
        self.observe(planner, 'local-model')
        card = self.app(planner=planner).propose('打开阅读灯')
        route = card['metadata']['route']
        self.assertEqual(route['backend'], 'local-rules')
        self.assertIn('metrics', route['selection'])
        self.assertEqual(route['selection']['metrics']['request_bytes'], 512)
        self.assertGreaterEqual(route['selection']['model_call_ms'], 0)

    def test_software_write_lock_wait_cannot_outlive_original_authorization(self):
        import json
        import sqlite3
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from platform_execution import ExecutionStore
        path = pathlib.Path(self.temp.name) / 'locked.json'
        states = {'light.reading': dict(state='off', attributes={}, last_updated='v0')}
        path.write_text(json.dumps(states), encoding='utf8')
        entered = threading.Event()
        class CheckedClient(SoftwareClient):
            def call_service_checked(self, *args, **kwargs):
                entered.set()
                return super().call_service_checked(*args, **kwargs)
        client = CheckedClient(states, path)
        self.assertTrue(hasattr(SoftwareClient, 'call_service_checked'),
                        'software adapter has no authorization recheck at its state write boundary')
        clock = [100.0]
        store = ExecutionStore(pathlib.Path(self.temp.name) / 'expiry.sqlite', client,
                               now=lambda: clock[0], verify_timeout_seconds=0)
        expected = store.snapshot(['light.reading'])
        blocker = sqlite3.connect(client.state_db)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(store.execute, 'write-expiry', ON['actions'], expected,
                                     authorized=True, authorization_expires_at=130)
                try:
                    self.assertTrue(entered.wait(5))
                    clock[0] = 131.0
                finally:
                    blocker.rollback()
                result = future.result(timeout=5)
        finally:
            blocker.close()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(client.get_state('light.reading')['state'], 'off')
        self.assertEqual(client.calls, 0)


if __name__ == '__main__':
    unittest.main()
