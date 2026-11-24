import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai6g_runtime.command_contract_v7 import (
    CommandContract,
    ContractBackend,
    compile_contract,
)
from ai6g_runtime.grounding_v7 import ContractGuardBackend
from ai6g_runtime.models import ModelResult
from ai6g_runtime.protocol import classify_task


class StaticBackend:
    name = "local-model"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def infer(self, task, timeout_s):
        self.calls += 1
        return ModelResult(ok=True, payload=self.payload, model_real=True,
                           backend=self.name, raw_response={"usage": {"total_tokens": 7}})


def action_payload(*actions):
    return {"result_kind": "action", "actions": list(actions), "explanation": "ok"}


class ContractV7Tests(unittest.TestCase):
    def test_compiler_consumes_each_explicit_clause_in_order(self):
        contract = compile_contract("关闭客厅灯，然后拉开卧室窗帘")
        self.assertIsInstance(contract, CommandContract)
        self.assertEqual([(a.type, a.location) for a in contract.actions],
                         [("light_off", "客厅"), ("curtain_open", "卧室")])
        self.assertEqual([a.clause for a in contract.actions], ["关闭客厅灯", "拉开卧室窗帘"])

    def test_compiler_rejects_unknown_tail_without_executing_prefix(self):
        self.assertIsNone(compile_contract("打开客厅灯，然后打开音响"))

    def test_compiler_rejects_condition_and_timer(self):
        for text in ("如果客厅灯亮就打开卧室窗帘", "十分钟后打开客厅灯", "定时打开客厅灯"):
            with self.subTest(text=text):
                self.assertIsNone(compile_contract(text))

    def test_compiler_requires_room_per_clause(self):
        self.assertIsNone(compile_contract("关闭客厅灯，然后打开窗帘"))

    def test_policy_precedes_contract(self):
        contract = compile_contract("不要记录日志，打开客厅灯")
        self.assertEqual(contract.result_kind, "refusal")
        self.assertEqual(contract.actions, ())

    def test_backend_is_deterministic_and_marks_non_real_model(self):
        backend = ContractBackend()
        result = backend.infer(classify_task("关闭客厅灯，然后拉开卧室窗帘"), timeout_s=1)
        self.assertTrue(result.ok)
        self.assertFalse(result.model_real)
        self.assertEqual(result.backend, "local-contract")
        self.assertEqual(len(result.payload["actions"]), 2)

    def test_guard_rejects_cross_clause_room_swap(self):
        backend = StaticBackend(action_payload(
            {"type": "light_off", "location": "卧室"},
            {"type": "curtain_open", "location": "客厅"},
        ))
        result = ContractGuardBackend(backend).infer(
            classify_task("关闭客厅灯，然后拉开卧室窗帘"), timeout_s=1)
        self.assertEqual(result.payload["result_kind"], "clarification")
        self.assertEqual(result.payload["actions"], [])
        self.assertTrue(result.raw_response["grounding_v7"]["intervened"])

    def test_guard_rejects_extra_model_action_and_preserves_transport_failure(self):
        backend = StaticBackend(action_payload(
            {"type": "light_off", "location": "客厅"},
            {"type": "curtain_open", "location": "卧室"},
            {"type": "light_on", "location": "书房", "brightness": 100},
        ))
        result = ContractGuardBackend(backend).infer(
            classify_task("关闭客厅灯，然后拉开卧室窗帘"), timeout_s=1)
        self.assertEqual(result.payload["result_kind"], "clarification")

        class Failed(StaticBackend):
            def infer(self, task, timeout_s):
                return ModelResult(ok=False, error="offline", model_real=False, backend=self.name)

        failed = ContractGuardBackend(Failed(None)).infer(
            classify_task("关闭客厅灯，然后拉开卧室窗帘"), timeout_s=1)
        self.assertFalse(failed.ok)

    def test_guard_allows_exact_plan_and_single_action_compatibility(self):
        backend = StaticBackend(action_payload(
            {"type": "light_on", "location": "客厅"},
            {"type": "curtain_close", "location": "卧室"},
        ))
        result = ContractGuardBackend(backend).infer(
            classify_task("打开客厅灯，并关闭卧室窗帘"), timeout_s=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["result_kind"], "action")

    def test_compiler_supports_bounded_two_to_eight_actions(self):
        texts = ["打开客厅灯", "打开客厅灯，然后关闭卧室灯"]
        for text in texts:
            contract = compile_contract(text)
            self.assertIsNotNone(contract)
        clauses = [f"打开{room}灯" for room in ("客厅", "卧室", "厨房", "浴室", "书房", "室外")]
        self.assertEqual(len(compile_contract("，".join(clauses)).actions), 6)

    def test_eight_actions_are_complete_but_nine_and_empty_clause_rejected(self):
        clauses = ["打开客厅灯", "关闭卧室灯"]*4
        self.assertEqual(len(compile_contract("然后".join(clauses)).actions), 8)
        self.assertIsNone(compile_contract("然后".join(clauses+["打开书房灯"])))
        for text in ("打开客厅灯，，关闭卧室灯", "然后打开客厅灯", "打开客厅灯，然后"):
            self.assertIsNone(compile_contract(text))

    def test_brightness_must_match_room_and_exact_integer(self):
        backend = StaticBackend(action_payload(
            {"type": "set_brightness", "location": "客厅", "brightness": 75},
            {"type": "set_brightness", "location": "卧室", "brightness": 35},
        ))
        result = ContractGuardBackend(backend).infer(
            classify_task("客厅灯调到35，然后卧室灯调到75"), timeout_s=1)
        self.assertEqual(result.payload["result_kind"], "clarification")
        for text in ("客厅灯调到35.5", "客厅灯调到105", "客厅灯调到三十五"):
            contract = compile_contract(text)
            self.assertTrue(contract is None or (contract.result_kind == "clarification" and not contract.actions))

    def test_device_swap_verb_reversal_reordering_and_drop_are_rejected(self):
        task = classify_task("关闭客厅灯，然后打开卧室窗帘")
        mutations = (
            [{"type": "light_on", "location": "客厅"}, {"type": "curtain_open", "location": "卧室"}],
            [{"type": "curtain_close", "location": "客厅"}, {"type": "light_on", "location": "卧室"}],
            [{"type": "curtain_open", "location": "卧室"}, {"type": "light_off", "location": "客厅"}],
            [{"type": "light_off", "location": "客厅"}],
        )
        for actions in mutations:
            result = ContractGuardBackend(StaticBackend(action_payload(*actions))).infer(task, timeout_s=1)
            self.assertEqual(result.payload["result_kind"], "clarification")

    def test_guard_does_not_execute_unsupported_single_or_timed_action(self):
        for text in ("十分钟后打开客厅灯", "等到有人回家再打开客厅灯", "客厅有点暗请照顾一下"):
            result = ContractGuardBackend(StaticBackend(action_payload(
                {"type": "light_on", "location": "客厅", "brightness": 100},
            ))).infer(classify_task(text), timeout_s=1)
            self.assertEqual(result.payload["result_kind"], "clarification")


if __name__ == "__main__":
    unittest.main()
