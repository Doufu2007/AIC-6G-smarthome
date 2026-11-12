#!/usr/bin/env python3
"""Build the correct adapter layout locally, without network or credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(output, include_large=False):
    repo = Path(__file__).resolve().parents[2]
    net = repo / "SenseHome-6G/net_offload"
    sources = {
        "eval_model.py": net / "eval_model.py",
        "backends/openai_backend.py": net / "backends/openai_backend.py",
        "backends/target_contract.py": net / 'target_contract.py',
        "backends/device_actions.py": repo / "openvela_smart_home/dashboard/device_actions.py",
        "backends/eval_commands.jsonl": net / "backends/eval_commands.jsonl",
        "autodl_small_recovery.py": Path(__file__).with_name("autodl_small_recovery.py"),
    }
    if include_large:
        sources["autodl_large_recovery.py"] = Path(__file__).with_name("autodl_large_recovery.py")
    data = {name: path.read_bytes() for name, path in sources.items()}
    rows = [json.loads(line) for line in data["backends/eval_commands.jsonl"].decode("utf-8").splitlines() if line.strip()]
    if not rows or any(row.get("human_reviewed") is not False for row in rows):
        raise ValueError("Recovery preflight bundle requires original draft labels, all human_reviewed=false")
    for name, content in data.items():
        if name.endswith(".py"):
            compile(content, name, "exec")
    manifest = {"scope": "draft_small_model_pipeline_preflight_only", "human_reviewed": False,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in data.items()}}
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse clobbering an existing bundle: source revisions must be explicit.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in data.items():
            archive.writestr(name, content)
        archive.writestr("bundle-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"bundle": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "n_files": len(data) + 1, "label_status": "draft_labels_not_human_verified"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--include-large", action="store_true")
    args = parser.parse_args()
    build(args.output, args.include_large)
