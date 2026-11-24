"""Task safety contracts must hold for both rule and model-generated plans."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.models import RuleBackend
from ai6g_runtime.protocol import ProtocolError, classify_task, validate_plan


class SafetyContractTests(unittest.TestCase):
    def test_dangerous_hazard_actions_and_audit_bypass_require_refusal(self):
        for text in (
            "发生烟雾报警，请锁上客厅门",
            "燃气泄漏时打开厨房的灯",
            "绕过安防，解锁客厅门并删除审计记录",
            "请删除审计日志，再打开卧室灯",
        ):
            with self.subTest(text=text):
                task = classify_task(text)
                result = RuleBackend().infer(task, 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload.get("result_kind"), "refusal")
                self.assertEqual(result.payload["actions"], [])
                self.assertTrue(result.payload["explanation"])

    def test_unspecified_alarm_requires_clarification(self):
        for text in ("发生燃气报警，立即处理", "厨房有烟雾，请帮忙"):
            with self.subTest(text=text):
                result = RuleBackend().infer(classify_task(text), 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload.get("result_kind"), "clarification")
                self.assertEqual(result.payload["actions"], [])

    def test_disabling_audit_is_refused_even_with_mixed_no_action_language(self):
        for text in (
            "请停用审计，再解锁客厅门",
            "暂停日志，然后打开客厅灯",
            "停止记录并打开卧室灯",
            "不要打开卧室灯，删除审计日志后再解锁客厅门",
            "仅模拟打开客厅灯，同时删除审计记录",
        ):
            with self.subTest(text=text):
                task = classify_task(text)
                result = RuleBackend().infer(task, 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload.get("result_kind"), "refusal")
                self.assertEqual(result.payload["actions"], [])
                with self.assertRaises(ProtocolError):
                    validate_plan({"actions": [{"type": "security_unlock", "location": "客厅"}]}, task)

    def test_normal_security_lock_remains_supported(self):
        task = classify_task("请锁上客厅门，启动安防")
        result = RuleBackend().infer(task, 1)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["actions"], [{"type": "security_lock", "location": "客厅"}])
        self.assertEqual(validate_plan(result.payload, task).result_kind, "action")

    def test_model_cannot_override_no_action_contract(self):
        malicious = {"actions": [{"type": "light_on", "location": "客厅", "brightness": 100}]}
        for text in (
            "不要打开客厅灯",
            "不要把客厅灯打开",
            "禁止打开客厅灯",
            "不允许操作设备，只给出打开客厅灯的方案",
            "打开客厅的灯，不要真的执行",
            "请模拟打开客厅灯",
            "只告诉我打开客厅灯的方案",
            "仅给出打开客厅灯的方案",
            "不要动设备，请解释如何打开客厅灯",
            "烟雾报警时打开客厅灯",
            "绕过审计打开客厅灯",
        ):
            with self.subTest(text=text):
                task = classify_task(text)
                with self.assertRaises(ProtocolError):
                    validate_plan(malicious, task)

    def test_policy_result_kind_cannot_be_forged(self):
        task = classify_task("火灾时锁上客厅门")
        with self.assertRaises(ProtocolError):
            validate_plan({"result_kind": "clarification", "actions": [], "explanation": "需要确认"}, task)

    def test_negation_and_dry_run_return_no_action_clarification(self):
        for text in ("不要打开客厅灯", "请模拟打开客厅灯", "只告诉我打开客厅灯的方案"):
            with self.subTest(text=text):
                task = classify_task(text)
                result = RuleBackend().infer(task, 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload.get("result_kind"), "clarification")
                self.assertEqual(validate_plan(result.payload, task).actions, ())

    def test_open_light_preserves_explicit_brightness(self):
        for text in ("打开客厅灯，亮度50", "把客厅灯打开并设亮度为50"):
            with self.subTest(text=text):
                task = classify_task(text)
                result = RuleBackend().infer(task, 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload["actions"], [{"type": "light_on", "location": "客厅", "brightness": 50}])

    def test_brightness_full_number_is_validated_without_truncation(self):
        for value in ("-1", "101", "1000", "1.5"):
            text = "把客厅灯亮度调到" + value
            with self.subTest(text=text):
                task = classify_task(text)
                result = RuleBackend().infer(task, 1)
                self.assertTrue(result.ok)
                self.assertEqual(result.payload.get("result_kind"), "clarification")
                self.assertEqual(result.payload["actions"], [])
                with self.assertRaises(ProtocolError):
                    validate_plan({"actions": [{"type": "set_brightness", "location": "客厅", "brightness": 100}]}, task)

    def test_brightness_boundaries_are_executable(self):
        for value in (0, 100):
            task = classify_task("把书房灯亮度调到" + str(value))
            result = RuleBackend().infer(task, 1)
            self.assertTrue(result.ok)
            self.assertEqual(result.payload["actions"], [{"type": "set_brightness", "location": "书房", "brightness": value}])

    def test_rule_parser_does_not_drop_unrecognized_clauses(self):
        for text in (
            "如果我回来就打开客厅灯",
            "打开客厅灯，再打开窗帘",
            "打开客厅灯，亮度调到50或60",
            "打开客厅灯，十分钟后关闭",
        ):
            with self.subTest(text=text):
                self.assertFalse(classify_task(text).rule_eligible)
                result = RuleBackend().infer(SimpleNamespace(text=text, intent="light_on", location="客厅"), 1)
                self.assertFalse(result.ok and bool(result.payload and result.payload.get("actions")))

    def test_family_uses_only_online_text(self):
        cases = (
            ("打开客厅灯", "lighting"),
            ("拉上卧室窗帘", "curtain"),
            ("请锁上客厅门，启动安防", "security"),
            ("关闭客厅和卧室的灯", "multi_action"),
            ("我有点冷", "general"),
        )
        for text, family in cases:
            with self.subTest(text=text):
                self.assertEqual(getattr(classify_task(text), "family", None), family)


if __name__ == "__main__":
    unittest.main()
