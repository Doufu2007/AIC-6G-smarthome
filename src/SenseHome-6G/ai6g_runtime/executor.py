"""Safe, idempotent software device executor."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any

from .protocol import ActionPlan, LOCATIONS


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    request_id: str
    applied_actions: int
    error: str | None = None
    idempotent_replay: bool = False
    late_commit: bool = False


class DeviceStore:
    def __init__(self, state_path: str | Path | None = None):
        self._lock = RLock()
        self._state_path = Path(state_path) if state_path else None
        self._state: dict[str, Any] = {
            "lights": {loc: {"is_on": False, "brightness": 0} for loc in LOCATIONS},
            "curtains": {loc: {"is_open": loc != "浴室", "position": 100 if loc != "浴室" else 0} for loc in LOCATIONS},
            "security": {loc: {"locked": True, "window_closed": loc != "浴室"} for loc in LOCATIONS},
        }
        self._seen: dict[str, ExecutionResult] = {}
        self._receipts: dict[str, dict[str, Any]] = {}
        if self._state_path and self._state_path.exists():
            try:
                loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError('persistent state must be an object')
                if loaded.get('schema') == 'ai6g.device_store.v2':
                    self._validate_state(loaded.get('state'))
                    self._merge_loaded(loaded['state'])
                    self._receipts = loaded['receipts']
                    if not isinstance(self._receipts, dict):
                        raise ValueError('invalid persistent receipts')
                    for request_id, receipt in self._receipts.items():
                        self._seen[request_id] = ExecutionResult(**receipt['execution'])
                else:
                    self._merge_loaded(loaded)  # explicit migration of legacy state-only files
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValueError(f'cannot load persistent device state: {exc}') from exc

    def _merge_loaded(self, loaded: dict[str, Any]) -> None:
        if not isinstance(loaded, dict):
            return
        for key in ("lights", "curtains", "security"):
            section = loaded.get(key)
            if not isinstance(section, dict):
                continue
            for location in LOCATIONS:
                if isinstance(section.get(location), dict):
                    self._state[key][location].update(section[location])

    @staticmethod
    def _validate_state(state: Any) -> None:
        if not isinstance(state, dict) or set(state) != {"lights", "curtains", "security"}:
            raise ValueError('persistent state sections are invalid')
        if not all(isinstance(state[name], dict) for name in ('lights', 'curtains', 'security')):
            raise ValueError('persistent state sections must be objects')
        for location in LOCATIONS:
            light, curtain, security = (state[name].get(location) for name in ('lights', 'curtains', 'security'))
            if not all(isinstance(item, dict) for item in (light, curtain, security)):
                raise ValueError(f'persistent state missing location: {location}')
            if not isinstance(light.get('is_on'), bool) or not isinstance(light.get('brightness'), int) or not 0 <= light['brightness'] <= 100:
                raise ValueError(f'invalid light state: {location}')
            if not isinstance(curtain.get('is_open'), bool) or not isinstance(curtain.get('position'), int) or not 0 <= curtain['position'] <= 100:
                raise ValueError(f'invalid curtain state: {location}')
            if not isinstance(security.get('locked'), bool) or not isinstance(security.get('window_closed'), bool):
                raise ValueError(f'invalid security state: {location}')

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def receipt(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._receipts.get(request_id))

    def mark_audit(self, request_id: str, status: str) -> None:
        if status not in {'pending', 'persisted', 'failed', 'not_configured'}:
            raise ValueError('invalid audit status')
        with self._lock:
            if request_id not in self._receipts:
                return
            receipts = copy.deepcopy(self._receipts)
            receipts[request_id]['audit_status'] = status
            self._save(self._state, receipts)
            self._receipts = receipts

    def _save(self, state, receipts, deadline_at=None) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'schema': 'ai6g.device_store.v2', 'state': state, 'receipts': receipts}
        # State and the idempotency receipt share one atomic replacement.
        fd, temporary = tempfile.mkstemp(prefix=self._state_path.name+'.', suffix='.tmp', dir=self._state_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            if deadline_at is not None and time.perf_counter() >= deadline_at:
                raise TimeoutError('deadline_exceeded')
            os.replace(temporary, self._state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def apply(self, plan: ActionPlan, request_id: str, *, deadline_at: float | None = None,
              request_key: str | None = None) -> ExecutionResult:
        if not isinstance(request_id, str) or not request_id.strip():
            return ExecutionResult(False, str(request_id), 0, "request_id must be non-empty")
        with self._lock:
            if deadline_at is not None and time.perf_counter() >= deadline_at:
                return ExecutionResult(False, request_id, 0, "deadline_exceeded")
            previous = self._seen.get(request_id)
            plan_data = asdict(plan)
            binding = request_key or hashlib.sha256(json.dumps(plan_data, ensure_ascii=False, sort_keys=True).encode('utf8')).hexdigest()
            if previous is not None:
                if self._receipts[request_id]['request_key'] != binding:
                    return ExecutionResult(False, request_id, 0, 'request_id_conflict')
                return ExecutionResult(previous.ok, request_id, previous.applied_actions, previous.error, True, previous.late_commit)
            candidate = copy.deepcopy(self._state)
            try:
                for action in plan.actions:
                    if action.location not in LOCATIONS:
                        raise ValueError('unknown action location')
                    if action.type == "light_on":
                        light = candidate["lights"][action.location]
                        light["is_on"] = True
                        light["brightness"] = 100 if action.brightness is None else action.brightness
                    elif action.type == "light_off":
                        light = candidate["lights"][action.location]
                        light["is_on"] = False
                        light["brightness"] = 0
                    elif action.type == "set_brightness":
                        light = candidate["lights"][action.location]
                        light["brightness"] = int(action.brightness or 0)
                        light["is_on"] = light["brightness"] > 0
                    elif action.type == "curtain_open":
                        candidate["curtains"][action.location] = {"is_open": True, "position": 100}
                    elif action.type == "curtain_close":
                        candidate["curtains"][action.location] = {"is_open": False, "position": 0}
                    elif action.type == "security_lock":
                        candidate["security"][action.location]["locked"] = True
                    elif action.type == "security_unlock":
                        candidate["security"][action.location]["locked"] = False
                    else:
                        raise ValueError('unsupported action')
                result = ExecutionResult(True, request_id, len(plan.actions))
                receipts = dict(self._receipts)
                receipts[request_id] = {'request_key': binding, 'execution': asdict(result), 'plan': plan_data,
                                        'audit_status': 'pending'}
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    return ExecutionResult(False, request_id, 0, 'deadline_exceeded')
                self._save(candidate, receipts, deadline_at)
                self._state, self._receipts = candidate, receipts
                if deadline_at is not None and time.perf_counter() >= deadline_at:
                    late = ExecutionResult(False, request_id, len(plan.actions), 'late_commit', False, True)
                    receipts[request_id]['execution'] = asdict(late)
                    try:
                        self._save(self._state, receipts)
                        self._receipts = receipts
                    except OSError:
                        # The state commit already happened. Keep the explicit in-memory late result;
                        # a later restart will surface an audit/receipt uncertainty instead of success.
                        pass
                    self._seen[request_id] = late
                    return late
                self._seen[request_id] = result
                return result
            except TimeoutError:
                return ExecutionResult(False, request_id, 0, 'deadline_exceeded')
            except (KeyError, TypeError, ValueError, OSError) as exc:
                return ExecutionResult(False, request_id, 0, f"execution failed: {exc}")
