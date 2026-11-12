"""Structured device intents accepted from a remote model.

No model text is interpreted as Python, shell, or a legacy command string.
Validate the entire batch before changing the simulated device state.
"""
from copy import deepcopy


def execute_device_actions(state, actions):
    if not isinstance(actions, list) or not 1 <= len(actions) <= 16:
        raise ValueError('INVALID_ACTION_BATCH')
    updated = deepcopy(state)
    observations = []
    descriptions = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {'name', 'params'}:
            raise ValueError('INVALID_ACTION_SCHEMA')
        name, params = action['name'], action['params']
        if name not in {'light_control', 'curtain_control', 'read_temperature'} or not isinstance(params, dict):
            raise ValueError('ACTION_NOT_ALLOWED')
        room = params.get('location')
        if not isinstance(room, str) or room not in updated.get('lights', {}):
            raise ValueError('INVALID_LOCATION')
        if name == 'read_temperature':
            if set(params) != {'location'} or room not in updated.get('temperature', {}):
                raise ValueError('INVALID_READ_PARAMETERS')
            value = updated['temperature'][room]
            observations.append(f"{room}温度={value['temp']}℃，湿度={value['humidity']}%")
        elif name == 'light_control':
            if set(params) - {'location', 'action', 'brightness'}:
                raise ValueError('INVALID_LIGHT_PARAMETERS')
            operation = params.get('action', 'set' if 'brightness' in params else None)
            if operation not in {'on', 'off', 'set'}:
                raise ValueError('INVALID_LIGHT_ACTION')
            brightness = params.get('brightness', 0 if operation == 'off' else 100)
            if type(brightness) is not int or not 0 <= brightness <= 100:
                raise ValueError('INVALID_BRIGHTNESS')
            if operation == 'off':
                brightness = 0
            updated['lights'][room] = {'is_on': brightness > 0, 'brightness': brightness}
            observations.append(f'{room}灯光亮度={brightness}%')
        else:
            if set(params) - {'location', 'action', 'position'} or room not in updated.get('curtains', {}):
                raise ValueError('INVALID_CURTAIN_PARAMETERS')
            operation = params.get('action', 'set' if 'position' in params else None)
            if operation not in {'open', 'close', 'set'}:
                raise ValueError('INVALID_CURTAIN_ACTION')
            position = params.get('position', 0 if operation == 'close' else 100)
            if type(position) is not int or not 0 <= position <= 100:
                raise ValueError('INVALID_POSITION')
            if operation == 'close':
                position = 0
            updated['curtains'][room] = {'is_open': position > 0, 'position': position}
            observations.append(f'{room}窗帘位置={position}%')
        descriptions.append({'name': name, 'params': dict(params), 'description': observations[-1]})
    state.clear()
    state.update(updated)
    return {'semantic_complete': True, 'actions': descriptions, 'observation': '\n'.join(observations)}
