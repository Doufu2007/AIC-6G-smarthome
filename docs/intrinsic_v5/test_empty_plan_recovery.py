"""A transport-successful empty plan is incomplete for fully resolved goals."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
for folder in ('docs/demo_v3', 'SenseHome-6G/net_offload'):
    sys.path.insert(0, str(ROOT / folder))
from offload_gateway import BackendResult
from planning_recovery import PlanningRecoveryBackend

COMMAND = '把客厅灯光设为35%，再把卧室灯光设为25%'
ACTIONS = [{'name': 'light_control', 'params': {'location': room, 'brightness': value}}
           for room, value in [('客厅', 35), ('卧室', 25)]]


class Planner:
    def __init__(self, port, result):
        self.base_url = f'http://127.0.0.1:{port}'
        self.result, self.calls = result, []

    def health(self):
        return {'ready': True, 'reachable': True}

    def infer(self, command, context):
        self.calls.append(command)
        return deepcopy(self.result)


def wrapper(response=None, secondary_empty=False):
    a = Planner(18001, BackendResult(True, False, response or {'answer': '请指定目标亮度百分比。', 'actions': []}, []))
    b = Planner(18002, BackendResult(True, False, {}, [] if secondary_empty else deepcopy(ACTIONS)))
    return PlanningRecoveryBackend(a, b, primary_name='a', secondary_name='b',
        primary_url=a.base_url, secondary_url=b.base_url,
        total_budget_ms=10000, primary_budget_ms=4000), a, b


def test_empty_plan_for_explicit_complete_request_tries_secondary_once():
    recovery, a, b = wrapper()
    result = recovery.infer(COMMAND)
    assert len(a.calls) == len(b.calls) == 1
    assert result.actions == ACTIONS
    assert result.response['recovery']['primary_error'] == 'EMPTY_PLAN_FOR_RESOLVED_REQUEST'
    assert result.response['recovery']['reason'] == 'secondary_success'


@pytest.mark.parametrize('command,response', [
    ('客厅有点暗', None),
    ('关闭客厅燃气报警器并打开卧室灯', None),
    (COMMAND, {'actions': [], 'refusal': True}),
    (COMMAND, {'actions': [], 'clarification_required': True}),
])
def test_ambiguous_unsupported_or_explicit_terminal_response_is_not_retried(command, response):
    recovery, _, b = wrapper(response)
    assert recovery.infer(command).actions == []
    assert b.calls == []


def test_both_empty_plans_are_recorded_as_incomplete_and_never_loop():
    recovery, a, b = wrapper(secondary_empty=True)
    result = recovery.infer(COMMAND)
    assert len(a.calls) == len(b.calls) == 1
    assert result.actions == [] and not result.semantic_complete
    assert result.response['recovery']['reason'] == 'secondary_returned_no_plan'
