"""Explicit helper mapping for virtual HA integration tests.

This adapter exposes logical light IDs while identifying the official helper in
every returned state. It has no brightness, cover, or physical-device support.
"""
import copy
import re
from platform_execution import NotDispatched


class VirtualHAAdapter:
    def __init__(self, client, mapping):
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError('explicit nonempty helper mapping required')
        for logical, physical in mapping.items():
            if not isinstance(logical, str) or not re.fullmatch(r'light\.[a-z0-9_]+', logical):
                raise ValueError('only logical light IDs supported')
            if not isinstance(physical, str) or not re.fullmatch(r'input_boolean\.[a-z0-9_]+', physical):
                raise ValueError('only official input_boolean helpers supported')
        if len(set(mapping.values())) != len(mapping):
            raise ValueError('helper targets must be distinct')
        self.client = client
        self._mapping = dict(mapping)

    def _physical(self, entity_id):
        if entity_id not in self._mapping:
            raise ValueError('unmapped logical entity')
        return self._mapping[entity_id]

    def get_state(self, entity_id):
        physical = self._physical(entity_id)
        state = copy.deepcopy(self.client.get_state(physical))
        state['entity_id'] = entity_id
        state['attributes'] = {**state.get('attributes', {}),
                               'physical_entity_id': physical,
                               'virtual_mapping': 'input_boolean helper representing a light',
                               'physical_device': False,
                               'supported_services': ['light.turn_on', 'light.turn_off'],
                               'brightness_supported': False}
        return state

    def get_state_with_timeout(self, entity_id, timeout):
        physical = self._physical(entity_id)
        bounded_read = getattr(self.client, 'get_state_with_timeout', None)
        raw = (bounded_read(physical, timeout) if callable(bounded_read)
               else self.client.get_state(physical))
        state = copy.deepcopy(raw)
        state['entity_id'] = entity_id
        state['attributes'] = {**state.get('attributes', {}),
                               'physical_entity_id': physical,
                               'virtual_mapping': 'input_boolean helper representing a light',
                               'physical_device': False,
                               'supported_services': ['light.turn_on', 'light.turn_off'],
                               'brightness_supported': False}
        return state

    def call_service(self, entity_id, service, data):
        self.validate_action(entity_id, service, data)
        physical = self._physical(entity_id)
        helper_service = 'input_boolean.' + service.split('.')[1]
        return self.client.call_service(physical, helper_service, {})

    def validate_action(self, entity_id, service, data, state=None):
        if entity_id not in self._mapping:
            raise NotDispatched('unmapped logical entity')
        if service not in ('light.turn_on', 'light.turn_off') or not isinstance(data, dict) or data:
            raise NotDispatched('helper supports only light.turn_on/off with empty data')
