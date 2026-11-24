import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.protocol import ActionPlan, ProtocolError, classify_task, validate_plan
from ai6g_runtime.executor import DeviceStore


class ProtocolExecutorTests(unittest.TestCase):
    def test_classify_simple_light_command(self):
        task = classify_task("\u6253\u5f00\u5ba2\u5385\u7684\u706f")
        self.assertEqual(task.task_class, "simple")
        self.assertEqual(task.location, "客厅")
        self.assertEqual(task.intent, "light_on")

    def test_validate_plan_rejects_unknown_action_and_location(self):
        task = classify_task("\u6253\u5f00\u5ba2\u5385\u7684\u706f")
        with self.assertRaises(ProtocolError):
            validate_plan({"actions": [{"type": "delete_everything", "location": "客厅"}]}, task)
        with self.assertRaises(ProtocolError):
            validate_plan({"actions": [{"type": "light_on", "location": "车库"}]}, task)

    def test_device_store_applies_action_and_idempotent_replay(self):
        task = classify_task("\u6253\u5f00\u5ba2\u5385\u7684\u706f")
        plan = validate_plan({"actions": [{"type": "light_on", "location": "客厅", "brightness": 80}]}, task)
        store = DeviceStore()
        first = store.apply(plan, "req-1")
        second = store.apply(plan, "req-1")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(store.snapshot()["lights"]["客厅"]["brightness"], 80)

    def test_validate_plan_rejects_non_list_and_out_of_range_brightness(self):
        task = classify_task("\u6253\u5f00\u5ba2\u5385\u7684\u706f")
        with self.assertRaises(ProtocolError):
            validate_plan({"actions": "bad"}, task)
        with self.assertRaises(ProtocolError):
            validate_plan({"actions": [{"type": "light_on", "location": "客厅", "brightness": 101}]}, task)

    def test_classify_marks_only_explicit_single_action_as_rule_eligible(self):
        self.assertTrue(classify_task("打开客厅的灯").rule_eligible)
        self.assertFalse(classify_task("关闭客厅和卧室的灯").rule_eligible)
        self.assertFalse(classify_task("不要打开客厅的灯").rule_eligible)
        self.assertFalse(classify_task("帮我把书房灯调到舒服的亮度").rule_eligible)

    def test_privacy_phrases_cover_no_network_variants(self):
        for text in ("不要联网，打开客厅的灯", "不联网，打开客厅的灯", "禁止联网，打开客厅的灯"):
            self.assertEqual(classify_task(text).privacy, "strict")

    def test_single_character_and_marker_blocks_rule_path(self):
        task = classify_task("打开卧室灯并拉开卧室窗帘")
        self.assertFalse(task.rule_eligible)

    def test_lock_and_curtain_rules_require_complete_supported_command(self):
        self.assertTrue(classify_task("锁上客厅门").rule_eligible)
        self.assertTrue(classify_task("关上客厅窗帘").rule_eligible)
        self.assertFalse(classify_task("锁上门").rule_eligible)
        self.assertFalse(classify_task("关上客厅窗帘，稍后再打开").rule_eligible)

    def test_validate_allows_empty_clarification_or_refusal_only(self):
        task = classify_task("发生烟雾报警，请帮忙")
        clarification = validate_plan({
            "result_kind": "clarification", "actions": [], "explanation": "请确认是否执行锁门"
        }, task)
        refusal = validate_plan({
            "result_kind": "refusal", "actions": [], "explanation": "危险请求需要人工确认"
        }, classify_task("发生烟雾报警，请锁上客厅门"))
        self.assertEqual(clarification.result_kind, "clarification")
        self.assertEqual(refusal.result_kind, "refusal")
        self.assertEqual(clarification.actions, ())

    def test_validate_rejects_actions_for_clarification_and_missing_explanation(self):
        task = classify_task("发生烟雾报警，请锁上客厅门")
        with self.assertRaises(ProtocolError):
            validate_plan({"result_kind": "clarification", "actions": [{
                "type": "security_lock", "location": "客厅"
            }], "explanation": "确认"}, task)
        with self.assertRaises(ProtocolError):
            validate_plan({"result_kind": "refusal", "actions": []}, task)


if __name__ == "__main__":
    unittest.main()

