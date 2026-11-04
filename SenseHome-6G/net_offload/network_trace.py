#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可序列化、可复放的链路条件轨迹。

轨迹只描述实验条件，不把任何模型耗时伪装成测量值。静态臂和自适应臂
应消费同一份 ``samples``，这样差异来自策略行为而不是随机网络序列。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
from typing import Any, Iterable, Optional


@dataclasses.dataclass(frozen=True)
class TraceSample:
    index: int
    step: str
    configured_rtt_ms: Optional[float]
    jitter_ms: float
    loss_pct: float
    blackout: bool
    outcomes: Optional[dict[str, dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        # Omitting the new field keeps historical trace hashes readable.
        if self.outcomes is None:
            result.pop("outcomes")
        return result

    def outcome(self, role: str, link: str = "wan") -> dict[str, Any]:
        """Return a concrete, replayable TCP outcome, never a probability."""
        if role not in {"probe", "infer"} or link not in {"wan", "lan"}:
            raise ValueError("INVALID_TRAFFIC_ROLE")
        if self.outcomes is None or f"{link}-{role}" not in self.outcomes:
            raise ValueError("LEGACY_TRACE_HAS_NO_ACTUAL_OUTCOMES")
        outcome = self.outcomes[f"{link}-{role}"]
        rtt = outcome.get("rtt_ms")
        refused = outcome.get("refused")
        if (not isinstance(rtt, (int, float)) or isinstance(rtt, bool)
                or not math.isfinite(rtt) or rtt < 0
                or not isinstance(refused, bool)):
            raise ValueError("INVALID_REPLAY_OUTCOME")
        return dict(outcome)


@dataclasses.dataclass(frozen=True)
class NetworkTrace:
    seed: int
    samples: tuple[TraceSample, ...]
    trace_id: str

    @classmethod
    def generate(
        cls,
        seed: int,
        steps: Iterable[Optional[float]],
        probes_per_step: int,
        jitter_max_ms: Optional[float] = None,
        loss_max_pct: float = 5.0,
        lan_rtt_ms: float = 3.0,
        lan_jitter_ms: float = 1.0,
        lan_loss_pct: float = 0.0,
        lan_blackout: bool = False,
    ) -> "NetworkTrace":
        if probes_per_step < 1:
            raise ValueError("probes_per_step must be positive")
        samples: list[TraceSample] = []
        for step_value in steps:
            blackout = step_value is None
            configured = None if blackout else float(step_value)
            step = "BLACKOUT" if blackout else str(step_value)
            for _ in range(probes_per_step):
                jitter_limit = 0.0 if blackout else (
                    max(1.0, configured * 0.05) if jitter_max_ms is None else max(0.0, float(jitter_max_ms))
                )
                jitter = 0.0 if blackout else jitter_limit
                loss = 100.0 if blackout else min(100.0, max(0.0, float(loss_max_pct)))
                outcomes = {}
                for link in ("wan", "lan"):
                    base_rtt = configured if link == "wan" else float(lan_rtt_ms)
                    base_jitter = jitter if link == "wan" else max(0.0, float(lan_jitter_ms))
                    failure_pct = loss if link == "wan" else min(100.0, max(0.0, float(lan_loss_pct)))
                    is_blackout = blackout if link == "wan" else bool(lan_blackout)
                    for role in ("probe", "infer"):
                        # Independent stream per request/link/traffic role:
                        # adaptive-only probes cannot shift business failures.
                        stream = f"{seed}:{len(samples)}:{link}:{role}".encode("ascii")
                        rng = random.Random(int.from_bytes(hashlib.sha256(stream).digest(), "big"))
                        actual_rtt = max(0.0, float(base_rtt or 0.0) + rng.uniform(-base_jitter, base_jitter))
                        outcomes[f"{link}-{role}"] = {
                            "rtt_ms": round(actual_rtt, 6),
                            "refused": is_blackout or rng.random() < failure_pct / 100.0,
                        }
                samples.append(
                    TraceSample(
                        index=len(samples),
                        step=step,
                        configured_rtt_ms=configured,
                        jitter_ms=jitter,
                        loss_pct=loss,
                        blackout=blackout,
                        outcomes=outcomes,
                    )
                )
        payload = {"seed": seed, "samples": [sample.to_dict() for sample in samples]}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        trace_id = hashlib.sha256(canonical).hexdigest()[:16]
        return cls(seed=seed, samples=tuple(samples), trace_id=trace_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "trace_id": self.trace_id,
            "samples": [sample.to_dict() for sample in self.samples],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkTrace":
        samples = tuple(TraceSample(**sample) for sample in data["samples"])
        trace = cls(seed=int(data["seed"]), samples=samples, trace_id=str(data["trace_id"]))
        payload = {"seed": trace.seed, "samples": [sample.to_dict() for sample in samples]}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()[:16]
        if actual != trace.trace_id:
            raise ValueError("TRACE_ID_MISMATCH")
        return trace
