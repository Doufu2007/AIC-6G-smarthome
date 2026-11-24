"""Native adapter tests with official REST-shaped responses, no real platform."""
import copy
import importlib
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'native_ha_adapter.py'
        self.assertTrue(source.exists(), 'native HA adapter is not implemented')
        self.module = importlib.import_module('native_ha_adapter')
        class Client:
            def __init__(inner):
                inner.calls = []
                inner.states = {
                    'light.desk': dict(entity_id='light.desk', state='off', last_updated='v0',
                        attributes=dict(friendly_name='台灯', room='书房', supported_color_modes=['brightness'])),
                    'cover.study': dict(entity_id='cover.study', state='closed', last_updated='v0',
                        attributes=dict(friendly_name='窗帘', room='书房', supported_features=3)),
                    'light.simple': dict(entity_id='light.simple', state='off', last_updated='v0',
                        attributes=dict(supported_color_modes=['onoff'])),
                }
            def get_state(inner, entity): return copy.deepcopy(inner.states[entity])
            def get_states(inner): return copy.deepcopy(list(inner.states.values()))
            def call_service(inner, entity, service, data):
                inner.calls.append((entity, service, data))
                return []
        self.client = Client()
        self.adapter = self.module.NativeHAAdapter(self.client)

    def test_light_brightness_and_cover_capabilities_are_discovered(self):
        self.assertTrue(self.adapter.get_state('light.desk')['attributes']['brightness_supported'])
        self.assertFalse(self.adapter.get_state('light.simple')['attributes']['brightness_supported'])
        self.assertEqual(self.adapter.get_state('cover.study')['attributes']['supported_services'],
                         ['cover.open_cover', 'cover.close_cover'])

    def test_supported_actions_forward_exact_entity_and_data(self):
        self.adapter.call_service('light.desk', 'light.turn_on', {'brightness_pct': 40})
        self.adapter.call_service('cover.study', 'cover.close_cover', {})
        self.assertEqual(self.client.calls, [('light.desk', 'light.turn_on', {'brightness_pct': 40}),
                                             ('cover.study', 'cover.close_cover', {})])

    def test_missing_cover_features_and_onoff_brightness_reject_without_post(self):
        self.client.states['cover.study']['attributes'].pop('supported_features')
        self.assertEqual(self.adapter.get_state('cover.study')['attributes']['supported_services'], [])
        for entity, service, data in [('cover.study', 'cover.open_cover', {}),
                                      ('light.simple', 'light.turn_on', {'brightness_pct': 50})]:
            with self.assertRaises(self.module.NotDispatched):
                self.adapter.call_service(entity, service, data)
        self.assertEqual(self.client.calls, [])

    def test_discovery_reads_catalog_without_service_calls(self):
        catalog = self.adapter.discover_catalog()
        self.assertEqual({e['entity_id'] for e in catalog}, set(self.client.states))
        desk = next(e for e in catalog if e['entity_id'] == 'light.desk')
        self.assertEqual((desk['name'], desk['room'], desk['kind']), ('台灯', '书房', 'light'))
        self.assertEqual(self.client.calls, [])

    def test_checked_send_has_no_hidden_state_get_that_can_outlive_consent(self):
        state = self.adapter.get_state('light.desk')
        def forbidden_read(entity): raise AssertionError('unexpected second network state read')
        self.client.get_state = forbidden_read
        self.adapter.call_service_checked('light.desk', 'light.turn_on', {}, state=state,
                                           authorization_expires_at=102, now=lambda: 100)
        self.assertEqual(self.client.calls, [('light.desk', 'light.turn_on', {})])

    def test_checked_send_rejects_expired_original_consent(self):
        state = self.adapter.get_state('light.desk')
        with self.assertRaises(self.module.NotDispatched):
            self.adapter.call_service_checked('light.desk', 'light.turn_on', {}, state=state,
                                               authorization_expires_at=102, now=lambda: 102)
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__': unittest.main(verbosity=2)
