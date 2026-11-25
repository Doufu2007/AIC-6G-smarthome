import copy
from pathlib import Path
import tempfile
import unittest

from cli import DEFAULT_CATALOG, DEFAULT_STATES
from controller import Controller, SoftwareClient
from inventory import DeviceInventory, demo_household
from native_ha_adapter import NativeHAAdapter


class NativeFixture:
    def __init__(self):
        self.states = {
            'switch.patio': dict(entity_id='switch.patio', state='off', last_updated='v0',
                                 attributes=dict(friendly_name='阳台插座', room='阳台')),
            'climate.living': dict(entity_id='climate.living', state='cool', last_updated='v0',
                                   attributes=dict(friendly_name='客厅空调', room='客厅', temperature=23,
                                                   min_temp=16, max_temp=30, target_temp_step=0.5,
                                                   supported_features=1)),
        }
        self.calls = []
        self.temperature_unit = '°C'
        self.mode_after_command = None

    def get_config(self, *, timeout=None):
        return {'unit_system': {'temperature': self.temperature_unit}}

    def get_states(self):
        return copy.deepcopy(list(self.states.values()))

    def get_state(self, entity_id):
        return copy.deepcopy(self.states[entity_id])

    def call_service(self, entity_id, service, data):
        self.calls.append((entity_id, service, data))
        state = self.states[entity_id]
        state['last_updated'] = 'v1'
        if service in ('switch.turn_on', 'switch.turn_off'):
            state['state'] = service.rsplit('_', 1)[1]
        else:
            state['attributes']['temperature'] = data['temperature']
            if self.mode_after_command is not None:
                state['state'] = self.mode_after_command
        return []


class SwitchClimateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.fixture = NativeFixture()
        self.adapter = NativeHAAdapter(self.fixture)

    def controller(self):
        return Controller(self.directory, self.adapter.discover_catalog(), self.adapter,
                          execution_options={'verify_timeout_seconds': 0})

    def confirm(self, app, card, request_id='test'):
        return app.confirm(dict(proposal_id=card['proposal_id'], digest=card['digest'],
                                request_id=request_id, confirmed=True))

    def test_switch_runs_through_propose_confirm_and_readback(self):
        app = self.controller()
        card = app.propose('打开阳台的插座')
        self.assertEqual(card['payload']['actions'],
                         [dict(entity_id='switch.patio', service='switch.turn_on', data={})])
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.confirm(app, card)['status'], 'succeeded')
        self.assertEqual(app.snapshot(['switch.patio'])['switch.patio']['state'], 'on')
        off = app.propose('关闭阳台的插座')
        self.assertEqual(self.confirm(app, off, 'off')['status'], 'succeeded')
        self.assertEqual(self.fixture.states['switch.patio']['state'], 'off')

    def test_absolute_climate_target_is_verified_via_setpoint(self):
        app = self.controller()
        card = app.propose('把客厅空调调到24.5摄氏度')
        self.assertEqual(card['payload']['actions'],
                         [dict(entity_id='climate.living', service='climate.set_temperature',
                               data={'temperature': 24.5})])
        result = self.confirm(app, card)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['steps'][0]['verification']['attributes']['temperature'], 24.5)
        self.assertEqual(self.fixture.calls, [('climate.living', 'climate.set_temperature', {'temperature': 24.5})])

    def test_climate_rejects_unsupported_range_or_operation_before_dispatch(self):
        app = self.controller()
        for text in ('把客厅空调调到32度', '把客厅空调调到24.3度', '关闭客厅空调', '客厅空调调低一度',
                     '把客厅空调调到75华氏度', '10秒后把客厅空调调到24度'):
            with self.subTest(text=text):
                self.assertEqual(app.propose(text)['status'], 'clarification')
        self.assertEqual(self.fixture.calls, [])

    def test_fahrenheit_or_off_does_not_advertise_celsius_control(self):
        self.fixture.temperature_unit = '°F'
        self.assertEqual(self.controller().propose('把客厅空调调到24度')['status'], 'clarification')
        self.fixture.temperature_unit = '°C'
        self.fixture.states['climate.living']['state'] = 'off'
        self.assertEqual(self.controller().propose('把客厅空调调到24度')['status'], 'clarification')
        self.assertEqual(self.fixture.calls, [])

    def test_mode_change_on_dispatch_cannot_verify_temperature_only(self):
        app = self.controller()
        card = app.propose('把客厅空调调到24度')
        self.fixture.mode_after_command = 'heat'
        self.assertEqual(self.confirm(app, card)['status'], 'unknown')
        self.assertEqual(len(self.fixture.calls), 1)

    def test_software_household_supports_both_controllable_devices(self):
        catalog, states, extras = demo_household(DEFAULT_CATALOG, DEFAULT_STATES)
        app = Controller(self.directory, catalog, SoftwareClient(states))
        inventory = DeviceInventory(app, extra_states=extras)
        for request, entity, service in (('打开阳台插座', 'switch.patio', 'switch.turn_on'),
                                         ('把客厅空调调到25度', 'climate.living', 'climate.set_temperature')):
            with self.subTest(entity=entity):
                card = app.propose(request)
                self.assertEqual(card['payload']['actions'][0]['service'], service)
                self.assertEqual(self.confirm(app, card, entity)['status'], 'succeeded')
                entry, = [row for row in inventory.list_devices() if row['entity_id'] == entity]
                self.assertEqual(entry['access'], 'control')
                self.assertIn(service, entry['supported_services'])

    def test_persistent_demo_adds_new_devices_without_resetting_existing_states(self):
        path = Path(self.directory) / 'software_states.json'
        original = SoftwareClient(DEFAULT_STATES, path)
        original.call_service('light.reading', 'light.turn_on', {})
        before = original.get_state('light.reading')
        _, states, _ = demo_household(DEFAULT_CATALOG, DEFAULT_STATES)
        upgraded = SoftwareClient(states, path, seed_missing=True)
        self.assertEqual(upgraded.get_state('light.reading'), before)
        self.assertEqual(upgraded.get_state('switch.patio')['state'], 'off')
        self.assertEqual(upgraded.get_state('climate.living')['attributes']['temperature'], 23)

    def test_native_climate_advertises_capability_after_turned_on(self):
        self.fixture.states['climate.living']['state'] = 'off'
        app = self.controller()
        inventory = DeviceInventory(app, ha_client=self.fixture)
        by_id = lambda: {entry['entity_id']: entry for entry in inventory.list_devices()}
        self.assertEqual(by_id()['climate.living']['access'], 'read_only')
        self.fixture.states['climate.living']['state'] = 'cool'
        self.assertEqual(by_id()['climate.living']['access'], 'control')
        self.assertEqual(app.propose('把客厅空调调到24度')['status'], 'awaiting_confirmation')
