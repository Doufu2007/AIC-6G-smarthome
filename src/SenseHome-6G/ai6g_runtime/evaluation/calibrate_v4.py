"""Calibrate real model capabilities on a frozen synthetic set; no device IO."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai6g_runtime.protocol import classify_task, validate_plan
from ai6g_runtime.models import OpenAICompatibleBackend
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.evaluator import semantic_score_row
from ai6g_runtime.benchmark import load_tasks


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _fingerprint(manifest):
    return _sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf8"))


def _source_hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: _sha256((root / name).read_bytes()) for name in (
        "evaluation/calibrate_v4.py", "models.py", "protocol.py", "executor.py",
        "evaluator.py", "benchmark.py")}


def _endpoint_identity(endpoint):
    # Keep credentials and query tokens out of the readable manifest. Hashing the
    # full address still detects changes to them instead of silently merging runs.
    parts = urlsplit(endpoint)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("explicit HTTP(S) model endpoint required")
    if ":" in host:
        host = "[" + host + "]"
    netloc = host + (":" + str(parts.port) if parts.port is not None else "")
    return {"endpoint": urlunsplit((parts.scheme, netloc, parts.path, "", "")),
            "endpoint_sha256": _sha256(endpoint.encode("utf8"))}


def _validate_tasks(tasks):
    if not tasks:
        raise ValueError("calibration task set is empty")
    ids = set()
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("each calibration task requires a nonempty string id")
        if task_id in ids:
            raise ValueError("duplicate calibration task id: " + task_id)
        if not isinstance(task.get("gold"), dict):
            raise ValueError("each calibration task requires a gold object")
        ids.add(task_id)
    return ids


def _bind_manifest(manifest_path, raw_path, out, manifest):
    envelope = {"fingerprint": _fingerprint(manifest), "manifest": manifest}
    if manifest_path.exists():
        try:
            recorded = json.loads(manifest_path.read_text(encoding="utf8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("corrupt calibration manifest; use a new output path") from exc
        if recorded != envelope:
            raise ValueError("calibration manifest fingerprint changed; use a new output path")
    else:
        # Legacy rows belong to their original source, never the current code.
        if raw_path.exists() or out.exists():
            raise ValueError("unbound legacy calibration requires its original manifest; use a new output path")
        with manifest_path.open("x", encoding="utf8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    if out.exists() and not raw_path.exists():
        raise ValueError("calibration journal is missing; use a new output path")
    return envelope["fingerprint"]


def _truncated_json(text, error):
    if error.pos >= len(text.rstrip()) or error.msg.startswith("Unterminated string"):
        return True
    remainder = text[error.pos:]
    if remainder and any(token.startswith(remainder) for token in ("true", "false", "null")):
        return True
    number = re.search(r"(?:^|[\s\[,:])(-|-?(?:0|[1-9][0-9]*)\.|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?[eE][+-]?)\Z", text)
    return number is not None and number.start(1) <= error.pos


def _load_journal(raw_path, task_ids, backends, task_sha, fingerprint):
    """Validate all completed rows before repairing an interrupted final write."""
    if not raw_path.exists():
        return [], False
    data = raw_path.read_bytes()
    lines = data.splitlines(keepends=True)
    rows, done = [], set()
    offset, repair_at = 0, None
    for index, line in enumerate(lines):
        last_unterminated = index == len(lines) - 1 and not line.endswith((b"\n", b"\r"))
        try:
            decoded = line.decode("utf8")
        except UnicodeDecodeError as exc:
            if last_unterminated and exc.reason == "unexpected end of data" and line.lstrip().startswith(b"{"):
                repair_at = offset
                break
            raise ValueError("corrupt calibration journal UTF-8") from exc
        try:
            row = json.loads(decoded)
        except json.JSONDecodeError as exc:
            truncated = _truncated_json(decoded, exc)
            if last_unterminated and truncated and decoded.lstrip().startswith("{"):
                repair_at = offset
                break
            raise ValueError("corrupt calibration journal JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("calibration journal row must be an object")
        key = (row.get("backend"), row.get("task_id"))
        if not all(isinstance(value, str) for value in key):
            raise ValueError("invalid calibration journal request key")
        if key in done:
            raise ValueError("duplicate calibration journal request key")
        if key[0] not in backends or key[1] not in task_ids:
            raise ValueError("calibration journal request is outside the manifest")
        if row.get("task_source_sha256") != task_sha:
            raise ValueError("calibration journal task source differs from manifest")
        if row.get("calibration_fingerprint") != fingerprint:
            raise ValueError("calibration journal row fingerprint differs from manifest")
        if "skipped" not in row and row.get("model") != backends[key[0]].model:
            raise ValueError("calibration journal model differs from manifest")
        rows.append(row)
        done.add(key)
        offset += len(line)
    if repair_at is not None:
        with raw_path.open("r+b") as handle:
            handle.truncate(repair_at)
            handle.flush()
            os.fsync(handle.fileno())
        data = data[:repair_at]
    return rows, bool(data and not data.endswith((b"\n", b"\r")))


def calibrate(tasks_path, out_path):
    tasks = load_tasks(tasks_path)
    task_ids = _validate_tasks(tasks)
    task_sha = _sha256(Path(tasks_path).read_bytes())
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_path = out.with_suffix(".rows.jsonl")
    manifest_path = out.with_suffix(".manifest.json")
    backends, identities = {}, {}
    for name, prefix in (("local-model", "LOCAL"), ("cloud-model", "CLOUD")):
        url = os.getenv("AI6G_" + prefix + "_URL")
        model = os.getenv("AI6G_" + prefix + "_MODEL")
        if not url or not model:
            raise ValueError("explicit real model URL and model name required")
        backend = OpenAICompatibleBackend(name, url, model, max_tokens=384, seed=20261104)
        backends[name] = backend
        identities[name] = {**_endpoint_identity(backend.endpoint), "model": backend.model,
                            "seed": backend.seed, "max_tokens": backend.max_tokens,
                            "prompt_sha256": _sha256(backend.system_prompt.encode("utf8")),
                            "timeout_s": 12, "temperature": 0,
                            "response_format": {"type": "json_object"}}
    manifest = {"schema": "ai6g.family_calibration_manifest.v1", "task_source_sha256": task_sha,
                "backends": identities, "sources": _source_hashes()}
    fingerprint = _bind_manifest(manifest_path, raw_path, out, manifest)
    rows, needs_separator = _load_journal(raw_path, task_ids, backends, task_sha, fingerprint)
    done = {(row["backend"], row["task_id"]) for row in rows}
    for name, backend in backends.items():
        for task_row in tasks:
            if (name, task_row["id"]) in done:
                continue
            task = classify_task(task_row["text"])
            row = {"backend": name, "task_id": task_row["id"], "family": task.family,
                   "task_source_sha256": task_sha, "calibration_fingerprint": fingerprint}
            if task.privacy == "strict":
                row.update(skipped="remote_backend_outside_privacy_domain", model_real=False)
            else:
                store = DeviceStore()
                initial = store.snapshot()
                started = time.perf_counter()
                result = backend.infer(task, timeout_s=12)
                raw = {"ok": False, "execution": {"ok": False}, "initial_state": initial,
                       "state": store.snapshot(), "plan": None, "result_kind": "failure",
                       "raw_response": result.raw_response, "model_real": result.model_real,
                       "error_code": result.error}
                if result.ok:
                    try:
                        plan = validate_plan(result.payload, task)
                        execution = store.apply(plan, task_row["id"])
                        raw.update(ok=execution.ok, execution=asdict(execution),
                                   plan={"actions": [asdict(action) for action in plan.actions]},
                                   state=store.snapshot(), result_kind=plan.result_kind)
                    except ValueError as exc:
                        raw["error_code"] = "invalid_model_output: " + str(exc)
                row.update(model=backend.model, elapsed_ms=(time.perf_counter() - started) * 1000,
                           raw=raw, model_real=result.model_real,
                           **semantic_score_row(raw, task_row["gold"]))
            with raw_path.open("ab") as handle:
                if needs_separator:
                    handle.write(b"\n")
                handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf8"))
                handle.flush()
                os.fsync(handle.fileno())
            needs_separator = False
            rows.append(row)
            done.add((name, task_row["id"]))
            print(name, task_row["id"], row.get("semantic_success", row.get("skipped")), flush=True)
    source_sha = _sha256(raw_path.read_bytes())
    summaries = {}
    for name in backends:
        eligible = [row for row in rows if row["backend"] == name and "skipped" not in row]
        groups = defaultdict(list)
        for row in eligible:
            groups[row["family"]].append(row)
        summaries[name] = {
            "quality_score": sum(bool(row["semantic_success"]) for row in eligible) / len(eligible) if eligible else None,
            "source_sha256": source_sha,
            "quality_by_family": {key: sum(bool(row["semantic_success"]) for row in group) / len(group)
                                  for key, group in groups.items() if len(group) >= 3},
            "family_samples": {key: len(group) for key, group in groups.items()},
            "physical_trust": "external_AutoDL", "real_rows": sum(row["model_real"] for row in eligible),
            "n": len(eligible)}
    payload = {"schema": "ai6g.family_calibration.v4", "task_source_sha256": task_sha,
               "source_sha256": source_sha, "calibration_fingerprint": fingerprint,
               "manifest": manifest, "backends": summaries,
               "notes": ["Synthetic protocol suite, not real users.",
                         "Privacy-strict examples are not sent to external model endpoints.",
                         "Family scores are descriptive calibration estimates, not guaranteed accuracy.",
                         "Quality is unknown (null) when there are no eligible samples."]}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summaries), flush=True)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    calibrate(args.tasks, args.out)
