"""Optional v7 contract overlay; original model calibration stays untouched.

The standalone factory needs no GPU and controls only the existing software
DeviceStore. Upgrade an owned Runtime before starting request threads.
"""
from __future__ import annotations

from ..command_contract_v7 import CONTRACT_VERSION, ContractBackend, compile_contract
from ..executor import DeviceStore
from ..grounding_v7 import ContractGuardBackend
from ..models import RuleBackend
from ..router import ObservationCache, RouteDecision, TaskRouter
from ..runtime import TaskRuntime


class ContractRouter:
    """Conservative dispatch overlay, preserving underlying model reservations."""
    def __init__(self, router, *, fast_path: bool = True):
        self.router = router
        self.fast_path = fast_path

    def __getattr__(self, name):
        return getattr(self.router, name)

    def _contract_route(self, task):
        contract = compile_contract(task.text)
        if contract is None:
            return RouteDecision("local-contract", "V7_UNSUPPORTED_COMPLETE_CONTRACT_CLARIFY",
                                 0.0, 0.0, None, False)
        if contract.result_kind != "action":
            return RouteDecision("local-contract", "V7_EXISTING_POLICY_"+contract.result_kind.upper(),
                                 0.0, 1.0, None, False)
        if self.fast_path and len(contract.actions) >= 2:
            return RouteDecision("local-contract", "V7_COMPLETE_EXPLICIT_MULTI_ACTION_CONTRACT",
                                 0.0, 1.0, None, False)
        return None

    def choose(self, task, now=None):
        decision = self._contract_route(task)
        return decision if decision is not None else self.router.choose(task, now=now)

    def choose_and_reserve(self, task, now=None):
        decision = self._contract_route(task)
        if decision is not None:
            return decision, None
        choose = getattr(self.router, "choose_and_reserve", None)
        if callable(choose):
            return choose(task, now=now)
        return self.router.choose(task, now=now), None

    def release(self, token):
        if token is not None:
            self.router.release(token)


def upgrade_runtime(runtime: TaskRuntime, *, fast_path: bool = True) -> TaskRuntime:
    """Overlay an owned idle Runtime in place, retaining state and deadline rules.

    Existing model adapters keep their observations and calibrations. Their new
    strict execution projection is *not* a newly calibrated v7 model capability;
    users must collect new calibration before claiming model quality for v7.
    """
    if isinstance(runtime.router, ContractRouter):
        runtime.router.fast_path = fast_path
        return runtime
    runtime.router = ContractRouter(runtime.router, fast_path=fast_path)
    runtime.backends = {
        name: backend if isinstance(backend, ContractGuardBackend) else ContractGuardBackend(backend)
        for name, backend in runtime.backends.items()
    }
    runtime.backends["local-contract"] = ContractBackend()
    runtime.trusted_backends = set(runtime.trusted_backends) | {"local-contract"}
    runtime.contract_profile_v7 = {
        "contract_version": CONTRACT_VERSION,
        "fast_path": fast_path,
        "model_calibration_status": "not_recalibrated_for_v7_projection",
        "scope": "explicit immediate lighting/curtain software-state execution only",
    }
    return runtime


def build_runtime(log_path, state_path=None, *, fast_path: bool = True) -> TaskRuntime:
    """Build the CPU-only strict demonstration profile (no model endpoints)."""
    runtime = TaskRuntime(router=TaskRouter(ObservationCache()),
                          backends={"local-rules": RuleBackend()},
                          store=DeviceStore(state_path=state_path), log_path=log_path)
    return upgrade_runtime(runtime, fast_path=fast_path)
