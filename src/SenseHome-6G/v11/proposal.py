"""Catalog-bound proposals. A model proposal carries no execution authority.

Confirmation binds an immutable card, user confirmation, a request ID, the
catalog, and observed states. It verifies integrity, not the user's intent.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf8")).hexdigest()


def catalog_index(catalog):
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("catalog must be a nonempty list")
    result = {}
    for entry in catalog:
        if not isinstance(entry, dict) or not isinstance(entry.get("entity_id"), str):
            raise ValueError("invalid catalog entity")
        entity = entry["entity_id"]
        if entity in result or entry.get("kind") not in ("light", "cover"):
            raise ValueError("duplicate entity or unsupported kind")
        result[entity] = entry
    return result


def validate_payload(payload, catalog, states=None):
    """Validate a non-executable proposal or clarification; never fill intent."""
    index = catalog_index(catalog)
    if not isinstance(payload, dict) or set(payload) != {"result_kind", "actions", "reason"}:
        raise ValueError("proposal requires exactly result_kind/actions/reason")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("reason required")
    actions = payload["actions"]
    if not isinstance(actions, list): raise ValueError("actions must be a list")
    if payload["result_kind"] == "clarification":
        if actions: raise ValueError("clarification must have no actions")
        return json.loads(canonical(payload))
    if payload["result_kind"] != "proposal" or not 1 <= len(actions) <= 8:
        raise ValueError("proposal requires 1..8 actions")
    seen = set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"entity_id", "service", "data"}:
            raise ValueError("action fields invalid")
        entity, service, data = action["entity_id"], action["service"], action["data"]
        if not isinstance(entity, str) or entity not in index or entity in seen:
            raise ValueError("unknown or duplicate entity")
        seen.add(entity)
        entry = index[entity]
        if entry.get("available") is not True: raise ValueError("entity unavailable")
        allowed = {"light": ("light.turn_on", "light.turn_off"), "cover": ("cover.open_cover", "cover.close_cover")}
        if service not in allowed[entry["kind"]] or not isinstance(data, dict):
            raise ValueError("unsupported service")
        if service == "light.turn_on":
            if set(data) - {"brightness_pct"}: raise ValueError("unsupported light data")
            if "brightness_pct" in data and (type(data["brightness_pct"]) is not int or not 0 <= data["brightness_pct"] <= 100):
                raise ValueError("brightness_pct requires integer 0..100")
        elif data: raise ValueError("this service requires empty data")
    if states is not None:
        from capabilities import validate_actions
        validate_actions(actions, catalog, states)
    return json.loads(canonical(payload))


def state_identity(states):
    return {entity: {k: value.get(k) for k in ("state", "attributes", "last_updated")}
            for entity, value in states.items()}


class ProposalStore:
    def __init__(self, path, *, ttl_seconds=30, now=time.time):
        if not 0 < ttl_seconds <= 300: raise ValueError("invalid ttl")
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds; self.now = now
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, digest TEXT NOT NULL, card TEXT NOT NULL, request_id TEXT)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _fresh(self, states):
        if not isinstance(states, dict) or not states: raise ValueError("snapshot missing")
        now = self.now()
        for value in states.values():
            observed = value.get("observed_at") if isinstance(value, dict) else None
            if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= now - observed <= self.ttl_seconds:
                raise ValueError("snapshot stale or future")
            if not isinstance(value.get("state"), str) or not isinstance(value.get("last_updated"), str):
                raise ValueError("snapshot state/version missing")

    @staticmethod
    def _readable_dependencies(states):
        if any(not isinstance(value,dict) or not isinstance(value.get('state'),str)
               or not value['state'].strip() or value['state'].lower() in ('unknown','unavailable')
               or value.get('read_error_type') or not isinstance(value.get('attributes',{}),dict)
               for value in states.values()):
            raise ValueError('selection dependency unavailable; create a new proposal')

    def create(self, request_text, catalog, states, payload, metadata=None, dependency_states=None):
        validated = validate_payload(payload, catalog, states=states)
        if validated["result_kind"] != "proposal": raise ValueError("clarification cannot be confirmed")
        if not isinstance(request_text, str) or not request_text.strip(): raise ValueError("request missing")
        entities = {a["entity_id"] for a in validated["actions"]}
        if set(states) != entities: raise ValueError("snapshot must exactly cover plan")
        self._fresh(states)
        dependencies = states if dependency_states is None else dependency_states
        if not isinstance(dependencies, dict) or not entities <= set(dependencies):
            raise ValueError('dependencies must cover action entities')
        if not set(dependencies) <= set(catalog_index(catalog)):
            raise ValueError('unknown dependency entity')
        self._fresh(dependencies)
        self._readable_dependencies(dependencies)
        if state_identity({entity:dependencies[entity] for entity in entities}) != state_identity(states):
            raise ValueError('action/dependency snapshot mismatch')
        if metadata is not None and not isinstance(metadata, dict): raise ValueError('metadata must be an object')
        card = dict(proposal_id=uuid.uuid4().hex, request_text=request_text, catalog=catalog,
                    states=states, dependency_states=dependencies,
                    selection_scope='reviewed proposal snapshot; dependency changes require a new proposal',
                    payload=validated, created_at=self.now(), expires_at=self.now()+self.ttl_seconds,
                    metadata=metadata or {}, authorized=False, status="awaiting_confirmation")
        card = json.loads(canonical(card)); card["digest"] = digest(card)
        with self._connect() as conn:
            conn.execute("INSERT INTO proposals VALUES (?,?,?,NULL)", (card["proposal_id"], card["digest"], canonical(card)))
        return card

    def confirm(self, *, proposal_id, digest, request_id, confirmed, current_catalog, current_states,
                current_dependency_states=None):
        if confirmed is not True: raise ValueError("explicit boolean confirmation required")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError("invalid request ID")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT digest,card,request_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None: raise ValueError("unknown proposal")
            if digest != row[0]: raise ValueError("digest mismatch")
            card = json.loads(row[1]); previous_id = row[2]
            if previous_id is not None and previous_id != request_id: raise ValueError("proposal already bound to another request")
            if self.now() >= card["expires_at"]: raise ValueError("proposal expired")
            if canonical(current_catalog) != canonical(card["catalog"]): raise ValueError("catalog changed")
            if state_identity(current_states) != state_identity(card["states"]): raise ValueError("state changed")
            dependencies=card.get('dependency_states',card['states'])
            current_dependencies=current_states if current_dependency_states is None else current_dependency_states
            self._readable_dependencies(current_dependencies)
            if state_identity(current_dependencies) != state_identity(dependencies):
                raise ValueError('selection dependency changed; create and review a new proposal')
            self._fresh(dependencies);self._fresh(current_dependencies)
            self._fresh(card["states"]); self._fresh(current_states)
            validate_payload(card['payload'], current_catalog, states=current_states)
            conn.execute("UPDATE proposals SET request_id=? WHERE id=?", (request_id, proposal_id))
            return dict(authorized=True, request_id=request_id, proposal_id=proposal_id, digest=row[0],
                        plan=card["payload"]["actions"], expected_states=card["states"],
                        metadata=card.get('metadata', {}), expires_at=card['expires_at'],
                        duplicate_confirmation=previous_id is not None,
                        scope="Explicit confirmation of this card; not proof of intent understanding")
