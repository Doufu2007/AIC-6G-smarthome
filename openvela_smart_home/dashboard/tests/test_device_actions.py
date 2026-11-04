import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from device_actions import execute_device_actions


class DeviceActionTests(unittest.TestCase):
    def state(self):
        return {
            'lights': {'客厅': {'is_on': False, 'brightness': 0}},
            'curtains': {'客厅': {'is_open': False, 'position': 0}},
            'temperature': {'客厅': {'temp': 21, 'humidity': 55}},
        }

    def test_remote_structured_action_changes_state_and_reports_observation(self):
        state = self.state()
        result = execute_device_actions(state, [
            {'name': 'light_control', 'params': {'location': '客厅', 'action': 'on', 'brightness': 80}},
            {'name': 'read_temperature', 'params': {'location': '客厅'}},
        ])
        self.assertEqual(state['lights']['客厅'], {'is_on': True, 'brightness': 80})
        self.assertTrue(result['semantic_complete'])
        self.assertIn('21', result['observation'])

    def test_unknown_or_malformed_actions_are_atomic_and_do_not_execute(self):
        for bad in [
            {'name': 'unlock_door', 'params': {'location': '客厅'}},
            {'name': 'light_control', 'params': {'location': '客厅', 'brightness': 101}},
            {'name': 'light_control', 'params': {'location': '客厅', 'action': 'on', 'command': 'shell'}},
            {'description': 'light_control(location="客厅",action="on")'},
            {'name': 'light_control', 'params': {'location': '客厅', 'brightness': True}},
        ]:
            with self.subTest(bad=bad):
                state = self.state()
                with self.assertRaises(ValueError):
                    execute_device_actions(state, [
                        {'name': 'light_control', 'params': {'location': '客厅', 'action': 'on'}}, bad,
                    ])
                self.assertFalse(state['lights']['客厅']['is_on'])


if __name__ == '__main__':
    unittest.main()
