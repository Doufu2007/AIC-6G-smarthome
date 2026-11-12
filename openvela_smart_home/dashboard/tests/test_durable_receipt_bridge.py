from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_server as bridge


class ReceiptBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = patch.object(bridge, "STATE_FILE", str(Path(self.tmp.name) / "state.json"))
        self.state_file.start()
        bridge.save_state(bridge.default_state())

    def tearDown(self):
        self.state_file.stop()
        self.tmp.cleanup()

    def test_original_allowlist_rejects_invalid_batch_before_receipt_transport(self):
        if not hasattr(bridge, "gateway_receipt_executor"):
            self.fail("bridge has no durable execution adapter")
        calls = []
        with patch.object(bridge, "gateway_receipt_executor", lambda *a: calls.append(a)):
            with self.assertRaises(ValueError):
                bridge._gateway_action_executor([{"name": "light_control", "params": {"location": "客厅", "brightness": 999}}], {"_receipt_command": "有点暗"})
        self.assertEqual(calls, [])
        self.assertEqual(bridge.load_state()["lights"]["客厅"]["brightness"], 0)

    def test_unknown_receipt_does_not_claim_simulated_completion_or_mutate_state(self):
        if not hasattr(bridge, "gateway_receipt_executor"):
            self.fail("bridge has no durable execution adapter")
        captured = []
        def uncertain(actions, context):
            captured.append(deepcopy(context))
            return {"execution_state": "unknown", "semantic_complete": False, "actions": [],
                    "observation": "", "answer": "未知，不重发"}
        with patch.object(bridge, "gateway_receipt_executor", uncertain):
            result = bridge._gateway_local_handler("打开客厅灯", {"command_id": "bridge-1", "_receipt_command": "伪造"})
        self.assertFalse(result["semantic_complete"])
        self.assertFalse(result.get("simulated_execution_confirmed", False))
        self.assertEqual(captured[0]["_receipt_command"], "打开客厅灯")
        self.assertEqual(bridge.load_state()["lights"]["客厅"]["brightness"], 0)

    def test_historical_receipt_never_replays_old_action_into_current_mirror(self):
        current = bridge.load_state()
        current['lights']['客厅'] = {'is_on': False, 'brightness': 0}
        calls = []
        class Adapter:
            def __call__(self, *args):
                calls.append(args)
                return {'execution_state': 'applied', 'semantic_complete': True,
                        'interface_ok': True, 'receipt': {'state_version_after': 1},
                        'actions': [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 100}, 'description': '旧提交'}]}
            def snapshot(self):
                return {'schema': 'sensehome.software-state.v1', 'platform_id': 'software-home',
                        'evidence_kind': 'mqtt-software-entity', 'state_version': 2, 'software_state': current}
            platform_id = 'software-home'
        with patch.object(bridge, 'gateway_receipt_executor', Adapter()):
            result = bridge._gateway_action_executor([{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 100}}], {'_receipt_command': '打开客厅灯'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(result['execution_complete'])
        self.assertFalse(result['task_complete'])
        self.assertEqual(bridge.load_state()['lights']['客厅']['brightness'], 0)

    def test_receipt_mode_cannot_bypass_allowlist_via_legacy_leave_home_or_patrol(self):
        before = bridge.load_state()
        with patch.object(bridge, 'gateway_receipt_executor', object()):
            result = bridge._gateway_local_handler('离家模式', {})
            bridge.run_environment_patrol()
        self.assertFalse(result['semantic_complete'])
        self.assertEqual(bridge.load_state(), before)


if __name__ == "__main__":
    unittest.main()
