#!/usr/bin/env python3
"""Freeze a seeded programmatic contract holdout before its only model run.

This generates labels and registration artifacts only; it makes no inference
request and does not edit production prompts, guards, policy or configuration.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/experiments/20261111_fused_real/holdout_12_preregistered"
SEED = 2026111126


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    data_path = OUT / "holdout.programmatic.jsonl"
    registration_path = OUT / "registration.json"
    if data_path.exists() or registration_path.exists():
        raise SystemExit("already_frozen_do_not_overwrite")
    rng = random.Random(SEED)
    rooms = ("客厅", "卧室", "厨房", "浴室", "书房", "室外")
    percentages = (11, 17, 26, 34, 39, 47, 53, 62, 69, 73, 82, 91)
    numbers = rng.sample(percentages, len(percentages))
    rows = []

    def action(name, room, **params):
        return {"name": name, "params": {"location": room, **params}}

    def add(identity, category, command, expected):
        rows.append({"id": identity, "category": category, "command": command,
                     "expected_actions": expected, "human_reviewed": False,
                     "label_source": "preregistered_programmatic_holdout_not_human_reviewed",
                     "label_basis": "explicit_allowlisted_actions_or_whole_request_requires_clarification_rejection",
                     "answer_semantics_scored": False})

    room_pairs = [rng.sample(rooms, 2) for _ in range(10)]
    for index in range(4):
        left, right = room_pairs[index]
        first, second = numbers[2 * index:2 * index + 2]
        if index % 2 == 0:
            command = f"请把{left}灯光亮度设为{first}%，然后把{right}窗帘位置设为{second}%"
            expected = [action("light_control", left, brightness=first), action("curtain_control", right, position=second)]
        else:
            command = f"将{left}窗帘位置设为{first}%，并把{right}灯光亮度设为{second}%"
            expected = [action("curtain_control", left, position=first), action("light_control", right, brightness=second)]
        add(f"H{index + 1:02}", "explicit_numeric_cross_room", command, expected)
    for index in range(2):
        left, right = room_pairs[index + 4]
        light_op, curtain_op = (("on", "close"), ("off", "open"))[index]
        command = f"请{'打开' if light_op == 'on' else '关闭'}{left}的灯，再{'关闭' if curtain_op == 'close' else '打开'}{right}的窗帘"
        add(f"H{index + 5:02}", "explicit_switch_cross_room", command,
            [action("light_control", left, action=light_op), action("curtain_control", right, action=curtain_op)])
    for index in range(2):
        left, right = room_pairs[index + 6]
        command = f"请读取{left}的温度，同时查看{right}的温度和湿度"
        add(f"H{index + 7:02}", "dual_room_read", command,
            [action("read_temperature", left), action("read_temperature", right)])
    add("H09", "missing_room", f"请把窗帘位置设为{numbers[8]}%", [])
    add("H10", "missing_device", f"请将{room_pairs[8][0]}那件设备设置好", [])
    for index, unsupported in enumerate(("插座", "空调")):
        left, right = room_pairs[index + 8]
        add(f"H{index + 11:02}", "unsupported_mixed_whole_request_rejection",
            f"请打开{left}{unsupported}，然后把{right}的灯光亮度设为{numbers[index + 9]}%", [])

    old_paths = [ROOT / "SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl",
                 ROOT / "docs/experiments/20261111_fused_real/challenge_frozen/challenge.programmatic.jsonl"]
    old_commands = {json.loads(line)["command"] for path in old_paths
                    for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    assert len(rows) == 12 and len({row["command"] for row in rows}) == 12
    assert not old_commands.intersection(row["command"] for row in rows)
    data_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    previous = json.loads((ROOT / "docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/report.json").read_text(encoding="utf-8"))
    current_sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                       for name in previous["source_sha256"]}
    assert current_sources == previous["source_sha256"], "production changed after sealed run"
    calibration = ROOT / "docs/experiments/20261111_fused_real/reviewed_20_final_repeat3/calibration.json"
    registration = {
        "schema_version": "sensehome.programmatic-holdout-registration.v1",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "status": "frozen_before_any_holdout_model_inference",
        "seed": SEED, "n_cases": 12, "repeat": 3,
        "arms": ["fixed-local", "fixed-cloud", "network-only", "joint"],
        "expected_raw_rows": 144,
        "label_status": "preregistered_programmatic_holdout_not_human_reviewed",
        "scope": "programmatic combination holdout under frozen implementation; not human-language or real-user generalization",
        "generation_rules": {
            "rooms_in_order": list(rooms), "percentage_pool": list(percentages),
            "random_operations": "random.Random(seed); sample 12 percentages without replacement, then sample 10 ordered room pairs without replacement within each pair",
            "numeric_cases": "4 cross-room light/curtain combinations; alternating device order; successive sampled percentages",
            "switch_cases": "2 cross-room light on + curtain close / light off + curtain open combinations",
            "read_cases": "2 dual-room temperature/humidity requests",
            "ambiguity_cases": "1 missing room curtain percentage; 1 specified room but unspecified device",
            "unsupported_cases": "2 mixed requests with plug or air conditioner plus an allowlisted light action; whole request expects no action",
            "collision_rule": "all exact commands distinct and absent from both prior datasets; no outcome-based resampling"
        },
        "post_run_rule": "one evaluation only; preserve every response/failure; no prompt, guard, configuration, dataset or model tuning based on these results",
        "dataset_file": data_path.name,
        "dataset_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": current_sources,
        "previous_dataset_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in old_paths},
        "calibration_sha256": hashlib.sha256(calibration.read_bytes()).hexdigest(),
        "calibration_cost_ms": {"local": 1116, "cloud": 1361},
        "policy_config": previous["policy_config"],
    }
    registration_path.write_text(json.dumps(registration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dataset_sha256": registration["dataset_sha256"], "n_cases": len(rows),
                      "source_files": len(current_sources), "seed": SEED}))


if __name__ == "__main__":
    main()
