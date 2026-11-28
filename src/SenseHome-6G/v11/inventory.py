"""Project household inventory onto the data allowed in the AgentArts tools."""

import copy
import threading

from capabilities import DEVICE_SERVICES


_READ_ONLY = {'binary_sensor', 'sensor', 'switch', 'climate', 'camera', 'event', 'lock'}
_STATE_ATTRIBUTES = {'unit_of_measurement', 'device_class', 'brightness', 'current_position',
                     'current_temperature', 'temperature', 'temperature_unit', 'min_temp', 'max_temp',
                     'target_temp_step'}


def demo_household(catalog, states):
    catalog, states = copy.deepcopy((catalog, states))
    known = {entry['entity_id'] for entry in catalog}
    climate_attributes = dict(temperature=23, current_temperature=24, min_temp=16, max_temp=30,
                              target_temp_step=0.5, temperature_unit='°C', supported_features=1)
    for entity, room, name, state, attributes in (
        ('light.patio', '阳台', '阳台灯', 'off', {}),
        ('light.bedroom', '卧室', '卧室灯', 'off', {}),
        ('light.kitchen', '厨房', '厨房灯', 'off', {}),
        ('light.bathroom', '卫生间', '卫生间灯', 'off', {}),
        ('light.entry', '门口', '门口灯', 'off', {}),
        ('switch.patio', '阳台', '阳台插座', 'off', {}),
        ('climate.living', '客厅', '客厅空调', 'cool', climate_attributes),
    ):
        if entity not in known:
            catalog.append(dict(entity_id=entity, kind=entity.partition('.')[0], room=room, name=name,
                                aliases=[], available=True))
            states[entity] = dict(state=state, attributes=attributes, last_updated='initial')
    extras = {
        'binary_sensor.front_door': dict(kind='binary_sensor', room='门口', name='入户门磁',
                                         aliases=['大门门磁'], state='off',
                                         attributes={'device_class': 'door'}),
        'camera.front_door': dict(kind='camera', room='门口', name='门口监控',
                                  aliases=['门口摄像头'], state='online', attributes={}),
        'sensor.living_temperature': dict(kind='sensor', room='客厅', name='客厅温度', aliases=[],
                                          state='24', attributes={'unit_of_measurement': '°C'}),
    }
    return catalog, states, {entity: dict(value, entity_id=entity, last_updated='initial')
                              for entity, value in extras.items()}


class DeviceInventory:
    def __init__(self, controller, *, ha_client=None, rooms=None, extra_states=None):
        self.controller = controller
        self.ha_client = ha_client
        self.rooms = copy.deepcopy(rooms or {})
        self.extra_states = copy.deepcopy(extra_states or {})
        self._lock = threading.RLock()

    def set_extra_state(self, entity_id, state, last_updated):
        with self._lock:
            if entity_id not in self.extra_states:
                raise KeyError(entity_id)
            self.extra_states[entity_id]['state'] = state
            self.extra_states[entity_id]['last_updated'] = last_updated
            return copy.deepcopy(self.extra_states[entity_id])

    def _room(self, entity, default):
        entry = self.rooms.get(entity, default)
        if isinstance(entry, str):
            return entry, []
        return entry.get('room', default), list(entry.get('room_aliases', []))

    def _entries(self):
        states = self.ha_client.get_states() if self.ha_client is not None else []
        state_index = {state['entity_id']: state for state in states}
        supported_services = getattr(self.controller.client, 'supported_services', None)
        live_services = supported_services(states) if self.ha_client is not None and callable(supported_services) else {}
        entries = {}
        for row in self.controller.catalog:
            entity = row['entity_id']
            room, room_aliases = self._room(entity, row['room'])
            services = row.get('supported_services', row.get('capabilities', {}).get('supported_services'))
            if services is None:
                services = DEVICE_SERVICES.get(row['kind'], ())
            if entity in live_services:
                services = [service for service in services if service in live_services[entity]]
            available = row.get('available') is True
            if self.ha_client is not None:
                available = available and state_index.get(entity, {}).get('state', 'unavailable') not in (
                    'unknown', 'unavailable')
            entries[entity] = dict(entity_id=entity, kind=row['kind'], room=room,
                                   room_aliases=room_aliases, name=row['name'], aliases=list(row.get('aliases', [])),
                                   available=available, access='control' if services else 'read_only',
                                   supported_services=list(services),
                                   source=self.controller.mode if self.ha_client is None else 'home-assistant')
        with self._lock:
            extras_snapshot = [copy.deepcopy(v) for v in self.extra_states.values()]
            extra_keys = set(self.extra_states)
        for value in [*states, *extras_snapshot]:
            entity = value.get('entity_id', '')
            kind = entity.partition('.')[0]
            if entity in entries or kind not in _READ_ONLY:
                continue
            attrs = value.get('attributes', {})
            room, room_aliases = self._room(entity, value.get('room', attrs.get('room', '未分组')))
            entries[entity] = dict(entity_id=entity, kind=kind, room=room, room_aliases=room_aliases,
                                   name=value.get('name', attrs.get('friendly_name', entity)),
                                   aliases=list(value.get('aliases', [])),
                                   available=value.get('state', 'unavailable') not in ('unknown', 'unavailable'),
                                   access='event_only' if kind in ('camera', 'event') else 'read_only',
                                   supported_services=[], source='software-demo' if entity in extra_keys
                                   else 'home-assistant')
        return entries

    def list_devices(self, room=None):
        if room is not None and (not isinstance(room, str) or not room.strip()):
            raise ValueError('room must be a nonempty string')
        return [entry for entry in self._entries().values() if room is None or
                room.strip().casefold() in {name.casefold() for name in [entry['room'], *entry['room_aliases']]}]

    def get_device_state(self, entity_id):
        entry = self._entries()[entity_id]
        if any(row['entity_id'] == entity_id for row in self.controller.catalog):
            value = self.controller.snapshot([entity_id])[entity_id]
        else:
            with self._lock:
                extra_val = copy.deepcopy(self.extra_states.get(entity_id))
            value = extra_val if extra_val is not None else self.ha_client.get_state(entity_id)
        state = value.get('state', 'unavailable')
        attributes = {}
        if entry['kind'] == 'camera':
            state = state if state in ('unknown', 'unavailable') else 'online'
        else:
            attributes = {key: item for key, item in value.get('attributes', {}).items()
                          if key in _STATE_ATTRIBUTES and type(item) in (str, int, float, bool)}
        result = dict(entity_id=entity_id, state=state, attributes=attributes,
                      source=entry['source'], last_updated=value.get('last_updated'))
        if value.get('read_error_type'):
            result['read_error_type'] = value['read_error_type']
        return result
