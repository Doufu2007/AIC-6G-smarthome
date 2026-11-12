"""Reject plans that silently invent a device or execute only a supported part."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends.openai_backend import BackendConfig, BackendError, InferenceService


class ModelCommandContractTests(unittest.TestCase):
    def service(self):
        return InferenceService(BackendConfig('http://127.0.0.1:1/v1', 'test-fixture'))

    def infer(self, command, actions):
        service = self.service()
        upstream = {'model': 'test-fixture', 'choices': [{'message': {'content':
                    json.dumps({'answer': '', 'actions': actions}, ensure_ascii=False)}}]}
        with mock.patch.object(service, '_upstream', return_value=(upstream, 1.0)):
            return service.infer({'command': command})

    def test_generic_device_does_not_authorize_invented_light_and_curtain(self):
        actions = [{'name': 'light_control', 'params': {'location': '卧室', 'brightness': 60}},
                   {'name': 'curtain_control', 'params': {'location': '卧室', 'action': 'open'}}]
        with self.assertRaisesRegex(BackendError, 'MODEL_COMMAND_TARGET_UNSPECIFIED'):
            self.infer('把卧室的设备调整一下', actions)

    def test_unsupported_device_rejects_entire_partial_plan(self):
        with self.assertRaisesRegex(BackendError, 'MODEL_COMMAND_UNSUPPORTED_DEVICE'):
            self.infer('打开厨房插座并把浴室窗帘位置设为33%',
                       [{'name': 'curtain_control', 'params': {'location': '浴室', 'position': 33}}])

    def test_non_execution_text_does_not_authorize_mutation(self):
        with self.assertRaisesRegex(BackendError, 'MODEL_COMMAND_NON_EXECUTION_INTENT'):
            self.infer('打开客厅灯的教程',
                       [{'name': 'light_control', 'params': {'location': '客厅', 'action': 'on'}}])

    def test_invalid_requested_numbers_are_not_silently_corrected(self):
        for value in ('-1%', '100.5%', '130%'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(BackendError, 'MODEL_COMMAND_INVALID_VALUE'):
                    self.infer('客厅灯亮度' + value,
                               [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}])

    def test_empty_refusal_remains_valid_without_fake_execution(self):
        result = self.infer('打开厨房插座并把浴室窗帘位置设为33%', [])
        self.assertEqual(result['actions'], [])
        self.assertFalse(result['semantic_complete'])

    def test_normalization_does_not_invent_an_action_from_quoted_or_negated_text(self):
        for command in ('客厅有点暗的故事', '不要读取客厅的温度'):
            with self.subTest(command=command):
                result = self.infer(command, [])
                self.assertEqual(result['actions'], [])
                self.assertNotIn('action_normalization', result)


if __name__ == '__main__':
    unittest.main()
