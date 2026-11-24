"""Complete, conservative contracts for explicit immediate device commands.

This is a small grammar, not a language model or a semantic oracle. A contract
exists only when every clause is consumed. Unsupported text stays unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import time

from .models import ModelResult
from .protocol import LOCATIONS, _parse_rule_action, classify_task, validate_plan

CONTRACT_VERSION = "sensehome.command_contract.v7.1"
SUPPORTED_ACTIONS = frozenset({"light_on", "light_off", "set_brightness", "curtain_open", "curtain_close"})
_SEPARATOR = re.compile(
    r"(?:[，,；;。]\s*(?:(?:然后|接着|随后|并且|同时|以及|并)\s*)?|(?:然后|接着|随后|并且|同时|以及|并))"
)
_CONDITIONAL_OR_DELAYED = re.compile(
    r"如果|假如|若|只要|除非|否则|等到|直到|一旦|当|时候|之后|以后|稍后|"
    r"待会|一会|过会|分钟|小时|秒|明天|后天|今晚|早上|晚上|每天|定时|延迟|"
    r"[0-9零一二三四五六七八九十两]+\s*[点时]"
)


@dataclass(frozen=True)
class ContractAction:
    type: str
    location: str
    brightness: int | None
    clause: str

    def payload(self) -> dict:
        result = {"type": self.type, "location": self.location}
        if self.brightness is not None:
            result["brightness"] = self.brightness
        return result


@dataclass(frozen=True)
class CommandContract:
    text: str
    clauses: tuple[str, ...]
    actions: tuple[ContractAction, ...]
    result_kind: str = "action"
    explanation: str = "全部明确子句已完整匹配即时灯光/窗帘动作合同。"
    version: str = CONTRACT_VERSION

    def payload(self) -> dict:
        return {"result_kind": self.result_kind,
                "actions": [action.payload() for action in self.actions],
                "explanation": self.explanation}

    def trace(self) -> dict:
        return {"version": self.version,
                "compiler_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "clauses": list(self.clauses), "action_count": len(self.actions),
                "scope": "explicit immediate lighting/curtain grammar; no full natural-language guarantee"}


def unsupported_reason(text: str) -> str:
    """Give a stable reason without guessing a missing action or parameter."""
    if _CONDITIONAL_OR_DELAYED.search(text):
        return "conditional_or_delayed_not_supported"
    return "complete_explicit_contract_not_supported"


def _parse_clause(clause: str) -> ContractAction | None:
    rooms = [room for room in LOCATIONS if room in clause]
    if len(rooms) != 1:
        return None
    parsed = classify_task(clause)
    if parsed.policy_result or not parsed.rule_eligible:
        return None
    kind, brightness = _parse_rule_action(clause, rooms[0])
    if kind not in SUPPORTED_ACTIONS:
        return None
    if kind == "light_on" and brightness is None:
        brightness = 100
    action = ContractAction(kind, rooms[0], brightness, clause)
    # The old public protocol remains the execution authority.
    validate_plan({"result_kind": "action", "actions": [action.payload()]}, parsed)
    return action


def compile_contract(text: str) -> CommandContract | None:
    """Return an all-or-nothing explicit contract (1--8 actions), or None.

    Every clause needs its own supported room. Policy refusal/clarification
    precedes grammar matching. No pronoun inheritance or partial compilation.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    original = text.strip()
    task = classify_task(original)
    if task.policy_result:
        return CommandContract(original, (original,), (), task.policy_result, task.policy_reason)
    if _CONDITIONAL_OR_DELAYED.search(original):
        return None
    normalized = re.sub(r"[。！!？?]+$", "", original).strip()
    # First try the full single-action grammar; it includes a brightness suffix
    # separated by a comma, which must not be mistaken for a second command.
    single = _parse_clause(normalized)
    if single is not None:
        return CommandContract(original, (normalized,), (single,))
    clauses = tuple(clause.strip() for clause in _SEPARATOR.split(normalized))
    if not 2 <= len(clauses) <= 8 or any(not clause for clause in clauses):
        return None
    actions = tuple(_parse_clause(clause) for clause in clauses)
    if any(action is None for action in actions):
        return None
    contract = CommandContract(original, clauses, actions)
    validate_plan(contract.payload(), task)
    return contract


class ContractBackend:
    """Deterministic local contract execution or explicit scope clarification."""
    name = "local-contract"

    def infer(self, task, timeout_s: float) -> ModelResult:
        started = time.perf_counter()
        text = task if isinstance(task, str) else task.text
        contract = compile_contract(text)
        if contract is None:
            payload = {"result_kind": "clarification", "actions": [],
                       "explanation": "当前模式仅支持明确地点的即时灯光/窗帘指令；请补全每个动作，条件和定时任务需人工确认。"}
            trace = {"version": CONTRACT_VERSION, "supported": False, "reason": unsupported_reason(text)}
        else:
            payload, trace = contract.payload(), {**contract.trace(), "supported": True}
        return ModelResult(ok=True, payload=payload, model_real=False, backend=self.name,
                           latency_ms=(time.perf_counter()-started)*1000,
                           raw_response={"command_contract_v7": trace,
                                         "evidence_kind": "deterministic_contract_no_model_dispatch"})
