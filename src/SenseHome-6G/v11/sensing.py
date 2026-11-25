"""Offline CSI replay, action-conditioned prediction and model version trial.

Run without arguments for a synthetic fixture. Pass --csv for a single capture
window from ESP-CSI get-started csi_recv. That input should cover one sender,
channel and packet format, with no concatenated sessions or device restarts.
"""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import mean


def observe_csi(rows, *, source='esp_csi_csv', min_frames=4, motion_threshold=5.0):
    """Summarize one capture window with an uncalibrated amplitude heuristic."""
    if min_frames < 2 or not math.isfinite(motion_threshold) or motion_threshold <= 0:
        raise ValueError('Require at least two frames and a positive motion threshold')
    if isinstance(rows, str):
        rows = rows.splitlines()
    context = None
    seen = set()
    frames = []
    discarded = 0
    for line in rows:
        if not line.startswith('CSI_DATA,'):
            continue
        try:
            row = next(csv.reader([line], strict=True))
            if len(row) not in (15, 25):
                raise ValueError('Unsupported ESP-CSI packet layout')
            sequence = int(row[1])
            frame_length, first_word_invalid = map(int, row[-3:-1])
            iq = json.loads(row[-1])
            if (not isinstance(iq, list) or len(iq) != frame_length or frame_length % 2
                    or any(type(value) is not int or not -32768 <= value <= 32767 for value in iq)
                    or first_word_invalid not in (0, 1)):
                raise ValueError('Invalid IQ array')
            offset = 4 if first_word_invalid else 0
            if sequence < 0 or not row[2] or frame_length <= offset:
                raise ValueError('Incomplete CSI frame')
            channel_index = 8 if len(row) == 15 else 16
            packet_context = len(row), row[2], int(row[channel_index]), row[-4], frame_length, offset
            if (context is not None and context != packet_context) or sequence in seen:
                raise ValueError('CSI frame is incompatible with this window')
        except (csv.Error, ValueError):
            discarded += 1
            continue
        context = packet_context
        seen.add(sequence)
        frames.append([math.hypot(iq[index], iq[index + 1]) for index in range(offset, frame_length, 2)])

    valid = len(frames)
    ratio = valid / max(1, valid + discarded)
    quality = {'valid_frames': valid, 'discarded_frames': discarded, 'valid_ratio': round(ratio, 3)}
    if valid < min_frames or ratio < 0.5:
        return {'status': 'missing', 'quality': quality, 'motion_score': None, 'source': source}
    score = mean(mean(abs(after - before) for before, after in zip(previous, current))
                 for previous, current in zip(frames, frames[1:]))
    return {'status': 'motion' if score >= motion_threshold else 'quiet',
            'quality': quality, 'motion_score': round(score, 3), 'source': source}


@dataclass(frozen=True)
class Transition:
    sample_id: str
    state: str
    action: str
    csi_status: str
    next_state: str


class ActionStateModel:
    """Empirical next-state frequencies from labelled, action-aligned windows."""

    def __init__(self, version, training, *, use_csi=True):
        training = tuple(training)
        self.version = version
        self.training_ids = frozenset(row.sample_id for row in training)
        if not version or not training or len(self.training_ids) != len(training):
            raise ValueError('Model requires a version and distinct training sample IDs')
        self._base = defaultdict(Counter)
        self._observed = defaultdict(Counter)
        for row in training:
            self._base[row.state, row.action][row.next_state] += 1
            if use_csi and row.csi_status in ('motion', 'quiet'):
                self._observed[row.state, row.action, row.csi_status][row.next_state] += 1

    def predict(self, state, action, csi_status='missing'):
        observed = self._observed.get((state, action, csi_status))
        counts = observed or self._base.get((state, action))
        probabilities = {}
        predicted_state = 'unknown'
        basis = 'unseen_state_action'
        if counts:
            total = sum(counts.values())
            probabilities = {item: count / total for item, count in counts.items()}
            predicted_state = max(probabilities, key=probabilities.get)
            confidence = probabilities[predicted_state]
            if sum(value == confidence for value in probabilities.values()) > 1:
                predicted_state = 'unknown'
            basis = 'state_action_csi' if observed else 'state_action_only'
        return {'state': state, 'action': action, 'csi_status': csi_status, 'predicted_state': predicted_state,
                'confidence': probabilities.get(predicted_state), 'model_version': self.version,
                'basis': basis, 'support': sum(counts.values()) if counts else 0,
                'state_frequencies': probabilities}


class OfflineModelTrial:
    """Evaluate a local model candidate before promotion, with reversible history."""

    def __init__(self, baseline, validation):
        self._validation = tuple(validation)
        self._validation_ids = {row.sample_id for row in self._validation}
        if (not self._validation or len(self._validation_ids) != len(self._validation)
                or baseline.training_ids & self._validation_ids):
            raise ValueError('Validation IDs must be distinct and held out from training')
        self._active = baseline
        self._prior = []
        self._versions = {baseline.version: baseline}
        self._evaluated = {}
        self.history = []

    @property
    def active_version(self):
        return self._active.version

    def evaluate_candidate(self, candidate):
        version = candidate.version
        if (version == self.active_version or self._versions.get(version, candidate) is not candidate
                or candidate.training_ids & self._validation_ids):
            raise ValueError('Candidate must be a distinct version trained on disjoint samples')
        baseline_correct = set()
        candidate_correct = set()
        for row in self._validation:
            args = row.state, row.action, row.csi_status
            if self._active.predict(*args)['predicted_state'] == row.next_state:
                baseline_correct.add(row.sample_id)
            if candidate.predict(*args)['predicted_state'] == row.next_state:
                candidate_correct.add(row.sample_id)
        regressions = sorted(baseline_correct - candidate_correct)
        eligible = len(candidate_correct) > len(baseline_correct) and not regressions
        result = {'operation': 'evaluate', 'baseline_version': self.active_version, 'version': version,
                  'validation_cases': len(self._validation), 'baseline_correct': len(baseline_correct),
                  'candidate_correct': len(candidate_correct), 'regression_ids': regressions,
                  'eligible': eligible}
        self._versions[version] = candidate
        self._evaluated[version] = eligible
        self.history.append(result)
        return dict(result, regression_ids=regressions.copy())

    def promote(self, version):
        if not self._evaluated.get(version):
            raise ValueError('Promotion requires an eligible evaluation against the active model')
        self._prior.append(self._active)
        self._active = self._versions[version]
        self._evaluated.clear()
        self.history.append({'operation': 'promote', 'version': self.active_version})

    def rollback(self):
        if not self._prior:
            raise ValueError('No previous active version')
        self._active = self._prior.pop()
        self._evaluated.clear()
        self.history.append({'operation': 'rollback', 'version': self.active_version})


def run_demo():
    """Return a JSON-ready report made entirely from synthetic fixtures."""
    def capture(amplitudes):
        lines = [f'CSI_DATA,{index},fixture,-30,11,-96,32,4,11,0,47,0,4,0,"[0,{amp},0,4]"'
                 for index, amp in enumerate(amplitudes)]
        return observe_csi(lines, source='synthetic_fixture')

    motion = capture((4, 40, 4, 40))
    quiet = capture((4, 4, 4, 4))
    missing = capture(())
    training = (
        Transition('train-1', 'closed', 'open', 'motion', 'open'),
        Transition('train-2', 'closed', 'open', 'motion', 'open'),
        Transition('train-3', 'closed', 'open', 'quiet', 'closed'),
        Transition('train-4', 'closed', 'hold', 'motion', 'closed'),
    )
    validation = (
        Transition('validation-1', 'closed', 'open', 'motion', 'open'),
        Transition('validation-2', 'closed', 'open', 'quiet', 'closed'),
        Transition('validation-3', 'closed', 'hold', 'motion', 'closed'),
    )
    baseline = ActionStateModel('fixture-baseline-v1', training, use_csi=False)
    candidate = ActionStateModel('fixture-candidate-v2', training)
    predictions = [candidate.predict('closed', action, observed['status'])
                   for action, observed in (('open', motion), ('open', quiet), ('hold', motion), ('open', missing))]
    trial = OfflineModelTrial(baseline, validation)
    trial.evaluate_candidate(candidate)
    trial.promote(candidate.version)
    trial.rollback()
    return {'mode': 'offline_fixture', 'csi': {'motion': motion, 'quiet': quiet, 'missing': missing},
            'wm': {'predictions': predictions},
            'rsi': {'history': trial.history, 'active_version': trial.active_version}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, help='replay one ESP-CSI get-started capture window')
    args = parser.parse_args()
    if args.csv:
        with args.csv.open(encoding='utf-8-sig') as capture_file:
            report = observe_csi(capture_file)
    else:
        report = run_demo()
    print(json.dumps(report, ensure_ascii=False, indent=2))
