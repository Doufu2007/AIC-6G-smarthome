"""Runtime capability checks shared by proposal and platform boundaries.

Missing declarations preserve legacy SDK compatibility. Native and software
adapters provide explicit declarations; false and empty are restrictive.
"""

import math


DEVICE_SERVICES = {
    'light': ('light.turn_on', 'light.turn_off'),
    'cover': ('cover.open_cover', 'cover.close_cover'),
    'switch': ('switch.turn_on', 'switch.turn_off'),
    'climate': ('climate.set_temperature',),
}


def _finite(value):
    return type(value) in (int, float) and -math.inf < value < math.inf


def temperature_data_valid(data):
    return isinstance(data, dict) and set(data) == {'temperature'} and _finite(data['temperature'])


def temperature_control_available(state):
    attrs = state.get('attributes', {})
    features = attrs.get('supported_features')
    low, high = attrs.get('min_temp'), attrs.get('max_temp')
    return (state.get('state') in ('cool', 'heat') and attrs.get('temperature_unit') == '°C' and
            type(features) is int and features >= 0 and bool(features & 1) and
            all(_finite(x) for x in (attrs.get('temperature'), low, high)) and low <= high)


def temperature_target_supported(data, state):
    if not temperature_data_valid(data) or not temperature_control_available(state):
        return False
    attrs = state['attributes']
    target = data['temperature']
    low, high = attrs['min_temp'], attrs['max_temp']
    if not low <= target <= high:
        return False
    step = attrs.get('target_temp_step')
    if step is None:
        return True
    if not _finite(step) or step <= 0:
        return False
    steps = (target - low) / step
    return math.isfinite(steps) and abs(steps - round(steps)) <= 1e-7


def validate_actions(actions, catalog, states):
    """Raise ValueError for a missing, unavailable, or unsupported target."""
    if not isinstance(states, dict):
        raise ValueError('runtime states required')
    index = {entry['entity_id']: entry for entry in catalog}
    for action in actions:
        entity = action['entity_id']
        entry = index.get(entity)
        if entry is None or entry.get('available') is not True:
            raise ValueError('entity unavailable')
        state = states.get(entity)
        if not isinstance(state, dict) or not isinstance(state.get('state'), str):
            raise ValueError('entity state unavailable')
        if not state['state'] or state['state'].lower() in ('unknown', 'unavailable') or state.get('read_error_type'):
            raise ValueError('entity state unavailable')
        if 'entity_id' in state and state['entity_id'] != entity:
            raise ValueError('entity state identity mismatch')
        attrs = state.get('attributes', {})
        if not isinstance(attrs, dict):
            raise ValueError('invalid entity attributes')
        for declaration in (entry, attrs):
            services = declaration.get('supported_services')
            if services is not None:
                if not isinstance(services, (list, tuple)) or not all(isinstance(x, str) for x in services):
                    raise ValueError('invalid supported services')
                if action['service'] not in services:
                    raise ValueError('unsupported entity service')
        if 'brightness_pct' in action['data']:
            if any(declaration.get('brightness_supported') is False for declaration in (entry, attrs)):
                raise ValueError('entity does not support brightness')
            modes = attrs.get('supported_color_modes')
            if modes is not None:
                if not isinstance(modes, (list, tuple)) or not all(isinstance(x, str) for x in modes):
                    raise ValueError('invalid supported color modes')
                if not set(modes) - {'onoff', 'unknown'}:
                    raise ValueError('entity does not support brightness')
        if action['service'] == 'climate.set_temperature' and not temperature_target_supported(action['data'], state):
            raise ValueError('unsupported target temperature or climate mode')
