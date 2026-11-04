#!/usr/bin/env python3
"""Promote a captured real-model run after its labels are frozen.

This does not rerun inference. It verifies that the frozen labels have the
same commands and expected actions as the labels used for the captured run,
then emits a formally labelled report with explicit provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load_jsonl(path: Path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 20 or len({row.get("id") for row in rows}) != 20:
        raise ValueError("EXPECTED_20_UNIQUE_LABELS")
    return {row["id"]: row for row in rows}


def promote(captured: Path, frozen: Path, output: Path, metadata: Path):
    report = json.loads(captured.read_text(encoding="utf-8"))
    frozen_rows = load_jsonl(frozen)
    if report.get("scope") != "model_action_planning_only_no_device_execution":
        raise ValueError("UNEXPECTED_CAPTURE_SCOPE")
    for row in report.get("rows", []):
        label = frozen_rows.get(row.get("id"))
        if label is None or row.get("command") != label.get("command") or row.get("expected_actions") != label.get("expected_actions"):
            raise ValueError("CAPTURED_LABELS_DO_NOT_MATCH_FROZEN_LABELS")
    if not all(label.get("human_reviewed") is True for label in frozen_rows.values()):
        raise ValueError("FROZEN_LABELS_NOT_MARKED_HUMAN_REVIEWED")
    refusal_ids = {"A01", "A02", "A04"}
    refusal_correct = sum(1 for row in report["rows"] if row["id"] in refusal_ids and row.get("http_status") == 422 and not row.get("actual_actions"))
    report["label_status"] = "human_reviewed"
    report["formal_accuracy_evidence"] = True
    report["evaluation_kind"] = "post_freeze_relabel_of_captured_real_model_responses"
    report["inference_rerun_after_freeze"] = False
    report["dataset_sha256"] = hashlib.sha256(frozen.read_bytes()).hexdigest()
    report["label_freeze_metadata"] = json.loads(metadata.read_text(encoding="utf-8"))
    report["refusal_summary"] = {
        "expected_refusal_ids": sorted(refusal_ids),
        "refusal_correct": refusal_correct,
        "refusal_total": len(refusal_ids),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--captured", required=True)
    parser.add_argument("--frozen", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = promote(Path(args.captured), Path(args.frozen), Path(args.out), Path(args.metadata))
    print(json.dumps({"label_status": result["label_status"], "formal_accuracy_evidence": result["formal_accuracy_evidence"], **result["summary"], "refusal_summary": result["refusal_summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
