import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import BackendResult, LinkSnapshot, OffloadGateway  # noqa: E402


class Policy:
    def decide(self, task_class, link):
        return "cloud-model", "WAN_OK", "test"


class Backend:
    def __init__(self, actions, source="cloud-model", semantic=False):
        self.actions = actions
        self.source = source
        self.semantic = semantic

    def infer(self, command, context=None):
        return BackendResult(True, self.semantic, {"answer": "plan", "keep": "field"}, self.actions, source=self.source)


class GatewayActionExecutionTests(unittest.TestCase):
    def gateway(self, executor, actions):
        return OffloadGateway(
            policy=Policy(),
            backends={"cloud-model": Backend(actions), "local-rules": Backend([], "local-rules")},
            link_snapshot=LinkSnapshot(wan_ok=True, lan_ok=True),
            action_executor=executor,
        )

    def test_remote_actions_reach_executor_and_merge_execution_response(self):
        actions = [{"name": "set_light", "params": {"room": "客厅", "brightness": 60}}]
        applied = [{"name": "set_light", "applied": True}]
        executor = mock.Mock(return_value={"answer": "applied", "actions": applied, "semantic_complete": True})
        gateway = self.gateway(executor, actions)

        event = gateway.handle("有点暗", {"session": "test"})

        executor.assert_called_once_with(actions, {"session": "test"})
        self.assertEqual(event["backend"], "cloud-model")
        self.assertEqual(event["actions"], applied)
        self.assertTrue(event["semantic_complete"])
        self.assertEqual(event["response"]["answer"], "applied")
        self.assertEqual(event["response"]["keep"], "field")

    def test_unsafe_actions_never_reach_executor(self):
        executor = mock.Mock()
        gateway = self.gateway(executor, [{"name": "unlock_door"}])

        event = gateway.handle("有点暗")

        executor.assert_not_called()
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["error"], "UNSAFE_ACTION_REJECTED")

    def test_executor_exception_falls_back_and_records_error(self):
        executor = mock.Mock(side_effect=ValueError("invalid device action"))
        gateway = self.gateway(executor, [{"name": "set_light"}])

        event = gateway.handle("有点暗")

        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["fallback_from"], "cloud-model")
        self.assertEqual(event["error"], "ACTION_EXECUTION_ValueError")
        self.assertEqual(event["actions"], [])

    def test_local_rules_actions_are_not_executed_twice(self):
        executor = mock.Mock()
        gateway = self.gateway(executor, [{"name": "set_light"}])
        gateway.backends["local-rules"] = Backend([{"name": "already_applied"}], "local-rules", semantic=True)

        event = gateway.handle("打开客厅灯")

        executor.assert_not_called()
        self.assertEqual(event["backend"], "local-rules")
        self.assertEqual(event["actions"], [{"name": "already_applied"}])


if __name__ == "__main__":
    unittest.main()
