"""Runtime capability checks shared by proposal and platform boundaries.

Missing declarations preserve legacy SDK compatibility. Native and software
adapters provide explicit declarations; false and empty are restrictive.
"""


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
