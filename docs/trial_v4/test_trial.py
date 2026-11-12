from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_trial import DemandTrialStore, parser, erase_private_runs, ROOT, HERE


class DemandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DemandTrialStore(Path(self.tmp.name), 'automation_test', None,
            {'models_enabled': True, 'runtime_manifest': 'test-only'})
        self.sid = self.store.start({'consent': True, 'role': 'user'})['session_id']

    def test_baseline_required_before_task(self):
        with self.assertRaisesRegex(ValueError, 'BASELINE_REQUIRED'):
            self.store.execute(self.sid, {'command': '开灯', 'origin': 'self_authored'})

    def test_baseline_keeps_none_and_no_forced_problem(self):
        self.store.baseline(self.sid, {'prior_usage': '手动开关', 'recent_incident_or_none': '没有遇到',
            'impact': '无', 'desired_improvement': '暂时不知道', 'acceptable_wait': '不知道'})
        task = self.store.execute(self.sid, {'command': '开灯', 'origin': 'self_authored'})
        self.assertIsNone(task['task_completed'])
        rec = self.store.submit(self.sid, {'reason': '不想使用', 'confirm_submit': True})
        self.assertEqual(rec['baseline']['recent_incident_or_none'], '没有遇到')
        self.assertFalse(rec['allow_anonymous_quote'])
        self.assertEqual(self.store.summary()['participant_feedback_count'], 0)
        self.assertTrue(rec['system_provenance']['models_enabled'])

    def test_baseline_is_immutable_after_first_task(self):
        self.store.baseline(self.sid, {'skipped': True})
        self.store.execute(self.sid, {'command': '开灯', 'origin': 'self_authored'})
        with self.assertRaisesRegex(ValueError, 'BASELINE_FROZEN'):
            self.store.baseline(self.sid, {'prior_usage': '新答案'})

    def test_cancel_deletes_record(self):
        self.store.baseline(self.sid, {'skipped': True})
        self.store.submit(self.sid, {'reason': '不会用', 'confirm_submit': True})
        self.store.cancel(self.sid)
        self.assertEqual(self.store.records(), [])

    def test_models_are_default_rules_are_explicit(self):
        self.assertFalse(parser().parse_args([]).rules_only)
        self.assertTrue(parser().parse_args(['--rules-only']).rules_only)

    def test_oversized_baseline_rejected_without_save(self):
        with self.assertRaises(ValueError):
            self.store.baseline(self.sid, {'impact': 'x' * 1201})
        self.assertFalse(list(Path(self.tmp.name).rglob('*.json')))

    def test_one_participant_per_runtime(self):
        with self.assertRaisesRegex(ValueError, 'ONE_SESSION_PER_RUN'):
            self.store.start({'consent': True, 'role': 'user'})

    def test_utf8_questions_are_readable_and_no_literal_corruption(self):
        page = (HERE / 'index.html').read_text(encoding='utf-8')
        self.assertIn('那次经历造成了什么影响', page)
        self.assertIn('暂存回答，开始尝试', page)
        self.assertNotIn('????', page)

    def test_erasure_rejects_other_directory(self):
        with self.assertRaisesRegex(ValueError, 'REFUSE_NON_PRIVATE'):
            erase_private_runs([Path(self.tmp.name)])
        self.assertTrue(Path(self.tmp.name).exists())

    def test_erasure_only_owned_run_children(self):
        base = ROOT / '_work/demo_v4'
        base.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(dir=base))
        (directory / 'command.txt').write_text('test-only', encoding='utf-8')
        result = erase_private_runs([directory])
        self.assertTrue(result['all_absent'])
        self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
