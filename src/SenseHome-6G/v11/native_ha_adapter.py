"""Native Home Assistant REST adapter for the SenseHome control inventory.

Discovery reads HA states only. State verification is platform state evidence,
not proof that a physical actuator moved. Missing optional capabilities are
treated conservatively instead of inventing features.
"""
import copy
import re
import time

from capabilities import DEVICE_SERVICES, temperature_control_available, temperature_target_supported
from platform_execution import HAError, NotDispatched


def _kind_of(entity):
    if isinstance(entity, str) and re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', entity):
        kind = entity.partition('.')[0]
        if kind in DEVICE_SERVICES:
            return kind
    return None


class NativeHAAdapter:
    def __init__(self, client, *, rooms=None):
        self.client = client
        self.rooms = dict(rooms or {})

    @staticmethod
    def _kind(entity_id):
        kind = _kind_of(entity_id)
        if kind is None:
            raise NotDispatched('unsupported native entity')
        return kind

    def _temperature_unit(self, timeout=None):
        getter = getattr(self.client, 'get_config', None)
        if not callable(getter) or (timeout is not None and timeout <= 0):
            return None
        try:
            config = getter(timeout=timeout)
        except (HAError, OSError):
            return None
        units = config.get('unit_system') if isinstance(config, dict) else None
        return units.get('temperature') if isinstance(units, dict) else None

    def _normalize(self, value, *, temperature_unit=None):
        if not isinstance(value, dict) or not isinstance(value.get('entity_id'), str):
            raise HAError('invalid_native_state')
        kind = self._kind(value['entity_id'])
        if not isinstance(value.get('state'), str) or not isinstance(value.get('attributes', {}), dict):
            raise HAError('invalid_native_state')
        state = copy.deepcopy(value)
        attrs = state.setdefault('attributes', {})
        services = []
        brightness = False
        if kind in ('light', 'switch'):
            services = list(DEVICE_SERVICES[kind])
        if kind == 'light':
            modes = attrs.get('supported_color_modes', [])
            brightness = isinstance(modes, list) and any(
                m in {'brightness', 'color_temp', 'hs', 'xy', 'rgb', 'rgbw', 'rgbww', 'white'} for m in modes)
        elif kind == 'cover':
            features = attrs.get('supported_features')
            if type(features) is int and features >= 0:
                if features & 1: services.append('cover.open_cover')
                if features & 2: services.append('cover.close_cover')
        elif kind == 'climate':
            attrs['temperature_unit'] = temperature_unit
            if temperature_control_available(state):
                services = list(DEVICE_SERVICES[kind])
        if state['state'] in ('unavailable', 'unknown'):
            services = []
        attrs['supported_services'] = services
        attrs['brightness_supported'] = brightness
        attrs['platform_adapter'] = 'native-ha-rest'
        return state

    def supported_services(self, states):
        """Evaluate live capabilities for an already-read group of native states."""
        unit = self._temperature_unit() if any(_kind_of(state.get('entity_id')) == 'climate' for state in states) else None
        return {state['entity_id']: self._normalize(state, temperature_unit=unit)['attributes']['supported_services']
                for state in states if _kind_of(state.get('entity_id')) is not None}

    def get_state(self, entity_id):
        kind = self._kind(entity_id)
        state = self.client.get_state(entity_id)
        if state.get('entity_id') != entity_id:
            raise HAError('native_entity_mismatch')
        unit = self._temperature_unit() if kind == 'climate' else None
        return self._normalize(state, temperature_unit=unit)

    def get_state_with_timeout(self, entity_id, timeout):
        kind = self._kind(entity_id)
        deadline = time.monotonic() + timeout
        bounded_read = getattr(self.client, 'get_state_with_timeout', None)
        state = (bounded_read(entity_id, timeout) if callable(bounded_read)
                 else self.client.get_state(entity_id))
        if state.get('entity_id') != entity_id:
            raise HAError('native_entity_mismatch')
        unit = self._temperature_unit(deadline - time.monotonic()) if kind == 'climate' else None
        return self._normalize(state, temperature_unit=unit)

    def validate_action(self, entity_id, service, data, state=None):
        if state is None:
            state = self.get_state(entity_id)
        else:
            unit = state.get('attributes', {}).get('temperature_unit')
            state = self._normalize(state, temperature_unit=unit)
        if state.get('entity_id') != entity_id or service not in state['attributes']['supported_services']:
            raise NotDispatched('unsupported_native_service')
        if not isinstance(data, dict):
            raise NotDispatched('invalid_native_service_data')
        if service == 'light.turn_on':
            if set(data) - {'brightness_pct'}:
                raise NotDispatched('invalid_native_light_data')
            if 'brightness_pct' in data:
                value = data['brightness_pct']
                if type(value) is not int or not 0 <= value <= 100:
                    raise NotDispatched('invalid_native_brightness')
                if not state['attributes']['brightness_supported']:
                    raise NotDispatched('native_brightness_unsupported')
        elif service == 'climate.set_temperature':
            if not temperature_target_supported(data, state):
                raise NotDispatched('invalid_native_target_temperature')
        elif data:
            raise NotDispatched('native_service_requires_empty_data')

    def call_service(self, entity_id, service, data):
        self.validate_action(entity_id, service, data)
        return self.client.call_service(entity_id, service, copy.deepcopy(data))

    def call_service_checked(self, entity_id, service, data, *, state, authorization_expires_at, now):
        """ExecutionStore already GET-checked state; do not add another GET.

        Re-reading inside the transport could outlive the consent deadline.
        This method validates the owned snapshot and checks time at send.
        """
        self.validate_action(entity_id, service, data, state=state)
        if authorization_expires_at is not None and now() >= authorization_expires_at:
            raise NotDispatched('authorization_expired_before_send')
        return self.client.call_service(entity_id, service, copy.deepcopy(data))

    def discover_catalog(self):
        states = self.client.get_states()
        if not isinstance(states, list):
            raise HAError('invalid_native_state_list')
        catalog, seen = [], set()
        for value in states:
            if not isinstance(value, dict):
                raise HAError('invalid_native_state_list')
            entity = value.get('entity_id')
            kind = _kind_of(entity)
            if kind is None:
                continue
            if entity in seen:
                raise HAError('duplicate_native_entity')
            seen.add(entity)
            state = self._normalize(value)
            attrs = state['attributes']
            room = self.rooms.get(entity, attrs.get('room', '未分组'))
            if not isinstance(room, str) or not room.strip(): room = '未分组'
            name = attrs.get('friendly_name', entity)
            if not isinstance(name, str) or not name.strip(): name = entity
            services = list(DEVICE_SERVICES[kind]) if kind == 'climate' else attrs['supported_services']
            catalog.append(dict(entity_id=entity, kind=kind, room=room,
                name=name, aliases=[], available=state['state'] not in ('unavailable', 'unknown'),
                capabilities=dict(supported_services=services,
                                  brightness_supported=attrs['brightness_supported'])))
        return catalog
