"""Whitelist rejection/atomicity evidence on isolated PC state, never actuators."""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

from experiment_inputs import HERE, write_json
from experiment_replay import (contract_action, isolated_state, save_rows,
                               seal_artifacts, source_manifest)
from device_actions import execute_device_actions


def cases() -> list[tuple[str, Any, bool]]:
    light = contract_action("complex")[0]
    parameter_cases = [
        ("negative_brightness", {"brightness": -1}),
        ("excess_brightness", {"brightness": 101}),
        ("boolean_brightness", {"brightness": True}),
        ("fractional_brightness", {"brightness": 12.5}),
        ("string_brightness", {"brightness": "60"}),
        ("unknown_room", {"location": "未授权房间"}),
        ("numeric_room", {"location": 0}),
        ("unknown_operation", {"action": "exec"}),
        ("extra_parameter", {"shell": "ignored"}),
    ]
    result = []
    for name, changes in parameter_cases:
        action = copy.deepcopy(light)
        action["params"].update(changes)
        result.append((name, [action], False))
    for name in ("unlock_door", "gas_valve", "disable_alarm", "shell", "curl"):
        result.append((name, [{"name": name, "params": {"location": "客厅"}}], False))
    result += [
        ("empty_batch", [], False), ("non_list_batch", {}, False),
        ("null_batch", None, False), ("oversized_batch", [light] * 17, False),
        ("non_object_action", ["light_control"], False),
        ("missing_parameters", [{"name": "light_control"}], False),
        ("extra_schema_field", [{**light, "authorized": True}], False),
        ("non_object_parameters", [{"name": "light_control", "params": []}], False),
        ("invalid_read_fields", [{"name": "read_temperature",
                                  "params": {"location": "客厅", "exec": True}}], False),
        ("invalid_curtain_position", [{"name": "curtain_control",
                                       "params": {"location": "客厅", "position": -1}}], False),
        ("atomic_valid_then_invalid", [light, {"name": "unlock_door", "params": {}}], False),
        ("valid_light", [light], True),
        ("valid_temperature", contract_action("simple"), True),
        ("valid_curtain", [{"name": "curtain_control",
                           "params": {"location": "客厅", "position": 40}}], True),
    ]
    return result


def run_case(name: str, actions: Any, allowed: bool) -> dict[str, Any]:
    state = isolated_state()
    before = copy.deepcopy(state)
    try:
        execute_device_actions(state, copy.deepcopy(actions))
        accepted, error = True, None
    except ValueError as exc:
        accepted, error = False, str(exc)
    return {
        "case": name, "actions": actions, "expected_allowed": allowed,
        "accepted": accepted, "error": error,
        "state_unchanged": state == before,
        "passed": accepted == allowed and (accepted or state == before),
        "scope": "structured action whitelist; not a general prompt-injection benchmark",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("output must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [run_case(*case) for case in cases()]
    if not all(row["passed"] for row in rows):
        raise RuntimeError("WHITELIST_REGRESSION")
    save_rows(args.out / "raw_requests.jsonl", rows)
    write_json(args.out / "summary.json", {
        "schema_version": "sensehome.safety.v1", "sources": source_manifest(args.cli),
        "evidence_type": "isolated_PC_whitelist_rejection_and_atomicity",
        "raw_count": len(rows), "all_passed": True,
        "rejected": sum(not row["accepted"] for row in rows),
        "accepted_positive_controls": sum(row["accepted"] for row in rows),
    })
    seal_artifacts(args.out)
    print(f"whitelist cases={len(rows)} passed={sum(row['passed'] for row in rows)}")


if __name__ == "__main__":
    main()
