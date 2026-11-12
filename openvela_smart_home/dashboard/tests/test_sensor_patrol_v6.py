"""Private software-entity regressions for sensor authority and patrol lifecycle."""
from contextlib import ExitStack
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / 'openvela_smart_home/dashboard'
sys.path[:0] = [str(DASH), str(ROOT / 'SenseHome-6G/net_offload')]
from durable_receipts import SQLiteReceiptExecutor
from mqtt_receipts import SoftwareEntityStore


class SensorPatrolTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        work = ROOT / '_work/intrinsic_v6_fix'
        work.mkdir(parents=True, exist_ok=True)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=work)))
        spec = importlib.util.spec_from_file_location('v6_sensor_bridge', DASH / 'bridge_server.py')
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'SENSEHOME_STATE_FILE': str(self.folder / 'state.json'),
            'SENSEHOME_EVENT_LOG': str(self.folder / 'events.jsonl'),
            'SENSEHOME_REQUEST_DB': str(self.folder / 'requests.sqlite3'),
            'SENSEHOME_POLICY_CLI': str(self.folder / 'absent-cli'),
            'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
        }):
            spec.loader.exec_module(self.bridge)
        self.stack.callback(self.bridge.close_gateway)
        self.bridge.save_state(self.bridge.default_state())

    def entity(self, state):
        entity = SoftwareEntityStore(self.folder / 'entity.sqlite3', 'sensor-test-home', state)
        self.bridge.gateway_receipt_executor = SQLiteReceiptExecutor(
            self.folder / 'receipts.sqlite3', entity, platform_id='sensor-test-home')
        return entity

    def trusted(self, sensor, *fields, age=0):
        sensor['observations'] = {
            field: {'sampled_at': time.time() - age, 'source': 'software-test-fixture',
                    'trusted': True, 'status': 'ok'} for field in fields}
        return sensor

    def test_original_frozen_motion_cannot_undo_explicit_user_light_on(self):
        path = ROOT / 'docs/experiments/20261111_race_v3_portfix/initial_state.json'
        initial = json.loads(path.read_text(encoding='utf-8'))
        entity = self.entity(initial)
        first = entity.snapshot()
        self.assertTrue(self.bridge._gateway_local_handler('打开卧室灯', {'command_id': 'light-on'})['task_complete'])
        patrol = self.bridge.run_environment_patrol({'command_id': 'frozen-patrol'})
        after = entity.snapshot()
        self.assertEqual(after['software_state']['lights']['卧室']['brightness'], 100)
        self.assertEqual(after['software_state']['sensors'], first['software_state']['sensors'])
        self.assertEqual(patrol['auto_decisions'], 0)
        self.assertIn('motion', patrol['unavailable_sensors'])

    def test_fresh_trusted_software_sample_can_trigger_only_its_authorized_condition(self):
        state = self.bridge.load_state()
        state['sensors'] = {'卧室': self.trusted({'illuminance': 900, 'motion_detected': True},
                                                'illuminance', 'motion_detected')}
        entity = self.entity(state)
        report = self.bridge.run_environment_patrol({'command_id': 'trusted-test'})
        self.assertEqual(report['auto_decisions'], 1)
        self.assertEqual(entity.snapshot()['software_state']['lights']['卧室']['brightness'], 60)
        self.assertEqual(report['sensor_observations']['卧室']['illuminance']['source'], 'software-test-fixture')
        self.assertIn('客厅', report['unavailable_sensor_rooms'])

    def test_stale_and_untrusted_samples_never_authorize_control(self):
        for kind in ('stale', 'untrusted', 'future', 'missing-motion-time'):
            with self.subTest(kind=kind):
                state = self.bridge.default_state()
                sensor = self.trusted({'illuminance': 1000, 'motion_detected': True},
                                      'illuminance', 'motion_detected')
                if kind == 'stale':
                    for metadata in sensor['observations'].values(): metadata['sampled_at'] -= 300
                elif kind == 'untrusted':
                    for metadata in sensor['observations'].values(): metadata['trusted'] = False
                elif kind == 'future':
                    for metadata in sensor['observations'].values(): metadata['sampled_at'] += 600
                else:
                    sensor['observations']['motion_detected'].pop('sampled_at')
                state['sensors'] = {'卧室': sensor}
                self.bridge.gateway_receipt_executor = None
                self.bridge.save_state(state)
                report = self.bridge.run_environment_patrol()
                self.assertEqual(report['auto_decisions'], 0)
                self.assertEqual(self.bridge.load_state()['lights']['卧室']['brightness'], 0)

    def test_malformed_sensor_containers_and_values_are_unavailable_without_crashing(self):
        cases = [None, [], {'卧室': None}, {'卧室': []}, {'卧室': {'illuminance': float('nan'), 'motion_detected': False}},
                 {'卧室': {'illuminance': float('inf'), 'motion_detected': 1}},
                 {'卧室': {'illuminance': '800', 'motion_detected': True}}]
        for sensors in cases:
            with self.subTest(sensors=sensors):
                state = self.bridge.default_state()
                state['sensors'] = deepcopy(sensors)
                self.bridge.save_state(state)
                result = self.bridge.run_environment_patrol()
                self.assertEqual(result['auto_decisions'], 0)
                self.assertTrue(result['unavailable_sensors'])

    def test_malformed_mqtt_room_does_not_invalidate_other_device_observations_or_crash_patrol(self):
        state = self.bridge.load_state()
        state['sensors']['卧室'] = None
        self.entity(state)
        self.assertIsNotNone(self.bridge._sync_mqtt_state())
        report = self.bridge.run_environment_patrol({'command_id': 'invalid-room'})
        self.assertEqual(report['auto_decisions'], 0)
        self.assertIn('卧室', report['unavailable_sensor_rooms'])

    def test_metadata_wrong_types_and_extreme_numbers_are_unavailable(self):
        if not hasattr(self.bridge, 'sensor_observations'):
            self.fail('per-signal sensor validation is missing')
        for field, value in (('source', []), ('sampled_at', 10**1000), ('sampled_at', True),
                             ('status', {}), ('trusted', 'true')):
            with self.subTest(field=field, value_type=type(value).__name__):
                sensor = self.trusted({'illuminance': 500, 'motion_detected': True}, 'illuminance')
                sensor['observations']['illuminance'][field] = value
                result = self.bridge.sensor_observations({'卧室': sensor})
                self.assertFalse(result['卧室']['illuminance']['available'])
        sensor = self.trusted({'illuminance': 10**1000}, 'illuminance')
        self.assertFalse(self.bridge.sensor_observations({'卧室': sensor})['卧室']['illuminance']['available'])

    def test_corrupt_existing_state_is_preserved_and_actions_report_error(self):
        path = Path(self.bridge.STATE_FILE)
        for content in (b'{"lights":', b'[]', b'{}'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, 'STATE_FILE_INVALID'):
                    self.bridge.load_state()
                with self.assertRaisesRegex(ValueError, 'STATE_FILE_INVALID'):
                    self.bridge._gateway_local_handler('打开客厅灯', {'command_id': 'corrupt'})
                self.assertEqual(path.read_bytes(), content)
                with self.assertRaisesRegex(ValueError, 'STATE_FILE_INVALID'):
                    self.bridge.save_state(self.bridge.default_state())
                self.assertEqual(path.read_bytes(), content)

    def test_missing_first_run_state_can_be_created(self):
        Path(self.bridge.STATE_FILE).unlink()
        state = self.bridge.load_state()
        self.bridge.save_state(state)
        self.assertEqual(self.bridge.load_state(), state)

    def test_patrol_exception_clears_running_flag_and_restart_works(self):
        if not hasattr(self.bridge, 'start_patrol'):
            self.fail('single-instance patrol lifecycle is missing')
        with patch.object(self.bridge, 'run_environment_patrol', side_effect=OSError('private fixture')):
            self.assertTrue(self.bridge.start_patrol(60))
            thread = self.bridge._patrol_thread
            if thread is not None: thread.join(2)
        self.assertFalse(self.bridge.patrol_running)
        called = threading.Event()
        with patch.object(self.bridge, 'run_environment_patrol', side_effect=lambda: called.set()):
            self.assertTrue(self.bridge.start_patrol(60))
            self.assertTrue(called.wait(2))
            self.assertTrue(self.bridge.stop_patrol(2))
        self.assertFalse(self.bridge.patrol_running)

    def test_patrol_off_on_releases_old_wait_and_keeps_one_worker(self):
        if not hasattr(self.bridge, 'start_patrol'):
            self.fail('single-instance patrol lifecycle is missing')
        entered, release = threading.Event(), threading.Event()
        calls = []
        def once():
            calls.append(threading.current_thread())
            if len(calls) == 1:
                entered.set()
                release.wait(2)
        with patch.object(self.bridge, 'run_environment_patrol', side_effect=once):
            self.assertTrue(self.bridge.start_patrol(60))
            self.assertTrue(entered.wait(2))
            old = self.bridge._patrol_thread
            self.assertFalse(self.bridge.stop_patrol(.01))
            self.assertFalse(self.bridge.start_patrol(60))
            release.set()
            old.join(2)
            self.assertFalse(old.is_alive())
            self.assertTrue(self.bridge.start_patrol(60))
            new = self.bridge._patrol_thread
            self.assertTrue(self.bridge.stop_patrol(2))
            self.assertFalse(new.is_alive())
        self.assertEqual(calls.count(old), 1)
        self.assertLessEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
