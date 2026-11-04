#!/usr/bin/env python3
"""Freeze, verify and preflight Qwen2.5-7B using the existing AutoDL runtime.

No SSH, credential handling or dependency installation. Download identity comes
from the live model API and is never inferred from small-model constants.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import autodl_small_recovery as small

REPO = "Qwen/Qwen2.5-7B-Instruct"
CHUNK = 192966697
ANCILLARY = (*small.ANCILLARY, "model.safetensors.index.json")
FROZEN_API = "sensehome-large-frozen-api.json"
DOWNLOAD_MANIFEST = "sensehome-large-download.json"


def revision_of(metadata):
    revision = metadata.get("sha")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError("API_REVISION_MISSING_OR_INVALID")
    if metadata.get("id", REPO) != REPO:
        raise RuntimeError("API_REPOSITORY_MISMATCH")
    return revision


def siblings_of(metadata):
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list) or not siblings:
        raise RuntimeError("API_FILE_METADATA_MISSING")
    result = {}
    for item in siblings:
        name = item.get("rfilename") if isinstance(item, dict) else None
        if not isinstance(name, str) or name in result:
            raise RuntimeError("API_FILE_METADATA_INVALID_OR_DUPLICATE")
        result[name] = item
    return result


def identity(item, name, *, require_lfs=False):
    if not isinstance(item, dict):
        raise RuntimeError("API_FILE_NOT_FOUND_" + name)
    lfs = item.get("lfs")
    if lfs is not None:
        digest = lfs.get("sha256")
        size = lfs.get("size")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("API_LFS_SHA256_MISSING_OR_INVALID_" + name)
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeError("API_LFS_SIZE_MISSING_OR_INVALID_" + name)
        return {"kind": "lfs_sha256", "sha256": digest, "size": size}
    if require_lfs:
        raise RuntimeError("SHARD_REQUIRES_EXPLICIT_LFS_METADATA_" + name)
    blob = item.get("blobId")
    if not isinstance(blob, str) or re.fullmatch(r"[0-9a-f]{40}", blob) is None:
        raise RuntimeError("API_GIT_BLOB_ID_MISSING_OR_INVALID_" + name)
    return {"kind": "git_blob_sha1", "blobId": blob}


def matches(path, expected):
    if not path.is_file():
        return False
    if expected["kind"] == "lfs_sha256":
        return path.stat().st_size == expected["size"] and small.sha256(path) == expected["sha256"]
    return small.git_blob_sha1(path.read_bytes()) == expected["blobId"]


def api_request(mirror, revision):
    with small.open_url(f"{mirror}/api/models/{REPO}/revision/{revision}?blobs=true") as response:
        return json.load(response)


def freeze_snapshot(model, mirror, evidence):
    frozen_file = model / FROZEN_API
    if frozen_file.is_file():
        original = json.loads(frozen_file.read_text(encoding="utf-8"))
        origin = "reused_existing_frozen_main_snapshot"
    else:
        original = api_request(mirror, "main")
        origin = "live_main_api"
    revision = revision_of(original)
    original_files = siblings_of(original)
    immutable = api_request(mirror, revision)
    if revision_of(immutable) != revision:
        raise RuntimeError("IMMUTABLE_API_REVISION_MISMATCH")
    immutable_files = siblings_of(immutable)
    # Compare all entries that will be allowed into the local model snapshot.
    allowed = set(ANCILLARY) | {name for name in original_files if name.endswith(".safetensors")}
    for name in allowed:
        before = identity(original_files.get(name), name, require_lfs=name.endswith(".safetensors"))
        after = identity(immutable_files.get(name), name, require_lfs=name.endswith(".safetensors"))
        if before != after:
            raise RuntimeError("IMMUTABLE_API_FILE_IDENTITY_MISMATCH_" + name)
    trace = []
    with small.open_url(f"{mirror}/{REPO}/resolve/{revision}/config.json", method="HEAD", header_trace=trace):
        pass
    commits = [hop.get("x-repo-commit") for hop in trace if hop.get("x-repo-commit")]
    small.write_json(evidence / "snapshot-header.json", {"requested_revision": revision, "header_trace": trace})
    if not commits or any(commit != revision for commit in commits):
        raise RuntimeError("IMMUTABLE_HEADER_REVISION_UNCONFIRMED")
    small.write_json(evidence / "main-frozen-api.json", original)
    small.write_json(evidence / "immutable-api.json", immutable)
    small.write_json(evidence / "snapshot-origin.json", {"source": origin, "repository": REPO, "revision": revision})
    if not frozen_file.exists():
        small.write_json(frozen_file, original)
    return revision, immutable_files


def fetch_auxiliary(model, mirror, revision, files):
    records = {}
    for name in ANCILLARY:
        expected = identity(files.get(name), name)
        destination = model / name
        reused = matches(destination, expected)
        if not reused:
            with small.open_url(f"{mirror}/{REPO}/resolve/{revision}/{name}") as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise RuntimeError("AUXILIARY_FILE_TOO_LARGE_" + name)
            temporary = destination.with_name(name + ".download")
            temporary.write_bytes(data)
            if not matches(temporary, expected):
                raise RuntimeError("AUXILIARY_FILE_IDENTITY_MISMATCH_" + name)
            temporary.replace(destination)
        records[name] = {**expected, "sha256": small.sha256(destination), "reused": reused}
    return records


def shards_from_index(model, files):
    index = json.loads((model / "model.safetensors.index.json").read_text(encoding="utf-8"))
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise RuntimeError("SAFETENSORS_INDEX_WEIGHT_MAP_MISSING")
    names = set(weight_map.values())
    if not names or any(not isinstance(name, str) for name in names):
        raise RuntimeError("SAFETENSORS_INDEX_SHARD_NAME_INVALID")
    parsed = []
    for name in names:
        match = re.fullmatch(r"model-(\d{5})-of-(\d{5})\.safetensors", name)
        if match is None:
            raise RuntimeError("UNSAFE_OR_UNSUPPORTED_SHARD_NAME")
        parsed.append((int(match[1]), int(match[2]), name))
        identity(files.get(name), name, require_lfs=True)
    parsed.sort()
    if [item[0] for item in parsed] != list(range(1, len(parsed) + 1)) or any(item[1] != len(parsed) for item in parsed):
        raise RuntimeError("SAFETENSORS_INDEX_SHARD_SEQUENCE_INVALID")
    return [item[2] for item in parsed]


def block_specs(name, size, parts):
    return [{"shard": name, "index": index, "start": start, "end": min(size, start + CHUNK) - 1,
             "part": str(parts / name / f"part_{index:04d}")}
            for index, start in enumerate(range(0, size, CHUNK))]


def assemble_shard(destination, specs, expected):
    assembled = destination.with_name(destination.name + ".assembling")
    digest = hashlib.sha256()
    total = 0
    with assembled.open("wb") as output:
        for spec in specs:  # Explicit order from the persisted numeric manifest.
            part = Path(spec["part"])
            required = spec["end"] - spec["start"] + 1
            if not part.is_file() or part.stat().st_size != required:
                raise RuntimeError("SHARD_BLOCK_SIZE_MISMATCH")
            with part.open("rb") as stream:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
                    total += len(block)
    if total != expected["size"] or digest.hexdigest() != expected["sha256"]:
        raise RuntimeError("SHARD_ASSEMBLED_LFS_HASH_MISMATCH_" + destination.name)
    assembled.replace(destination)


def recover_weights(args):
    if not 1 <= args.workers <= 4:
        raise ValueError("RANGE_WORKERS_MUST_BE_BETWEEN_ONE_AND_FOUR")
    model = Path(args.model_dir).resolve()
    parts = Path(args.parts_dir).resolve()
    evidence = Path(args.evidence_dir).resolve() / ("large_weights_" + small.timestamp())
    for folder in (model, parts, evidence):
        folder.mkdir(parents=True, exist_ok=True)
    status = {"status": "started", "repository": REPO, "scope": "download_verification_only"}
    small.write_json(evidence / "status.json", status)
    try:
        mirror = args.mirror.rstrip("/")
        revision, files = freeze_snapshot(model, mirror, evidence)
        records = fetch_auxiliary(model, mirror, revision, files)
        shards = shards_from_index(model, files)
        identities = {name: identity(files[name], name, require_lfs=True) for name in shards}
        total = sum(item["size"] for item in identities.values())
        specs = {name: block_specs(name, identities[name]["size"], parts) for name in shards}
        verified = {name for name in shards if matches(model / name, identities[name])}
        # Reserve two complete weight copies (blocks + assembled shards). On
        # resume, account only for files already occupying that reserved space.
        present = sum(identities[name]["size"] for name in verified)
        for name in shards:
            for spec in specs[name]:
                part = Path(spec["part"])
                if part.is_file():
                    present += min(part.stat().st_size, spec["end"] - spec["start"] + 1)
        needed_free = max(0, total * 2 - present)
        free = shutil.disk_usage(model).free
        disk = {"total_weight_bytes": total, "reserved_total_bytes": total * 2,
                "already_present_reserved_bytes": present, "required_free_bytes": needed_free, "available_free_bytes": free}
        small.write_json(evidence / "disk-reservation.json", disk)
        if free < needed_free:
            raise RuntimeError("INSUFFICIENT_SPACE_FOR_TWO_WEIGHT_COPIES")
        download_manifest = {"repository": REPO, "revision": revision, "workers": args.workers,
                             "chunk_bytes": CHUNK, "total_weight_bytes": total,
                             "shard_order": shards, "identities": identities, "blocks": specs}
        small.write_json(model / DOWNLOAD_MANIFEST, download_manifest)
        small.write_json(evidence / "download-manifest.json", download_manifest)
        operations = []
        tasks = []
        for name in shards:
            if name in verified:
                operations.append({"shard": name, "operation": "reused_full_lfs_verified_shard"})
                continue
            for spec in specs[name]:
                part = Path(spec["part"])
                part.parent.mkdir(parents=True, exist_ok=True)
                if part.is_file() and part.stat().st_size == spec["end"] - spec["start"] + 1:
                    operations.append({"shard": name, "block": spec["index"], "operation": "reused_size_matched_block"})
                else:
                    tasks.append(spec)
        def transfer(spec):
            name = spec["shard"]
            url = f"{mirror}/{REPO}/resolve/{revision}/{name}"
            result = small.download_part(url, Path(spec["part"]), spec["start"], spec["end"], identities[name]["size"])
            print(f"Verified range transfer {name} block {spec['index']}", flush=True)
            return {"shard": name, "block": spec["index"], "operation": "downloaded_exact_range", **result}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(transfer, spec) for spec in tasks]
            try:
                for future in as_completed(futures):
                    operations.append(future.result())
                    small.write_json(evidence / "block-operations.json", operations)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        for name in shards:
            if name not in verified:
                assemble_shard(model / name, specs[name], identities[name])
            records[name] = {**identities[name], "reused": name in verified}
        manifest = {"repository": REPO, "revision": revision, "verified_at": small.timestamp(),
                    "shard_order": shards, "file_identities": {name: identity(files[name], name, require_lfs=name in shards) for name in records},
                    "files": {name: small.sha256(model / name) for name in records}}
        small.write_json(model / "sensehome-verified-snapshot.json", manifest)
        small.write_json(evidence / "verified-snapshot.json", manifest)
        status.update(status="completed", revision=revision, total_weight_bytes=total)
        small.write_json(evidence / "status.json", status)
        print(json.dumps({"large_weights_verified": True, "revision": revision, "evidence": str(evidence)}))
    except BaseException as exc:
        status.update(status="failed", error_class=type(exc).__name__, error=str(exc))
        small.write_json(evidence / "status.json", status)
        raise


def verify_large_snapshot(model, manifest):
    frozen = json.loads((model / FROZEN_API).read_text(encoding="utf-8"))
    revision = revision_of(frozen)
    files = siblings_of(frozen)
    if manifest.get("repository") != REPO or manifest.get("revision") != revision:
        raise RuntimeError("LARGE_SNAPSHOT_IDENTITY_MISMATCH")
    shards = shards_from_index(model, files)
    expected_names = set(ANCILLARY) | set(shards)
    if set(manifest.get("files", {})) != expected_names or manifest.get("shard_order") != shards:
        raise RuntimeError("LARGE_SNAPSHOT_FILE_SET_MISMATCH")
    for name in sorted(expected_names):
        expected = identity(files.get(name), name, require_lfs=name in shards)
        if not matches(model / name, expected) or small.sha256(model / name) != manifest["files"][name]:
            raise RuntimeError("LARGE_SNAPSHOT_FILE_VERIFICATION_FAILED_" + name)
    return revision


def assert_small_stopped(args):
    evidence = Path(args.evidence_dir).resolve() / ("large_start_gate_" + small.timestamp())
    evidence.mkdir(parents=True)
    for port in (8001, 18092):
        small.port_available(port)
    command = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"]
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    (evidence / "gpu-processes-before-large.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
    active = [line for line in result.stdout.splitlines() if line.strip() and line.split(",", 1)[0].strip().isdigit()]
    small.write_json(evidence / "status.json", {"small_ports_free": True, "active_gpu_processes": len(active),
                                              "large_start_allowed": not active})
    if active:
        raise RuntimeError("STOP_SMALL_MODEL_AND_OTHER_GPU_COMPUTE_BEFORE_LARGE")


def preflight(args):
    assert_small_stopped(args)
    return small.preflight(args, snapshot_validator=verify_large_snapshot, served_model="sensehome-large",
                           upstream_port=8002, wrapper_port=18093, tier="cloud", model_size="large")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("weights", "preflight"))
    parser.add_argument("--model-dir", default="/root/autodl-tmp/models/large")
    parser.add_argument("--parts-dir", default="/root/autodl-tmp/models/large_parts")
    parser.add_argument("--adapter-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--legacy-adapter-dir", default="/root/autodl-tmp/sensehome-adapter")
    parser.add_argument("--evidence-dir", default="/root/autodl-tmp/sensehome-evidence")
    parser.add_argument("--mirror", default="https://hf-mirror.com")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--startup-timeout", type=int, default=1200)
    parser.add_argument("--gateway-capture-script")
    parser.add_argument("--gateway-capture-inject-lan-ok", action="store_true",
                        help="Required with a cloud gateway capture; passes the explicit injected LAN fixture")
    args = parser.parse_args()
    if args.repeat < 1 or args.startup_timeout < 1:
        parser.error("repeat and startup-timeout must be positive")
    if args.operation == "weights":
        recover_weights(args)
    else:
        preflight(args)


if __name__ == "__main__":
    main()
