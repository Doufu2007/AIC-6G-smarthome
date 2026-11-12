"""Local rules obey the same explicit target and action contract as models."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_server as bridge


class LocalExplicitContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_file = bridge.STATE_FILE
        bridge.STATE_FILE = str(Path(self.tmp.name) / 'isolated.json')
        bridge.save_state(bridge.default_state())

    def tearDown(self):
        bridge.STATE_FILE = self.original_file
        self.tmp.cleanup()

    def test_missing_target_does_not_default_to_living_room(self):
        for command in ('把灯调亮一点', '调暗灯光', '打开灯', '把窗帘打开', '把阳台灯打开'):
            with self.subTest(command=command):
                before = deepcopy(bridge.load_state())
                result = bridge._gateway_local_handler(command)
                self.assertEqual(bridge.load_state(), before)
                self.assertEqual(result['actions'], [])
                self.assertFalse(result['semantic_complete'])

    def test_curtain_suffix_open_executes_even_when_initially_closed(self):
        state = bridge.load_state()
        state['curtains']['客厅'] = {'is_open': False, 'position': 0}
        bridge.save_state(state)
        result = bridge._gateway_local_handler('把客厅窗帘打开')
        self.assertEqual(bridge.load_state()['curtains']['客厅']['position'], 100)
        self.assertTrue(result['simulated_execution_confirmed'])
        self.assertEqual(result['actions'][0]['name'], 'curtain_control')

    def test_curtain_percent_is_not_silently_ignored(self):
        result = bridge._gateway_local_handler('把书房窗帘位置设为30%')
        self.assertEqual(bridge.load_state()['curtains']['书房']['position'], 30)
        self.assertTrue(result['semantic_complete'])
        self.assertTrue(result['simulated_execution_confirmed'])

    def test_out_of_range_percentage_is_rejected_without_clamping(self):
        before = deepcopy(bridge.load_state())
        result = bridge._gateway_local_handler('把书房窗帘位置设为130%')
        self.assertEqual(bridge.load_state(), before)
        self.assertFalse(result['semantic_complete'])

    def test_invalid_light_numbers_do_not_fall_through_to_legacy_clamping(self):
        for command in ('把客厅灯亮度设为-1%', '把客厅灯亮度设为100.5%', '把客厅灯亮度设为130',
                        '客厅灯亮度-1%', '客厅灯亮度100.5%', '客厅灯亮度130%'):
            with self.subTest(command=command):
                before = deepcopy(bridge.load_state())
                result = bridge._gateway_local_handler(command)
                self.assertEqual(bridge.load_state(), before)
                self.assertFalse(result['semantic_complete'])

    def test_negated_conditional_or_quoted_text_never_becomes_a_keyword_action(self):
        for command in ('别打开客厅的灯', '只有检测到人时打开客厅的灯', '打开客厅灯的教程',
                        '不要执行离家模式', '客厅太冷了的例句'):
            with self.subTest(command=command):
                before = deepcopy(bridge.load_state())
                result = bridge._gateway_local_handler(command)
                self.assertEqual(bridge.load_state(), before)
                self.assertEqual(result['actions'], [])
                self.assertFalse(result['semantic_complete'])

    def test_simple_light_reports_structured_confirmation(self):
        result = bridge._gateway_local_handler('打开客厅的灯')
        self.assertTrue(result['simulated_execution_confirmed'])
        self.assertEqual(result['actions'][0]['name'], 'light_control')
        self.assertEqual(result['actions'][0]['params'], {'location': '客厅', 'action': 'on'})

    def test_read_reports_existing_simulated_sensor_without_mutating_it(self):
        before = deepcopy(bridge.load_state())
        result = bridge._gateway_local_handler('查看厨房的温度和湿度')
        self.assertEqual(bridge.load_state(), before)
        self.assertTrue(result['simulated_execution_confirmed'])
        self.assertEqual(result['actions'][0]['name'], 'read_temperature')


if __name__ == '__main__':
    unittest.main()
