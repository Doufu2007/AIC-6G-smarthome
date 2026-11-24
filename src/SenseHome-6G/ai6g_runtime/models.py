"""Model backends for the SenseHome AI+6G runtime.

The OpenAI-compatible adapter is intentionally dependency-free. A successful HTTP
response is marked as ``model_real``; local deterministic parsing is explicitly
marked false so evidence files cannot confuse a rule path with an LLM run.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class ModelResult:
    ok: bool
    payload: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    latency_ms: float = 0.0
    model_real: bool = False
    backend: str = "unknown"
    raw_response: Optional[Mapping[str, Any]] = None


def _task_text(task: Any) -> str:
    if isinstance(task, str):
        return task
    return str(getattr(task, "text", task))


class OpenAICompatibleBackend:
    """Call a local or remote OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        name: str,
        endpoint: str,
        model: str,
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_tokens: int = 384,
        seed: Optional[int] = 20261103,
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        if max_tokens is not None and int(max_tokens) < 1:
            raise ValueError("max_tokens must be positive")
        self.max_tokens = int(max_tokens) if max_tokens is not None else None
        self.seed = None if seed is None else int(seed)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.system_prompt = system_prompt or (
            "你是智能家居动作规划器。只输出一个合法JSON对象，不要Markdown、代码围栏或额外文字。"
            "可执行动作示例："
            "{\"result_kind\":\"action\",\"actions\":[{\"type\":\"light_on\",\"location\":\"客厅\",\"brightness\":100}],\"explanation\":\"已规划\",\"confidence\":0.99}。"
            "需要澄清时示例："
            "{\"result_kind\":\"clarification\",\"actions\":[],\"explanation\":\"请明确房间或动作\",\"confidence\":0.0}。"
            "必须拒绝时示例："
            "{\"result_kind\":\"refusal\",\"actions\":[],\"explanation\":\"该请求不允许执行\",\"confidence\":0.0}。"
            "result_kind只能是action、clarification或refusal。"
            "action结果的actions必须是1到8个对象；每个对象必须有type和location。"
            "type只能是light_on、light_off、set_brightness、curtain_open、curtain_close、security_lock、security_unlock；"
            "location只能是客厅、卧室、厨房、浴室、书房、室外。"
            "light_on和set_brightness可带brightness，必须是0到100的整数；其他动作不得带brightness。"
            "clarification和refusal的actions必须严格为空数组且explanation不能为空；信息不足、歧义、危险或无法确认时不要猜测，不要执行动作。"
        )

    def infer(self, task: Any, timeout_s: float) -> ModelResult:
        started = time.perf_counter()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": _task_text(task)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.seed is not None:
            body["seed"] = self.seed
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw: Optional[Mapping[str, Any]] = None
        try:
            with self._opener.open(request, timeout=max(0.01, timeout_s)) as response:
                raw_bytes = response.read()
            decoded = json.loads(raw_bytes.decode("utf-8"))
            raw = decoded if isinstance(decoded, Mapping) else None
            content = raw["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            payload = json.loads(content) if isinstance(content, str) else content
            if not isinstance(payload, Mapping):
                raise ValueError("model content must be a JSON object")
            return ModelResult(
                ok=True,
                payload=payload,
                latency_ms=(time.perf_counter() - started) * 1000,
                model_real=True,
                backend=self.name,
                raw_response=raw if isinstance(raw, Mapping) else None,
            )
        except json.JSONDecodeError as exc:
            return ModelResult(
                ok=False,
                error="invalid JSON from model: %s" % exc,
                latency_ms=(time.perf_counter() - started) * 1000,
                model_real=False,
                backend=self.name,
                raw_response=raw,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ModelResult(
                ok=False,
                error="invalid model response: %s" % exc,
                latency_ms=(time.perf_counter() - started) * 1000,
                model_real=False,
                backend=self.name,
                raw_response=raw,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ModelResult(
                ok=False,
                error="model endpoint unavailable: %s" % exc,
                latency_ms=(time.perf_counter() - started) * 1000,
                model_real=False,
                backend=self.name,
            )


class RuleBackend:
    """Deterministic safety path for simple, fully structured commands."""

    name = "local-rules"

    def infer(self, task: Any, timeout_s: float) -> ModelResult:
        started = time.perf_counter()
        from .protocol import classify_task

        # Reparse text even when a legacy caller provides intent fields: those
        # fields must not bypass the supported full-command grammar or policy.
        try:
            parsed = classify_task(_task_text(task))
        except (TypeError, ValueError) as exc:
            return ModelResult(False, error=f"cannot classify rule task: {exc}", backend=self.name)
        required_result = parsed.policy_result
        if required_result is not None:
            return ModelResult(
                ok=True,
                payload={
                    "result_kind": required_result,
                    "actions": [],
                    "explanation": parsed.policy_reason,
                },
                latency_ms=(time.perf_counter() - started) * 1000,
                backend=self.name,
            )
        if not parsed.rule_eligible:
            return ModelResult(
                ok=False,
                error="rule backend only accepts simple commands",
                latency_ms=(time.perf_counter() - started) * 1000,
                backend=self.name,
            )
        intent, location = parsed.intent, parsed.location
        action = {"type": str(intent), "location": str(location)}
        if intent == "light_on":
            action["brightness"] = 100 if parsed.brightness is None else parsed.brightness
        elif intent == "set_brightness":
            action["brightness"] = parsed.brightness
        return ModelResult(
            ok=True,
            payload={"actions": [action]},
            latency_ms=(time.perf_counter() - started) * 1000,
            model_real=False,
            backend=self.name,
        )
