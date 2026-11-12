"""Atomic adapter to the opt-in C dual-endpoint resource policy."""
from __future__ import annotations

import math
from typing import Any
import copy

from experiment_inputs import MODES, canonical_hash, transfer_ms, validate_config
from offload_gateway import ComputeSnapshot, LinkSnapshot, SubprocessPolicyClient


class ResourcePolicyClient(SubprocessPolicyClient):
    def __init__(self, cli_path: str, config: dict[str, Any], arm: str = "joint"):
        validate_config(config)
        super().__init__(cli_path)
        if arm not in MODES:
            self.close()
            raise ValueError("UNKNOWN_EXPERIMENT_ARM")
        self.config = dict(config)
        self.arm = arm
        self._last_observation = None
        self._observation_highwater = -1
        self.last_decision_path = "legacy_single_endpoint"
        self.transfer_costs_known = False

    def _expect_ok(self, command: str) -> None:
        if self._cmd(command) != "ok":
            raise ValueError("POLICY_INPUT_REJECTED:" + command)

    def update_links(self, link: LinkSnapshot) -> float:
        return super().update_links(link)

    def clear_observation_state(self) -> None:
        """Invalidate cached health before the current request republishes inputs."""
        with self._lock:
            self._expect_ok("resource_clear 1")
            self._last_observation = None
            self.transfer_costs_known = False

    def decide(self, task_class: str, link: LinkSnapshot) -> tuple[str, str, str]:
        with self._lock:
            self.last_decision_path = (
                "local_task_forced" if task_class in {"simple", "safety"}
                else "legacy_single_endpoint"
            )
            return super().decide(task_class, link)

    def observe(self, sample: dict[str, Any], profile: dict[str, Any]) -> None:
        cfg = self.config
        local_transfer = self._transfer_estimate(profile, sample["network"]["lan"])
        cloud_transfer = self._transfer_estimate(profile, sample["network"]["wan"])
        costs = {'local': cfg['local_infer_ms'], 'cloud': cfg['cloud_infer_ms']}
        self.online_cost_basis = {}
        estimates = sample.get('cost_estimates', {})
        # Wall-time estimates already include HTTP transport. Remove only the
        # exact terms C will add back, retaining link thresholds and evidence.
        both_rtt = all(isinstance(sample['network'][name].get('rtt_ms'), (int, float))
                       and not isinstance(sample['network'][name].get('rtt_ms'), bool) for name in ('lan', 'wan'))
        for endpoint, link_name, transfer in (('local', 'lan', local_transfer), ('cloud', 'wan', cloud_transfer)):
            value = estimates.get(endpoint, {})
            ms, count, age = value.get('wall_ms'), value.get('samples'), value.get('age_ms')
            if (value.get('usable') is True and type(count) is int and count >= 2
                    and isinstance(ms, (int, float)) and not isinstance(ms, bool) and math.isfinite(ms) and 0 <= ms <= 1000000
                    and isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age) and 0 <= age <= 30000):
                subtract = sample['network'][link_name]['rtt_ms'] if both_rtt and self.arm not in {'network-only', 'compute-only'} else 0
                if local_transfer is not None and cloud_transfer is not None and self.arm not in {'network-only', 'compute-only'}:
                    subtract += transfer
                costs[endpoint] = max(0, int(round(ms - subtract)))
                self.online_cost_basis[endpoint] = {**copy.deepcopy(value), 'c_base_cost_ms': costs[endpoint]}
        values = [
            profile["wan_rtt_max_ms"], profile["wan_jitter_max_ms"],
            profile["wan_loss_max_pct"], cfg["queue_max_ms"], cfg["inflight_max"],
            cfg["free_mem_min_mb"], costs['local'], costs['cloud'],
            -1 if local_transfer is None else local_transfer,
            -1 if cloud_transfer is None else cloud_transfer,
            0 if self.arm == "no-hysteresis" else cfg["switch_margin_ms"],
            0 if self.arm == "no-hysteresis" else cfg["recovery_samples"], MODES[self.arm],
        ]
        self.transfer_costs_known = local_transfer is not None and cloud_transfer is not None
        self._expect_ok("resource_cfg " + " ".join(str(int(v)) for v in values))
        identity = sample.get("observation_id", (sample.get("seed"), sample.get("index"),
                                               sample.get("at_ms")))
        sequence = sample.get("observation_id", sample.get("index"))
        if type(sequence) is not int or sequence < 0:
            raise ValueError("INVALID_OBSERVATION_SEQUENCE")
        out_of_order = sequence < self._observation_highwater
        if sequence > self._observation_highwater:
            self._expect_ok("resource_tick")
            self._last_observation = identity
            self._observation_highwater = sequence
        age = sample.get("observed_age_ms")
        stale = (out_of_order or bool(sample.get("source_out_of_order")) or
                 self._age_is_stale(age, cfg["observation_max_age_ms"]))
        for endpoint in ("local", "cloud"):
            compute = ComputeSnapshot.from_value(sample["compute"][endpoint]) or ComputeSnapshot()
            endpoint_age = sample.get("endpoint_age_ms", {}).get(endpoint, age)
            if stale or self._age_is_stale(endpoint_age, cfg["observation_max_age_ms"]):
                compute = ComputeSnapshot(model_ready=False, source="stale_health_cache")
            fields = (compute.model_ready, compute.queue_ms, compute.free_mem_mb, compute.inflight)
            self._expect_ok("endpoint %s %s" % (
                endpoint, " ".join(str(-1 if v is None else int(v)) for v in fields)))
        for name in ("wan", "lan"):
            link = sample["network"][name]
            # Freshness failure is fail-closed, not a reused optimistic sample.
            self._expect_ok("observe %s %d %d %d %d" % (
                name, int(bool(link["reachable"]) and not stale),
                self._wire_metric(link.get("rtt_ms"), 60000),
                self._wire_metric(link.get("jitter_ms"), 60000),
                self._wire_metric(link.get("loss_pct"), 100)))

    @staticmethod
    def _age_is_stale(value: Any, maximum: int) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True
        return not math.isfinite(value) or value < 0 or value > maximum

    @staticmethod
    def _wire_metric(value: Any, maximum: int) -> int:
        if value is None:
            return -1
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or value > maximum):
            raise ValueError("INVALID_NETWORK_OBSERVATION")
        return int(round(value))

    @staticmethod
    def _transfer_estimate(profile: dict[str, Any], link: dict[str, Any]) -> int | None:
        bandwidth = link.get("bandwidth_kbps")
        estimate = transfer_ms(profile, link)
        return None if bandwidth is None else math.ceil(estimate)

    def decide_observation(self, task_class: str, observation: dict[str, Any]) -> tuple[str, str, str]:
        """Trusted server-owned observation; never taken from request context."""
        with self._lock:
            profile = observation["profile"]
            if profile["task_class"] != task_class:
                raise ValueError("SERVICE_TASK_MISMATCH")
            if task_class == "complex":
                self.observe(observation, profile)
                self.last_decision_path = "dual_endpoint_resources"
            else:
                self.last_decision_path = "local_task_forced"
            return super().decide(task_class, LinkSnapshot())

    def describe(self) -> dict[str, Any]:
        return {"implementation": "dual_endpoint_C_policy",
                "active_decision_path": self.last_decision_path,
                "transfer_costs_known": self.transfer_costs_known,
                "arm": self.arm,
                "config": copy.deepcopy(self.config),
                "config_hash": canonical_hash(self.config),
                "online_cost_basis": copy.deepcopy(getattr(self, 'online_cost_basis', {}))}

    def select(self, sample: dict[str, Any], profile: dict[str, Any]) -> tuple[str, str, str]:
        with self._lock:
            self.observe(sample, profile)
            self.last_decision_path = (
                "dual_endpoint_resources" if profile["task_class"] == "complex"
                else "local_task_forced"
            )
            return super().decide(profile["task_class"], LinkSnapshot())
