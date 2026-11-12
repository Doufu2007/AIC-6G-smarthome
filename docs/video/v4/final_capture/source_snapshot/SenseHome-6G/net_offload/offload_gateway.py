#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端家庭 AI 卸载网关。

该模块把任务分类、链路/算力状态、C 策略、后端调用、设备动作和事件日志
接成一条可复现的业务路径。策略判据仍由 ``offload_policy.c`` 提供；Python
层只负责传递观测、调用后端和记录结果。
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Optional


@dataclasses.dataclass(frozen=True)
class LinkSnapshot:
    wan_ok: bool = False
    wan_rtt_ms: Optional[float] = None
    wan_jitter_ms: Optional[float] = None
    wan_loss_pct: Optional[float] = None
    lan_ok: bool = False
    lan_rtt_ms: Optional[float] = None
    lan_jitter_ms: Optional[float] = None
    lan_loss_pct: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ComputeSnapshot:
    # None is an explicit missing observation, never an observed zero.
    model_ready: Optional[bool] = None
    queue_ms: Optional[int] = None
    free_mem_mb: Optional[int] = None
    inflight: Optional[int] = None
    source: str = "unspecified_observation"
    field_sources: Mapping[str, str] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        fields = ("model_ready", "queue_ms", "free_mem_mb", "inflight")
        out["known_fields"] = [field for field in fields if getattr(self, field) is not None]
        out["unknown_fields"] = [field for field in fields if getattr(self, field) is None]
        out["field_sources"] = {
            field: self.field_sources.get(field, self.source) if getattr(self, field) is not None else "unknown"
            for field in fields
        }
        return out

    @classmethod
    def from_value(cls, value: Any) -> Optional["ComputeSnapshot"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        get = value.get if isinstance(value, Mapping) else lambda name, default=None: getattr(value, name, default)
        ready = get("model_ready", get("ready"))
        if type(ready) not in (int, bool) or ready not in (0, 1):
            ready = None
        else:
            ready = bool(ready)

        def metric(name):
            raw = get(name)
            if raw is None or isinstance(raw, bool):
                return None
            try:
                parsed = int(raw)
                return parsed if parsed >= 0 else None
            except (ValueError, TypeError, OverflowError):
                return None

        sources = get("field_sources", {})
        return cls(
            model_ready=ready,
            queue_ms=metric("queue_ms"),
            free_mem_mb=metric("free_mem_mb"),
            inflight=metric("inflight"),
            source=get("source", "unspecified_observation"),
            field_sources=dict(sources) if isinstance(sources, Mapping) else {},
        )


@dataclasses.dataclass
class BackendResult:
    ok: bool
    semantic_complete: bool
    response: Mapping[str, Any]
    actions: list[Mapping[str, Any]]
    model_ms: Optional[float] = None
    error: Optional[str] = None
    source: str = "unknown"
    # A refusal is a terminal response from a remote planner (for example an
    # adapter-side 4xx action validation failure).  It must not be confused
    # with a transport/backend outage, which is eligible for local fallback.
    failure_kind: Optional[str] = None


@dataclasses.dataclass
class LatencyBreakdown:
    probe_ms: float = 0.0
    policy_ms: float = 0.0
    backend_ms: float = 0.0

    @property
    def total_user_ms(self) -> float:
        return self.probe_ms + self.policy_ms + self.backend_ms

    def as_dict(self) -> dict[str, float]:
        return {
            "probe_ms": round(self.probe_ms, 3),
            "policy_ms": round(self.policy_ms, 3),
            "backend_ms": round(self.backend_ms, 3),
            "total_user_ms": round(self.total_user_ms, 3),
        }


class RuleBackend:
    """最小端侧规则后端。

    真实 Dashboard 可以注入 ``handler``，让规则结果直接修改设备状态；独立
    台架则使用此默认实现，明确把复杂 NLU 标为未完成。
    """

    def __init__(self, handler: Optional[Callable[[str, Optional[Mapping[str, Any]]], Mapping[str, Any]]] = None):
        self._handler = handler

    def infer(self, command: str, context: Optional[Mapping[str, Any]] = None) -> BackendResult:
        if self._handler is not None:
            raw = dict(self._handler(command, context))
            return BackendResult(
                ok=bool(raw.get("interface_ok", True)),
                semantic_complete=bool(raw.get("semantic_complete", True)),
                response=raw,
                actions=list(raw.get("actions", [])),
                error=raw.get("error"),
                source="local-rules",
            )
        simple = bool(re.search(r"(打开|关闭|开|关|灯|窗帘|温度|湿度)", command))
        return BackendResult(
            ok=True,
            semantic_complete=simple,
            response={"answer": "规则后端已处理" if simple else "无法理解"},
            actions=[],
            error=None if simple else "UNRESOLVED_NO_NLU",
            source="local-rules",
        )

    def health(self) -> dict[str, Any]:
        return {"ready": True, "backend": "local-rules"}


class HttpJsonBackend:
    """OpenAI-compatible 或项目自定义 JSON HTTP 后端。

    后端必须返回 JSON 对象；可选字段为 ``semantic_complete``、``actions``、
    ``answer`` 和 ``model_ms``。解析失败、超时或非 2xx 都会返回失败结果。
    """

    def __init__(
        self,
        base_url: str,
        name: str,
        timeout_s: float = 8.0,
        *,
        health_timeout_s: Optional[float] = None,
        inference_timeout_s: Optional[float] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.name = name
        if timeout_s <= 0:
            raise ValueError("backend_timeout_must_be_positive")
        self.timeout_s = float(timeout_s)  # backwards-compatible inference alias
        self.health_timeout_s = float(health_timeout_s if health_timeout_s is not None else timeout_s)
        self.inference_timeout_s = float(inference_timeout_s if inference_timeout_s is not None else timeout_s)
        if self.health_timeout_s <= 0 or self.inference_timeout_s <= 0:
            raise ValueError("backend_timeout_must_be_positive")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        timeout_s: Optional[float] = None,
    ) -> tuple[bool, dict[str, Any], float, Optional[str]]:
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False, {}, 0.0, "INVALID_BACKEND_URL"
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.hostname, parsed.port, timeout=timeout_s if timeout_s is not None else self.timeout_s)
        path_value = (parsed.path or "") + path
        raw = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        start = time.perf_counter()
        try:
            conn.request(method, path_value, body=raw, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            elapsed = (time.perf_counter() - start) * 1000.0
            obj = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(obj, dict):
                return False, {}, elapsed, "INVALID_JSON_OBJECT"
            if resp.status < 200 or resp.status >= 300:
                return False, obj, elapsed, "HTTP_%d" % resp.status
            return True, obj, elapsed, None
        except Exception as exc:  # noqa: BLE001
            return False, {}, (time.perf_counter() - start) * 1000.0, type(exc).__name__
        finally:
            conn.close()

    def infer(self, command: str, context: Optional[Mapping[str, Any]] = None) -> BackendResult:
        ok, obj, elapsed, error = self._request(
            "POST", "/infer", {"command": command, "context": dict(context or {})},
            timeout_s=self.inference_timeout_s,
        )
        if not ok:
            # The adapter uses HTTP 4xx for a rejected/ambiguous model plan.
            # Preserve that distinction so the gateway can fail closed rather
            # than handing the original command to keyword-based local rules.
            failure_kind = "model_refusal" if isinstance(error, str) and re.fullmatch(r"HTTP_4\d{2}", error) else None
            return BackendResult(
                False, False, obj, [],
                model_ms=obj.get("model_ms") if isinstance(obj, dict) else None,
                error=error, source=self.name, failure_kind=failure_kind,
            )
        actions = obj.get("actions", [])
        if not isinstance(actions, list):
            return BackendResult(False, False, obj, [], error="INVALID_ACTIONS", source=self.name)
        semantic = bool(obj.get("semantic_complete", bool(obj.get("answer") or actions)))
        source = obj.get("source", self.name)
        if not isinstance(source, str) or not source:
            source = self.name
        return BackendResult(True, semantic, obj, actions, model_ms=obj.get("model_ms", elapsed), source=source)

    def health(self) -> dict[str, Any]:
        ok, obj, elapsed, error = self._request("GET", "/health", timeout_s=self.health_timeout_s)
        # HTTP reachability and model readiness are independent observations.
        # Authoritative transport fields must not be overridden by a payload.
        return {
            **obj,
            "reachable": bool(ok),
            "ready": bool(ok and obj.get("ready", True)),
            "latency_ms": round(elapsed, 3),
            "error": error,
        }


class SubprocessPolicyClient:
    """``policy_cli`` 的安全行协议客户端。"""

    def __init__(self, cli_path: str | list[str], timeout_s: float = 2.0):
        # The CLI owns mutable smoothed link/compute state. Serialise the
        # line protocol so concurrent dashboard requests cannot interleave
        # writes and reads.
        self._lock = threading.RLock()
        if timeout_s <= 0:
            raise ValueError("policy_cli_timeout_must_be_positive")
        self.timeout_s = float(timeout_s)
        self._responses: queue.Queue[Optional[str]] = queue.Queue()
        self.proc = subprocess.Popen(
            cli_path if isinstance(cli_path, list) else [cli_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        # Windows pipe handles cannot be selected like sockets. A single
        # reader owns stdout and forwards complete lines through a queue,
        # allowing startup and command reads to share the same deadline.
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        try:
            ready = self._read_line().strip()
        except Exception:
            self.close()
            raise
        if ready != "ready":
            self.close()
            raise RuntimeError("policy_cli_not_ready:%r" % ready)

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def _kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            pass

    def _read_line(self) -> str:
        try:
            line = self._responses.get(timeout=self.timeout_s)
        except queue.Empty:
            self._kill()
            raise TimeoutError("policy_cli_timeout") from None
        if line is None:
            raise RuntimeError("policy_cli_exited")
        return line.rstrip("\n")

    def _cmd(self, line: str) -> str:
        with self._lock:
            if self.proc.poll() is not None:
                raise RuntimeError("policy_cli_exited")
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            # The protocol intentionally emits no response to quit.
            return "" if line == "quit" else self._read_line()

    def update_links(self, link: LinkSnapshot) -> float:
        start = time.perf_counter()
        with self._lock:
            for name, ok, rtt in (("wan", link.wan_ok, link.wan_rtt_ms), ("lan", link.lan_ok, link.lan_rtt_ms)):
                self._cmd("probe %s %d %d" % (name, 1 if ok else 0, int(rtt or 0)))
        return (time.perf_counter() - start) * 1000.0

    def update_compute(self, compute: Any) -> str:
        """向 C 策略进程提交可观测的模型健康状态。"""
        observation = ComputeSnapshot.from_value(compute) or ComputeSnapshot()
        fields = (observation.model_ready, observation.queue_ms, observation.free_mem_mb, observation.inflight)
        # Keep the legacy complete protocol. The partial protocol explicitly
        # marks unknown metrics with -1, interpreted only by the C policy.
        protocol = "compute_partial" if any(value is None for value in fields) else "compute"
        values = tuple(-1 if value is None else int(value) for value in fields)
        with self._lock:
            result = self._cmd((protocol + " %d %d %d %d") % values)
        if result != "ok":
            raise RuntimeError("policy_cli_compute_rejected")
        return result

    def decide(self, task_class: str, link: LinkSnapshot) -> tuple[str, str, str]:
        del link
        with self._lock:
            out = self._cmd("decide %s" % task_class)
        parts = out.split(" ", 3)
        if len(parts) < 3 or parts[0] != "decision":
            return "local-rules", "POLICY_ERROR", out
        return parts[1], parts[2], parts[3] if len(parts) > 3 else ""

    def close(self) -> None:
        with self._lock:
            try:
                if self.proc.poll() is None:
                    self._cmd("quit")
                    self.proc.wait(timeout=self.timeout_s)
            except Exception:  # noqa: BLE001
                try:
                    self._kill()
                except Exception:
                    pass
            self._reader.join(timeout=self.timeout_s)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass


class OffloadGateway:
    def __init__(
        self,
        policy: Any,
        backends: Mapping[str, Any],
        link_snapshot: Optional[LinkSnapshot] = None,
        link_probe: Optional[Callable[[], LinkSnapshot]] = None,
        compute_snapshot: Optional[ComputeSnapshot] = None,
        event_sink: Optional[Callable[[Mapping[str, Any]], None]] = None,
        action_executor: Optional[Callable[[list[Mapping[str, Any]], Optional[Mapping[str, Any]]], Mapping[str, Any]]] = None,
        receipt_executor: Optional[Callable[[list[Mapping[str, Any]], Optional[Mapping[str, Any]]], Mapping[str, Any]]] = None,
    ):
        self.policy = policy
        self.backends = dict(backends)
        self.backends.setdefault("local-rules", RuleBackend())
        self.link_snapshot = link_snapshot or LinkSnapshot()
        self.link_probe = link_probe
        self.compute_snapshot = compute_snapshot
        self.event_sink = event_sink
        self.action_executor = action_executor
        self.receipt_executor = receipt_executor
        # A stateful policy client (the C CLI) must receive link/compute
        # updates and its decision as one transaction per request.
        self._policy_lock = threading.Lock()
        self._request_order_lock = threading.Lock()
        self._policy_metadata = threading.local()
        self._decision_sequence = 0

    @staticmethod
    def needs_plan(command: str) -> bool:
        """Conservatively keep compound/conditional text out of one-action rules.

        This is a routing guard, not a complete language parser. Unknown plans
        must not become a successful partial keyword match in the fallback.
        """
        if re.search(r"(并且|同时|然后|接着|分别|如果|假如|若|不要|不许|禁止|请勿|取消|停止)", command):
            return True
        if re.search(r"(?:再|并)(?:把|将|打开|关闭|调|拉|开|关)", command):
            return True
        operations = re.findall(r"打开|关闭|开启|关掉|拉开|拉上|调亮|调暗|设为|开灯|关灯", command)
        rooms = {room for room in ("客厅", "卧室", "厨房", "浴室", "书房", "室外") if room in command}
        return len(operations) > 1 or len(rooms) > 1

    @staticmethod
    def classify(command: str) -> str:
        if re.search(r"(燃气|煤气|安防|报警|门锁|入侵|紧急|火灾)", command):
            return "safety"
        if OffloadGateway.needs_plan(command):
            return "complex"
        if re.search(r"(打开|关闭|开灯|关灯|窗帘|温度|湿度|调亮|调暗)", command):
            return "simple"
        return "complex"

    def _snapshot(self) -> tuple[LinkSnapshot, float, Optional[str]]:
        if self.link_probe is None:
            return self.link_snapshot, 0.0, None
        start = time.perf_counter()
        try:
            snapshot = self.link_probe()
        except Exception as exc:  # noqa: BLE001
            return self.link_snapshot, (time.perf_counter() - start) * 1000.0, type(exc).__name__
        self.link_snapshot = snapshot
        return snapshot, (time.perf_counter() - start) * 1000.0, None

    def _describe_policy(self) -> dict[str, Any]:
        metadata = {"implementation": type(self.policy).__name__}
        describe = getattr(self.policy, "describe", None)
        if callable(describe):
            try:
                description = describe()
            except Exception as exc:  # noqa: BLE001
                metadata["description_error"] = type(exc).__name__
            else:
                if isinstance(description, Mapping):
                    metadata.update(description)
        return metadata

    def _decide(
        self,
        task_class: str,
        link: LinkSnapshot,
        compute: Optional[ComputeSnapshot] = None,
        observation: Optional[Mapping[str, Any]] = None,
    ) -> tuple[str, str, str, float]:
        start = time.perf_counter()
        with self._policy_lock:
            try:
                if task_class == "complex" and observation is not None and hasattr(self.policy, "decide_observation"):
                    selected = self.policy.decide_observation(task_class, observation)
                else:
                    selected = None
                if (selected is None and task_class == "complex" and observation is None and
                        callable(getattr(self.policy, "clear_observation_state", None))):
                    self.policy.clear_observation_state()
                if selected is None and task_class == "complex" and hasattr(self.policy, "update_links"):
                    self.policy.update_links(link)
                if selected is None and task_class == "complex" and compute is not None and hasattr(self.policy, "update_compute"):
                    self.policy.update_compute(compute)
                if selected is None:
                    selected = self.policy.decide(task_class, link)
            except Exception as exc:  # noqa: BLE001
                self._decision_sequence += 1
                metadata = self._describe_policy()
                metadata["decision_sequence"] = self._decision_sequence
                self._policy_metadata.value = metadata
                return "local-rules", "POLICY_ERROR", type(exc).__name__, (time.perf_counter() - start) * 1000.0
            if len(selected) != 3:
                selected = ("local-rules", "POLICY_ERROR", "invalid policy result")
            if task_class == "safety":
                selected = ("local-rules", "TASK_IS_SAFETY", "safety task forced to local rules")
            elif task_class == "simple":
                selected = ("local-rules", "TASK_IS_SIMPLE", "simple task constrained to local rules")
            self._decision_sequence += 1
            metadata = self._describe_policy()
            metadata["decision_sequence"] = self._decision_sequence
            self._policy_metadata.value = metadata
        return (*selected, (time.perf_counter() - start) * 1000.0)

    @staticmethod
    def _actions_safe(actions: list[Mapping[str, Any]]) -> bool:
        """拒绝远端模型提出的高风险动作，避免模型输出直接越过本地边界。"""
        forbidden = (
            "unlock_door", "unlock", "开门", "解锁", "门禁",
            "gas", "燃气", "燃气阀", "disable_alarm", "关闭报警",
            "delete", "format", "关停安全",
            # Keep model output inside the action channel: reject shell,
            # network/file controls and common prompt-injection strings.
            "shell", "exec", "subprocess", "powershell", "python", "curl", "wget",
            "http://", "https://", "file://", "chmod", "rm -rf",
            "ignore previous", "ignore all previous", "system prompt", "developer message",
        )
        def flatten(value: Any) -> str:
            if isinstance(value, Mapping):
                return " ".join(flatten(k) + " " + flatten(v) for k, v in value.items())
            if isinstance(value, (list, tuple)):
                return " ".join(flatten(item) for item in value)
            return str(value)

        for action in actions:
            text = flatten(action)
            lowered = text.lower()
            if any(token in lowered or token in text for token in forbidden):
                return False
        return True

    @staticmethod
    def _is_model_refusal(backend_name: str, result: BackendResult) -> bool:
        """Return whether a remote backend rejected the request at its API boundary.

        ``HttpJsonBackend`` annotates HTTP 4xx responses with ``failure_kind``.
        The error-code check keeps the gateway fail-closed for injected/static
        backends that construct ``BackendResult`` directly and predate that
        annotation. Transport failures (timeouts, 5xx and disconnects) remain
        eligible for the normal local fallback path.
        """
        if backend_name == "local-rules":
            return False
        if result.failure_kind == "model_refusal":
            return True
        return isinstance(result.error, str) and bool(re.fullmatch(r"HTTP_4\d{2}", result.error))

    def handle(
        self,
        command: str,
        context: Optional[Mapping[str, Any]] = None,
        observation: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        event_id = uuid.uuid4().hex
        task_class = self.classify(command)
        # Keep each complex link sample adjacent to the stateful policy update
        # and decision so concurrent probes cannot publish out of order.
        with self._request_order_lock:
            # Local tasks do not depend on link/compute observations. Keep the
            # C policy decision in their path, but never delay device control on
            # a synchronous WAN probe. Complex tasks still obtain fresh samples.
            if task_class in {"simple", "safety"}:
                link, probe_ms, probe_error = self.link_snapshot, 0.0, None
            else:
                link, probe_ms, probe_error = self._snapshot()
            raw_compute = (context or {}).get("compute") if isinstance(context, Mapping) else None
            compute = self._coerce_compute(raw_compute) if raw_compute is not None else self.compute_snapshot
            decision_observation = observation if task_class == "complex" else None
            self._policy_metadata.value = None
            if decision_observation is None:
                backend_name, reason, policy_log, policy_ms = self._decide(task_class, link, compute)
            else:
                backend_name, reason, policy_log, policy_ms = self._decide(
                    task_class, link, compute, decision_observation
                )
        decision_backend = backend_name
        decision_reason = reason
        if probe_error:
            backend_name = "local-rules"
            reason = "LINK_PROBE_ERROR"
            policy_log = probe_error
        policy_metadata = self._policy_metadata.value
        if policy_metadata is None:
            with self._policy_lock:
                self._decision_sequence += 1
                policy_metadata = self._describe_policy()
                policy_metadata["decision_sequence"] = self._decision_sequence
        raw_key = context.get("experiment_request_key") if isinstance(context, Mapping) else None
        fallback_from = None
        fallback_attempted = False
        refusal_reason = None
        configuration_error = None
        backend = self.backends.get(backend_name)
        if backend is None:
            if backend_name != "local-rules":
                fallback_from = backend_name
                fallback_attempted = True
                configuration_error = "BACKEND_NOT_CONFIGURED"
                backend_name = "local-rules"
                reason = "BACKEND_NOT_CONFIGURED"
                policy_log = "selected backend is not configured"
            backend = self.backends["local-rules"]
        start = time.perf_counter()
        try:
            result = backend.infer(command, context)
        except Exception as exc:  # noqa: BLE001
            result = BackendResult(
                ok=False,
                semantic_complete=False,
                response={},
                actions=[],
                error=type(exc).__name__,
                source=backend_name,
            )
        backend_ms = (time.perf_counter() - start) * 1000.0
        error = result.error or configuration_error

        if backend_name != "local-rules" and result.ok and not self._actions_safe(result.actions):
            result = BackendResult(
                ok=False,
                semantic_complete=False,
                response=result.response,
                actions=[],
                model_ms=result.model_ms,
                error="UNSAFE_ACTION_REJECTED",
                source=result.source,
            )
            error = result.error

        # A remote adapter 4xx means the model plan was rejected (for example
        # an unmentioned room, unsupported device or required clarification).
        # Keep the refusal visible and terminal. Falling back to keyword rules
        # here could turn an intentionally empty/rejected plan into a physical
        # action, which violates the model boundary and the safety contract.
        model_refusal = self._is_model_refusal(backend_name, result)
        if model_refusal:
            candidate = result.response.get("error") if isinstance(result.response, Mapping) else None
            refusal_reason = candidate if isinstance(candidate, str) and candidate else result.error

        executor = self.receipt_executor or self.action_executor
        if backend_name != "local-rules" and result.ok and result.actions and executor is not None:
            execute_start = time.perf_counter()
            try:
                execution_context = ({**dict(context or {}), "_receipt_command": command}
                                     if self.receipt_executor is not None else context)
                executed = executor(result.actions, execution_context)
                if not isinstance(executed, Mapping):
                    raise TypeError("action_executor_must_return_mapping")
                actions = executed.get("actions", result.actions)
                if not isinstance(actions, list):
                    raise TypeError("action_executor_actions_must_be_list")
                result = dataclasses.replace(
                    result,
                    ok=bool(executed.get("interface_ok", result.ok)),
                    response={**result.response, **executed},
                    semantic_complete=bool(executed.get("semantic_complete", result.semantic_complete)),
                    actions=actions,
                )
                if self.receipt_executor is not None:
                    # An uncertain/partial software execution is terminal. A
                    # fallback could repeat a side effect after a lost ACK.
                    model_refusal = True
                    error = executed.get("error") or error
            except Exception as exc:  # noqa: BLE001
                result = dataclasses.replace(
                    result,
                    ok=False,
                    semantic_complete=False,
                    actions=[],
                    error="ACTION_EXECUTION_%s" % type(exc).__name__,
                )
                error = result.error
                if self.receipt_executor is not None:
                    model_refusal = True
            backend_ms += (time.perf_counter() - execute_start) * 1000.0

        if not result.ok and backend_name != "local-rules" and not model_refusal:
            fallback_from = backend_name
            fallback_attempted = True
            fallback = self.backends["local-rules"]
            fallback_start = time.perf_counter()
            try:
                result = fallback.infer(command, context)
            except Exception as exc:  # noqa: BLE001
                # The local rules path is the final safety boundary. A broken
                # fallback becomes an explicit failed event instead of
                # escaping the HTTP handler with an unstructured exception.
                result = BackendResult(
                    ok=False,
                    semantic_complete=False,
                    response={},
                    actions=[],
                    error="FALLBACK_%s" % type(exc).__name__,
                    source="local-rules",
                )
            backend_ms += (time.perf_counter() - fallback_start) * 1000.0
            backend_name = "local-rules"
            error = error or result.error

        event = {
            "event_id": event_id,
            **({"request_key": raw_key} if isinstance(raw_key, str) and 0 < len(raw_key) <= 256 else {}),
            "decision_sequence": policy_metadata["decision_sequence"],
            "selected_backend": decision_backend,
            "decision_reason": decision_reason,
            "command": command,
            "task_class": task_class,
            "backend": backend_name,
            "reason": reason,
            "policy_log": policy_log,
            "policy": policy_metadata,
            "link": link.as_dict(),
            "compute": compute.as_dict() if compute is not None else None,
            "observation": dict(observation) if observation is not None and task_class == "complex" else None,
            "latency": LatencyBreakdown(probe_ms, policy_ms, backend_ms).as_dict(),
            "semantic_complete": bool(result.semantic_complete),
            "interface_ok": bool(result.ok),
            "response": dict(result.response),
            "actions": list(result.actions),
            "model_ms": result.model_ms,
            "fallback_from": fallback_from,
            "fallback_attempted": fallback_attempted,
            "refusal_reason": refusal_reason,
            "error": ("POLICY_ERROR" if reason == "POLICY_ERROR" else (probe_error or error)),
            "source": result.source,
        }
        event["event_persisted"] = self.event_sink is not None
        if self.event_sink:
            try:
                self.event_sink(event)
            except Exception as exc:  # noqa: BLE001
                event["event_persisted"] = False
                event["event_persistence_error"] = type(exc).__name__
        return event

    @staticmethod
    def _coerce_compute(value: Any) -> Optional[ComputeSnapshot]:
        return ComputeSnapshot.from_value(value)

    def health(self) -> dict[str, Any]:
        out = {"link": self.link_snapshot.as_dict(), "backends": {}}
        for name, backend in self.backends.items():
            try:
                out["backends"][name] = backend.health()
            except Exception as exc:  # noqa: BLE001
                out["backends"][name] = {"ready": False, "error": type(exc).__name__}
        return out


def append_jsonl(path: str) -> Callable[[Mapping[str, Any]], None]:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lock = threading.Lock()

    def sink(event: Mapping[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    return sink

