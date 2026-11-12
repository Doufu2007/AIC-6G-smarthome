"""Planner call costs and separately counted plan quality, not task completion."""
from copy import deepcopy
import math
import re
import threading
import time


def task_key(command):
    reads = bool(re.search(r'温度|湿度', command))
    multi = len(re.findall(r'客厅|卧室|厨房|浴室|书房|室外', command)) > 1 or bool(re.search(r'再|并|然后', command))
    return ('read' if reads else 'control') + ('_multi' if multi else '_single')


class OnlineMetrics:
    def __init__(self, *, prior_ms=1000., max_age_s=30., clock=time.monotonic):
        self.clock, self.prior_ms, self.max_age_s = clock, prior_ms, max_age_s
        self.lock = threading.Lock()
        self.queued, self.active, self.costs = 0, {}, {}
        self.last_queue_wait_ms = None

    def waiting(self, command):
        token = {'task': task_key(command), 'queued_at': self.clock()}
        with self.lock:
            self.queued += 1
        return token

    def admitted(self, token):
        with self.lock:
            self.queued -= 1
            token['admitted_at'] = self.clock()
            self.last_queue_wait_ms = max(0., token['admitted_at'] - token['queued_at']) * 1000
            self.active[id(token)] = token
            sample = self.costs.setdefault(token['task'], {'wall_ms': None, 'samples': 0,
                'planner_attempts': 0, 'planner_completed_attempts': 0,
                'planner_response_ok': 0, 'valid_plans': 0, 'at': token['admitted_at']})
            sample['planner_attempts'] += 1

    def abandoned(self, token):
        with self.lock:
            self.queued -= 1

    def finished(self, token, *, success, valid_plan=False):
        """Record every completed attempt; success only describes adapter status.

        Callers must separately prove a nonempty complete goal-matching plan.
        The legacy ``success=True`` argument grants no semantic-quality credit.
        """
        now = self.clock()
        with self.lock:
            if self.active.pop(id(token), None) is None:
                return
            elapsed = max(0., now - token['admitted_at']) * 1000
            sample = self.costs[token['task']]
            sample['wall_ms'] = elapsed if sample['wall_ms'] is None else sample['wall_ms'] * .6 + elapsed * .4
            sample['samples'] += 1
            sample['planner_completed_attempts'] += 1
            sample['planner_response_ok'] += int(success is True)
            sample['valid_plans'] += int(success is True and valid_plan is True)
            sample['at'] = now

    def snapshot(self):
        now = self.clock()
        with self.lock:
            costs = {key: {**{k: v for k, v in value.items() if k != 'at'},
                           'age_ms': max(0., now - value['at']) * 1000,
                           'usable': value['samples'] >= 2 and now - value['at'] <= self.max_age_s,
                           'source': 'adapter_planner_wall_ewma', 'includes_transport': True,
                           'scope': 'planner_call_only', 'includes_device_execution': False,
                           'cost_samples_include_failed_and_empty_plans': True,
                           'valid_plan_rate': (value['valid_plans'] / value['planner_completed_attempts']
                                               if value['planner_completed_attempts'] else None),
                           'quality_inference': 'descriptive_counts_only',
                           'quality_sample_scope': 'since_adapter_start'}
                     for key, value in self.costs.items()}
            def estimate(key):
                value = costs.get(key, {})
                return value.get('wall_ms', self.prior_ms) if value.get('usable') else self.prior_ms
            residual = sum(max(1., estimate(t['task']) - (now - t['admitted_at']) * 1000) for t in self.active.values())
            queue = residual + self.queued * self.prior_ms
            return {'queue_ms': math.ceil(queue), 'queue_scope': 'adapter_serial_admission',
                    'queue_estimate_source': 'adapter_inflight_residual_and_waiting_count',
                    'queue_prior_ms': self.prior_ms, 'waiting_requests': self.queued,
                    'inflight': len(self.active), 'inflight_source': 'adapter_admitted_requests',
                    'last_admission_wait_ms': self.last_queue_wait_ms,
                    'upstream_queue_ms': None, 'free_mem_mb': None,
                    'cost_by_task': deepcopy(costs), 'resource_sampled_monotonic': now}
