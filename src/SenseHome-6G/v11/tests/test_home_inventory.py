import tempfile
import unittest

from cli import DEFAULT_CATALOG, DEFAULT_STATES
from controller import Controller, SoftwareClient
from inventory import DeviceInventory, demo_household


class InventoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        catalog, states, extras = demo_household(DEFAULT_CATALOG, DEFAULT_STATES)
        self.controller = Controller(directory.name, catalog, SoftwareClient(states))
        self.inventory = DeviceInventory(self.controller, extra_states=extras)

    def test_balcony_light_is_in_the_real_proposal_catalog(self):
        patio = {entry['entity_id']: entry for entry in self.inventory.list_devices(room='阳台')}
        self.assertEqual(set(patio), {'light.patio', 'switch.patio'})
        self.assertEqual(patio['light.patio']['access'], 'control')
        self.assertEqual(patio['light.patio']['source'], 'software-demo')
        card = self.controller.propose('打开阳台的灯')
        self.assertEqual(card['payload']['actions'][0]['entity_id'], 'light.patio')

    def test_front_door_devices_are_discoverable_without_actions_or_mediastream(self):
        door = {entry['kind']: entry for entry in self.inventory.list_devices(room='门口')}
        self.assertEqual(door['binary_sensor']['access'], 'read_only')
        self.assertEqual(door['camera']['access'], 'event_only')
        self.assertEqual(door['camera']['supported_services'], [])
        state = self.inventory.get_device_state(door['camera']['entity_id'])
        self.assertEqual(state['state'], 'online')
        self.assertNotIn('stream_url', state['attributes'])
        with self.assertRaises(KeyError):
            self.inventory.get_device_state('camera.unlisted')

    def test_ha_inventory_uses_explicit_area_mapping_and_filters_camera_attributes(self):
        class HA:
            camera = {'entity_id': 'camera.door', 'state': 'streaming', 'last_updated': 't0',
                      'attributes': {'friendly_name': '门口监控', 'access_token': 'hidden'}}

            def get_states(self):
                return [self.camera]

            def get_state(self, entity):
                if entity != self.camera['entity_id']:
                    raise AssertionError(entity)
                return self.camera

        inventory = DeviceInventory(self.controller, ha_client=HA(),
                                    rooms={'camera.door': {'room': '门口', 'room_aliases': ['玄关']}})
        self.assertEqual(inventory.list_devices(room='玄关')[0]['source'], 'home-assistant')
        self.assertEqual(inventory.get_device_state('camera.door')['attributes'], {})
