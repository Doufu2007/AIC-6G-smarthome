from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offload_gateway import BackendResult, OffloadGateway, RuleBackend

ACTION = [{"name": "light_control", "params": {"location": "客厅", "brightness": 60}}]


class ReceiptGatewayTests(unittest.TestCase):
    def test_local_unknown_receipt_retains_execution_error(self):
        backend = RuleBackend(handler=lambda *args: {'semantic_complete': False, 'interface_ok': False,
                                                     'actions': [], 'error': 'EXECUTION_UNKNOWN'})
        result = backend.infer('打开客厅灯')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'EXECUTION_UNKNOWN')

    def test_unknown_receipt_is_terminal_and_uses_original_command_binding(self):
        class Policy:
            def decide(self, *args):
                return "cloud-model", "WAN_OK", "test"

        class Planner:
            def infer(self, *args):
                return BackendResult(True, True, {}, ACTION, source="cloud-model")

        class Fallback:
            def infer(self, *args):
                raise AssertionError("unknown execution must not trigger fallback")

        contexts = []
        def executor(actions, context):
            contexts.append(context)
            return {"execution_state": "unknown", "semantic_complete": False, "interface_ok": False,
                    "actions": [], "error": "EXECUTION_UNKNOWN"}

        try:
            gateway = OffloadGateway(Policy(), {"cloud-model": Planner(), "local-rules": Fallback()}, receipt_executor=executor)
        except TypeError:
            self.fail("formal gateway is missing optional durable receipt executor")
        command = '把客厅灯光设为60%'
        gateway.classify = lambda command: 'complex'  # Exercise the receipt boundary independent of routing.
        event = gateway.handle(command, {"command_id": "g-1", "_receipt_command": "伪造绑定"})
        self.assertEqual(contexts[0]["_receipt_command"], command)
        self.assertFalse(event["interface_ok"])
        self.assertFalse(event["semantic_complete"])
        self.assertFalse(event["fallback_attempted"])
        self.assertEqual(event["error"], "EXECUTION_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
