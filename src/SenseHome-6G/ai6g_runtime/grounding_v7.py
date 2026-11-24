"""Exact clause-bound plan checking for the v7 conservative command contract."""
from __future__ import annotations

from dataclasses import replace

from .command_contract_v7 import CONTRACT_VERSION, compile_contract, unsupported_reason
from .protocol import validate_plan


def _signature(actions):
    return tuple((action.type, action.location,
                  100 if action.type == "light_on" and action.brightness is None else action.brightness)
                 for action in actions)


class ContractGuardBackend:
    """Preserve transport failures, and reject action plans outside full contracts.

    Strict scope is intentional: an implicit single command or a conditional
    command cannot borrow the explicit contract's safety evidence.
    """
    def __init__(self, backend):
        self.backend = backend
        self.name = backend.name

    def __getattr__(self, key):
        return getattr(self.backend, key)

    def infer(self, task, timeout_s):
        from .protocol import classify_task
        if isinstance(task, str):
            task = classify_task(task)
        result = self.backend.infer(task, timeout_s)
        if not result.ok:
            return result
        contract = compile_contract(task.text)
        try:
            plan = validate_plan(result.payload, task)
        except (ValueError, KeyError, TypeError) as exc:
            # Retain invalid results for the Runtime's existing failure handling.
            return result
        if plan.result_kind != "action":
            return result
        reason = None
        if contract is None:
            reason = unsupported_reason(task.text)
        elif contract.result_kind != "action":
            reason = "policy_requires_no_actions"
        elif _signature(plan.actions) != _signature(contract.actions):
            reason = "complete_clause_action_sequence_mismatch"
        raw = dict(result.raw_response or {})
        trace = {"version": CONTRACT_VERSION, "intervened": reason is not None,
                 "reason": reason, "original_payload": result.payload,
                 "scope": "exact complete supported explicit command contract"}
        if contract is not None:
            trace["contract"] = contract.trace()
        raw["grounding_v7"] = trace
        if reason is None:
            return replace(result, raw_response=raw)
        payload = {"result_kind": "clarification", "actions": [],
                   "explanation": "模型计划未与完整明确指令逐项匹配，或指令超出即时灯光/窗帘合同范围；请确认每个地点、设备和动作。"}
        return replace(result, payload=payload, raw_response=raw)
