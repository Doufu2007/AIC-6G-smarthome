import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai6g_runtime.deploy.contract_profile_v7 import build_runtime, upgrade_runtime
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.models import ModelResult, RuleBackend
from ai6g_runtime.router import ObservationCache, TaskRouter, HealthObservation
from ai6g_runtime.runtime import TaskRuntime


class WrongModel:
    name = "local-model"

    def __init__(self):
        self.calls = 0

    def infer(self, task, timeout_s):
        self.calls += 1
        return ModelResult(True, {"result_kind": "action", "actions": [
            {"type": "light_off", "location": "卧室"},
            {"type": "curtain_open", "location": "客厅"},
        ]}, model_real=False, backend=self.name)


class ProfileV7Tests(unittest.TestCase):
    def test_compound_actions_share_original_atomic_runtime_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            log, state = Path(td)/"requests.jsonl", Path(td)/"state.json"
            runtime = build_runtime(log, state)
            text = "打开客厅灯，然后关闭卧室窗帘"
            first = runtime.handle(text, request_id="compound-1")
            self.assertTrue(first["ok"])
            self.assertFalse(first["model_real"])
            self.assertEqual(first["execution"]["applied_actions"], 2)
            self.assertTrue(runtime.state()["lights"]["客厅"]["is_on"])
            second = runtime.handle(text, request_id="compound-1")
            self.assertTrue(second["execution"]["idempotent_replay"])
            restored = build_runtime(log, state)
            third = restored.handle(text, request_id="compound-1")
            self.assertTrue(third["execution"]["idempotent_replay"])
            self.assertEqual(third["result_origin"], "durable_receipt")
            conflict = restored.handle("关闭客厅灯，然后打开卧室灯", request_id="compound-1")
            self.assertEqual(conflict["error_code"], "request_id_conflict")

    def test_unknown_tail_condition_and_lock_leave_state_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = build_runtime(Path(td)/"requests.jsonl")
            for index, text in enumerate((
                "打开客厅灯，然后打开音响", "十分钟后打开客厅灯", "如果有人就打开客厅灯", "解锁客厅门",
            )):
                before = runtime.state()
                result = runtime.handle(text, request_id=f"unsupported-{index}")
                self.assertTrue(result["ok"])
                self.assertEqual(result["result_kind"], "clarification")
                self.assertEqual(result["execution"]["applied_actions"], 0)
                self.assertEqual(runtime.state(), before)

    def test_existing_policy_precedes_fast_path(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = build_runtime(Path(td)/"requests.jsonl")
            result = runtime.handle("不要记录日志，打开客厅灯然后关闭卧室灯")
            self.assertTrue(result["ok"])
            self.assertEqual(result["result_kind"], "refusal")
            self.assertEqual(result["execution"]["applied_actions"], 0)

    def test_expired_request_cannot_reach_contract_commit(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = build_runtime(Path(td)/"requests.jsonl")
            before = runtime.state()
            result = runtime.handle("打开客厅灯，然后关闭卧室窗帘", deadline_at=time.perf_counter()-1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "deadline_exceeded")
            self.assertEqual(runtime.state(), before)

    def test_upgrade_preserves_reservation_release_and_guards_model_plan(self):
        with tempfile.TemporaryDirectory() as td:
            cache = ObservationCache()
            base_router = TaskRouter(cache, max_inflight=1)
            cache.put(HealthObservation("local-model", True, 10, 1, time.time()))
            backend = WrongModel()
            base = TaskRuntime(router=base_router,
                backends={"local-rules": RuleBackend(), "local-model": backend},
                store=DeviceStore(), log_path=Path(td)/"requests.jsonl")
            upgraded = upgrade_runtime(base, fast_path=False)
            self.assertIs(upgraded, base)
            result = upgraded.handle("关闭客厅灯，然后打开卧室窗帘")
            self.assertTrue(result["ok"])
            self.assertEqual(result["result_kind"], "clarification")
            self.assertEqual(backend.calls, 1)
            self.assertEqual(base_router.inflight_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
