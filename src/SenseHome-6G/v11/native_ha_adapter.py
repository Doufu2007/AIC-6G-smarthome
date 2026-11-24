"""Native Home Assistant light/cover REST adapter.

Discovery reads HA states only. State verification is platform state evidence,
not proof that a physical actuator moved. Missing optional capabilities are
treated conservatively instead of inventing features.
"""
import copy
import re

from platform_execution import HAError, NotDispatched


class NativeHAAdapter:
    def __init__(self, client, *, rooms=None):
        self.client = client
        self.rooms = dict(rooms or {})

    @staticmethod
    def _kind(entity_id):
        if not isinstance(entity_id, str) or not re.fullmatch(r'(light|cover)\.[a-z0-9_]+', entity_id):
            raise NotDispatched('only native light/cover entities are supported')
        return entity_id.split('.')[0]

    def _normalize(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('entity_id'), str):
            raise HAError('invalid_native_state')
        kind = self._kind(value['entity_id'])
        if not isinstance(value.get('state'), str) or not isinstance(value.get('attributes', {}), dict):
            raise HAError('invalid_native_state')
        state = copy.deepcopy(value)
        attrs = state.setdefault('attributes', {})
        services = []
        brightness = False
        if kind == 'light':
            services = ['light.turn_on', 'light.turn_off']
            modes = attrs.get('supported_color_modes', [])
            brightness = isinstance(modes, list) and any(
                m in {'brightness', 'color_temp', 'hs', 'xy', 'rgb', 'rgbw', 'rgbww', 'white'} for m in modes)
        else:
            features = attrs.get('supported_features')
            if type(features) is int and features >= 0:
                if features & 1: services.append('cover.open_cover')
                if features & 2: services.append('cover.close_cover')
        if state['state'] in ('unavailable', 'unknown'):
            services = []
        attrs['supported_services'] = services
        attrs['brightness_supported'] = brightness
        attrs['platform_adapter'] = 'native-ha-rest'
        return state

    def get_state(self, entity_id):
        self._kind(entity_id)
        state = self.client.get_state(entity_id)
        if state.get('entity_id') != entity_id:
            raise HAError('native_entity_mismatch')
        return self._normalize(state)

    def get_state_with_timeout(self, entity_id, timeout):
        self._kind(entity_id)
        bounded_read = getattr(self.client, 'get_state_with_timeout', None)
        state = (bounded_read(entity_id, timeout) if callable(bounded_read)
                 else self.client.get_state(entity_id))
        if state.get('entity_id') != entity_id:
            raise HAError('native_entity_mismatch')
        return self._normalize(state)

    def validate_action(self, entity_id, service, data, state=None):
        state = self.get_state(entity_id) if state is None else self._normalize(state)
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
            if not isinstance(entity, str) or not re.fullmatch(r'(light|cover)\.[a-z0-9_]+', entity):
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
            catalog.append(dict(entity_id=entity, kind=entity.split('.')[0], room=room,
                name=name, aliases=[], available=state['state'] not in ('unavailable', 'unknown'),
                capabilities=dict(supported_services=attrs['supported_services'],
                                  brightness_supported=attrs['brightness_supported'])))
        return catalog
