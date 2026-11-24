"""Task and action protocol used by every inference backend."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

LOCATIONS = ("客厅", "卧室", "厨房", "浴室", "书房", "室外")
ACTION_TYPES = {
    "light_on", "light_off", "set_brightness", "curtain_open", "curtain_close",
    "security_lock", "security_unlock",
}


class ProtocolError(ValueError):
    """Raised when a task or model action violates the execution protocol."""


@dataclass(frozen=True)
class TaskSpec:
    text: str
    task_class: str
    intent: str = "unknown"
    location: str | None = None
    deadline_ms: int = 3000
    quality_floor: float = 0.8
    privacy: str = "standard"
    rule_eligible: bool = False
    family: str = "general"
    policy_result: str | None = None
    policy_reason: str = ""
    brightness: int | None = None


@dataclass(frozen=True)
class Action:
    type: str
    location: str
    brightness: int | None = None


@dataclass(frozen=True)
class ActionPlan:
    actions: tuple[Action, ...]
    explanation: str = ""
    confidence: float | None = None
    result_kind: str = "action"


def _find_location(text: str) -> str | None:
    for location in LOCATIONS:
        if location in text:
            return location
    return None


def _parse_rule_action(text: str, location: str | None) -> tuple[str, int | None]:
    """Recognize a complete supported command; never discard unknown clauses."""
    if location is None:
        return "unknown", None
    command = re.sub(r"\s+", "", text).strip("。！!？?")
    command = re.sub(
        r"^(?:(?:不要上传(?:任何)?信息|不要联网|不联网|禁止联网|仅在本地|只在本地|本地)[，,、：:]?)+",
        "", command,
    )
    command = re.sub(r"^(?:请|帮我|麻烦|把|将)+", "", command)
    command = re.sub(r"(?:一下|谢谢|吧)$", "", command)
    loc = re.escape(location)
    lamp = loc + r"的?(?:灯|照明)"
    number = r"([+-]?\d+(?:\.\d+)?)"
    match = re.fullmatch(
        lamp + r"的?(?:亮度)?(?:调到|调成|调为|设为|设置为|设置到|调整到|调节到)" + number + r"%?",
        command,
    )
    if match:
        raw = match.group(1)
        if raw.isdecimal() and 0 <= int(raw) <= 100:
            return "set_brightness", int(raw)
        return "unknown", None
    brightness = None
    suffix = re.search(
        r"[，,]?(?:并且|并)?(?:设|设置|调整|调节)?亮度(?:调到|调成|调为|设为|设置为|为|到)?" + number + r"%?$",
        command,
    )
    if suffix:
        raw = suffix.group(1)
        if not raw.isdecimal() or not 0 <= int(raw) <= 100:
            return "unknown", None
        brightness = int(raw)
        command = command[:suffix.start()]
    for pattern in (
        r"(?:打开|开启|开一下|开)" + lamp,
        lamp + r"(?:打开|开启|开)",
    ):
        if re.fullmatch(pattern, command):
            return "light_on", brightness
    # A brightness suffix without an on command is not silently repurposed.
    if brightness is not None:
        return "unknown", None
    patterns = (
        ("light_off", r"(?:关闭|关掉|关上|关)" + lamp),
        ("light_off", lamp + r"(?:关闭|关掉|关上|关)"),
        ("curtain_open", r"(?:打开|拉开|开启)" + loc + r"的?窗帘"),
        ("curtain_open", loc + r"的?窗帘(?:打开|拉开|开启)"),
        ("curtain_close", r"(?:关闭|拉上|关上)" + loc + r"的?窗帘"),
        ("curtain_close", loc + r"的?窗帘(?:关闭|拉上|关上)"),
        ("security_lock", r"(?:锁上|锁住)" + loc + r"的?门(?:[，,](?:启动|开启)安防)?"),
        ("security_lock", r"给?" + loc + r"的?门(?:上锁|锁上|锁住)(?:[，,](?:启动|开启)安防)?"),
        ("security_unlock", r"(?:解锁|开锁)" + loc + r"的?门"),
        ("security_unlock", loc + r"的?门(?:解锁|开锁)"),
    )
    for intent, pattern in patterns:
        if re.fullmatch(pattern, command):
            return intent, None
    return "unknown", None


def classify_task(text: str) -> TaskSpec:
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("task text must be a non-empty string")
    normalized = text.strip()
    location = _find_location(normalized)
    safety_words = ("报警", "燃气", "烟雾", "火灾", "紧急", "安防", "防盗")
    complex_words = ("有点冷", "太冷", "太热", "不舒服", "有点暗", "太暗", "不透气", "建议")
    if any(word in normalized for word in safety_words):
        task_class = "safety"
    elif any(word in normalized for word in complex_words):
        task_class = "complex"
    else:
        task_class = "simple"

    intent = "unknown"
    if any(word in normalized for word in ("打开", "开灯", "开一下")) and ("灯" in normalized or "照明" in normalized):
        intent = "light_on"
    elif any(word in normalized for word in ("关闭", "关掉", "关上", "关灯")) and ("灯" in normalized or "照明" in normalized):
        intent = "light_off"
    elif ("亮" in normalized or "调到" in normalized or "调成" in normalized or "调为" in normalized) and any(ch.isdigit() for ch in normalized):
        intent = "set_brightness"
    elif "窗帘" in normalized and any(word in normalized for word in ("打开", "拉开")):
        intent = "curtain_open"
    elif "窗帘" in normalized and any(word in normalized for word in ("关闭", "拉上")):
        intent = "curtain_close"
    elif any(word in normalized for word in ("锁门", "上锁", "锁上", "锁住")):
        intent = "security_lock"
    elif any(word in normalized for word in ("解锁", "开锁")):
        intent = "security_unlock"

    privacy = "strict" if any(word in normalized for word in (
        "隐私", "私密", "不要上传", "不要联网", "不联网", "禁止联网", "本地"
    )) else "standard"
    locations = [candidate for candidate in LOCATIONS if candidate in normalized]
    action_negated = bool(re.search(
        r"(?:不要|别|勿|无需|不用|不必|禁止|不准|不许|不允许|不能)\s*(?:再|真的|实际)?\s*(?:执行|打开|开启|开|关闭|关掉|关灯|关|拉开|拉上|锁上|锁|解锁|调亮|调暗|调整|调到|动|操作|改变|修改|把.{0,12}(?:打开|开启|关闭|调|锁|拉))",
        normalized,
    ))
    no_execute = action_negated or any(marker in normalized for marker in (
        "模拟", "仅告诉", "只告诉", "仅解释", "只解释", "只给出", "仅给出", "只提供方案", "仅提供方案", "不执行", "不操作", "不要真的执行",
    ))
    hazard = any(word in normalized for word in ("报警", "燃气", "烟雾", "火灾", "起火", "泄漏", "紧急"))
    bypass = bool(re.search(
        r"(?:删除|清空|抹除|关闭|禁用|停用|暂停|停止|绕过|跳过|忽略).{0,8}(?:审计|日志|记录|安防|安全|权限)|(?:不记录|不要记录|不留记录)",
        normalized,
    ))
    hazard_action = hazard and (intent != "unknown" or bool(re.search(
        r"打开|开启|关闭|锁上|锁住|解锁|开锁|拉开|拉上|调亮|调暗|调到|删除", normalized,
    )))
    policy_result = None
    policy_reason = ""
    if bypass:
        policy_result, policy_reason = "refusal", "不能关闭、删除或绕过安全与审计，即使请求同时包含模拟或否定动作。"
        task_class = "safety"
    elif no_execute:
        policy_result, policy_reason = "clarification", "请求禁止实际执行；请确认是否需要执行设备动作。"
    elif hazard_action:
        policy_result, policy_reason = "refusal", "不能执行灾害场景下的设备操作或绕过安全与审计。"
        task_class = "safety"
    elif hazard:
        policy_result, policy_reason = "clarification", "安全事件需要人工处置与确认，当前信息不足以执行设备动作。"

    brightness = None
    if ("灯" in normalized or "照明" in normalized) and ("亮度" in normalized or intent == "set_brightness"):
        numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", normalized)
        if len(numbers) == 1:
            raw = numbers[0]
            if raw.isdecimal() and 0 <= int(raw) <= 100:
                brightness = int(raw)
            elif policy_result is None:
                policy_result, policy_reason = "clarification", "亮度必须是0到100的整数，请确认有效数值。"

    parsed_intent, parsed_brightness = _parse_rule_action(normalized, location)
    if parsed_intent != "unknown":
        intent, brightness = parsed_intent, parsed_brightness
    multi_action = len(locations) > 1 or any(marker in normalized for marker in ("以及", "同时", "全部", "全屋", "所有"))
    actuator_families = [family for family, present in (
        ("lighting", "灯" in normalized or "照明" in normalized or "亮度" in normalized),
        ("curtain", "窗帘" in normalized),
        ("security", "门" in normalized or "安防" in normalized or "防盗" in normalized),
    ) if present]
    family = "multi_action" if multi_action or len(actuator_families) > 1 else (actuator_families[0] if actuator_families else "general")
    rule_eligible = (
        parsed_intent != "unknown" and len(locations) == 1 and policy_result is None
        and not multi_action and task_class != "complex"
    )
    deadline_ms = 1500 if task_class == "safety" else (2500 if task_class == "simple" else 5000)
    quality_floor = 0.95 if task_class == "safety" else (0.9 if task_class == "complex" else 0.8)
    return TaskSpec(normalized, task_class, intent, location, deadline_ms, quality_floor, privacy, rule_eligible,
                    family, policy_result, policy_reason, brightness)


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{name} must be a non-empty string")
    return value.strip()


def validate_plan(payload: Mapping[str, Any], task: TaskSpec) -> ActionPlan:
    if not isinstance(payload, Mapping):
        raise ProtocolError("model response must be an object")
    result_kind = payload.get("result_kind", "action")
    if result_kind not in {"action", "clarification", "refusal"}:
        raise ProtocolError("result_kind must be action, clarification, or refusal")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        raise ProtocolError("actions must be a list")
    required_result = getattr(task, "policy_result", None)
    if required_result is not None and (result_kind != required_result or raw_actions):
        raise ProtocolError(f"task policy requires {required_result} with no device actions")
    if result_kind == "action" and not raw_actions:
        raise ProtocolError("action result must contain at least one action")
    if result_kind != "action" and raw_actions:
        raise ProtocolError(f"{result_kind} result must not contain actions")
    if len(raw_actions) > 8:
        raise ProtocolError("at most 8 actions are allowed")
    actions: list[Action] = []
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            raise ProtocolError("each action must be an object")
        action_type = _require_string(raw.get("type"), "action.type")
        if action_type not in ACTION_TYPES:
            raise ProtocolError(f"unsupported action type: {action_type}")
        location = _require_string(raw.get("location"), "action.location")
        if location not in LOCATIONS:
            raise ProtocolError(f"unsupported location: {location}")
        brightness = raw.get("brightness")
        if action_type == "set_brightness" or (action_type == "light_on" and brightness is not None):
            if isinstance(brightness, bool) or not isinstance(brightness, (int, float)):
                raise ProtocolError("brightness must be numeric")
            if int(brightness) != brightness or not 0 <= int(brightness) <= 100:
                raise ProtocolError("brightness must be an integer from 0 to 100")
            brightness = int(brightness)
        elif brightness is not None:
            raise ProtocolError("brightness is only valid for light actions")
        actions.append(Action(action_type, location, brightness))
    explanation = payload.get("explanation", "")
    if not isinstance(explanation, str):
        raise ProtocolError("explanation must be a string")
    if result_kind != "action" and not explanation.strip():
        raise ProtocolError(f"{result_kind} result requires a non-empty explanation")
    confidence = payload.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ProtocolError("confidence must be between 0 and 1")
        confidence = float(confidence)
    return ActionPlan(tuple(actions), explanation[:500], confidence, result_kind)
