import json
import unittest

from sensing import ActionStateModel, OfflineModelTrial, Transition, observe_csi, run_demo


def sample(sequence, iq, sender='demo-link', first_word=0, *, older_format=False):
    metadata = (
        f'{sequence},{sender},-30,11,1,6,1,0,1,0,1,0,0,-93,0,13,2,2751923,0,67,0'
        if older_format else f'{sequence},{sender},-30,11,-96,32,4,11,372852,47,0'
    )
    return f'CSI_DATA,{metadata},{len(iq)},{first_word},"{json.dumps(iq)}"'


class CSIWindowTests(unittest.TestCase):
    def test_accepts_both_esp_csi_layouts_and_masks_invalid_first_word(self):
        older = [sample(index, [100 * (index % 2), 0, 0, 0, 0, 4], first_word=1, older_format=True)
                 for index in range(4)]
        newer = [sample(index, [0, amplitude, 0, 4])
                 for index, amplitude in enumerate((4, 40, 4, 40))]
        self.assertEqual(observe_csi('\n'.join(older))['status'], 'quiet')
        motion = observe_csi(newer)
        self.assertEqual(motion['status'], 'motion')
        self.assertEqual(motion['quality']['valid_frames'], 4)
        self.assertEqual(motion['source'], 'esp_csi_csv')

    def test_duplicate_malformed_or_foreign_frames_do_not_hide_missing_data(self):
        rows = [
            'serial boot log',
            sample(0, [0, 4]),
            sample(0, [0, 40]),
            sample(1, [0, 40000]),
            sample(2, [0, 40], sender='different-link'),
            sample(3, [0, 4, 0, 4]),
        ]
        result = observe_csi(rows)
        self.assertEqual(result['status'], 'missing')
        self.assertIsNone(result['motion_score'])
        self.assertEqual(result['quality']['valid_frames'], 1)
        self.assertEqual(result['quality']['discarded_frames'], 4)


class ActionStateModelTests(unittest.TestCase):
    def setUp(self):
        self.training = [
            Transition('train-1', 'closed', 'open', 'motion', 'open'),
            Transition('train-2', 'closed', 'open', 'motion', 'open'),
            Transition('train-3', 'closed', 'open', 'quiet', 'closed'),
            Transition('train-4', 'closed', 'hold', 'motion', 'closed'),
        ]
        self.validation = [
            Transition('val-1', 'closed', 'open', 'motion', 'open'),
            Transition('val-2', 'closed', 'open', 'quiet', 'closed'),
        ]
        self.baseline = ActionStateModel('v1', self.training, use_csi=False)
        self.candidate = ActionStateModel('v2', self.training, use_csi=True)

    def test_prediction_conditions_on_action_and_csi_with_explicit_fallback(self):
        predict = self.candidate.predict
        self.assertEqual(predict('closed', 'open', 'motion')['predicted_state'], 'open')
        self.assertEqual(predict('closed', 'hold', 'motion')['predicted_state'], 'closed')
        self.assertEqual(predict('closed', 'open', 'quiet')['predicted_state'], 'closed')
        self.assertEqual(predict('closed', 'open', 'missing')['basis'], 'state_action_only')
        self.assertEqual(predict('open', 'open', 'motion')['predicted_state'], 'unknown')

    def test_promotes_only_evaluated_candidate_and_rollback_restores_version(self):
        trial = OfflineModelTrial(self.baseline, self.validation)
        with self.assertRaises(ValueError):
            trial.promote('v2')
        result = trial.evaluate_candidate(self.candidate)
        self.assertEqual(result['baseline_correct'], 1)
        self.assertEqual(result['candidate_correct'], 2)
        self.assertEqual(trial.active_version, 'v1')
        trial.promote('v2')
        self.assertEqual(trial.active_version, 'v2')
        trial.rollback()
        self.assertEqual(trial.active_version, 'v1')
        self.assertEqual([item['operation'] for item in trial.history], ['evaluate', 'promote', 'rollback'])

    def test_rejects_candidates_with_no_improvement_or_validation_overlap(self):
        trial = OfflineModelTrial(self.baseline, self.validation)
        flat = ActionStateModel('flat', self.training, use_csi=False)
        self.assertFalse(trial.evaluate_candidate(flat)['eligible'])
        with self.assertRaises(ValueError):
            trial.promote('flat')
        leaking = ActionStateModel('leaking', self.training + self.validation, use_csi=True)
        with self.assertRaises(ValueError):
            trial.evaluate_candidate(leaking)

    def test_blocks_higher_total_if_candidate_breaks_a_previously_correct_case(self):
        misleading = ActionStateModel('regressed', [
            Transition('candidate-1', 'closed', 'open', 'motion', 'closed'),
            Transition('candidate-2', 'closed', 'open', 'quiet', 'closed'),
        ], use_csi=True)
        third = Transition('val-3', 'closed', 'open', 'quiet', 'closed')
        trial = OfflineModelTrial(self.baseline, self.validation + [third])
        result = trial.evaluate_candidate(misleading)
        self.assertGreater(result['candidate_correct'], result['baseline_correct'])
        self.assertEqual(result['regression_ids'], ['val-1'])
        self.assertFalse(result['eligible'])

    def test_candidate_must_be_reevaluated_after_active_model_changes(self):
        first = ActionStateModel('first', self.training, use_csi=True)
        trial = OfflineModelTrial(self.baseline, self.validation)
        trial.evaluate_candidate(first)
        trial.evaluate_candidate(self.candidate)
        trial.promote('first')
        with self.assertRaises(ValueError):
            trial.promote('v2')
        trial.rollback()
        with self.assertRaises(ValueError):
            trial.promote('v2')


class SensingDemoTests(unittest.TestCase):
    def test_report_marks_synthetic_provenance_and_can_be_serialized(self):
        report = run_demo()
        self.assertEqual(report['mode'], 'offline_fixture')
        self.assertEqual(report['csi']['motion']['source'], 'synthetic_fixture')
        self.assertEqual(report['csi']['missing']['status'], 'missing')
        self.assertEqual(report['wm']['predictions'][0]['predicted_state'], 'open')
        self.assertEqual(report['wm']['predictions'][1]['state'], 'closed')
        self.assertEqual(report['wm']['predictions'][1]['csi_status'], 'quiet')
        self.assertEqual(report['rsi']['active_version'], 'fixture-baseline-v1')
        self.assertEqual(report['rsi']['history'][-1]['operation'], 'rollback')
        self.assertEqual(json.loads(json.dumps(report)), report)
