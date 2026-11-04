#!/usr/bin/env python3
"""Explicit, resumable small-model recovery. No SSH, credentials or installation.

Run on AutoDL with its existing vLLM Python environment. The weights command
uses the network; preflight uses only loopback. Logs are evidence of actual
execution, never supplied results.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse, urlencode, urlsplit, urlunsplit

REPO = "Qwen/Qwen2.5-1.5B-Instruct"
REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
TOTAL = 3087467144
CHUNK = 192966697
WEIGHT_SHA256 = "dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee"
if len(WEIGHT_SHA256) != 64 or any(character not in "0123456789abcdef" for character in WEIGHT_SHA256):
    raise RuntimeError("INVALID_PINNED_WEIGHT_SHA256")
ANCILLARY = ("config.json", "generation_config.json", "merges.txt", "tokenizer.json",
             "tokenizer_config.json", "vocab.json")
USER_AGENT = "Mozilla/5.0 (compatible; SenseHomeRecovery/1.0)"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


class RevisionTraceRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def redirect_request(self, request, fp, code, message, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise RuntimeError("UNSAFE_MIRROR_REDIRECT")
        if self.trace is not None:
            self.trace.append({"status": code, "x-repo-commit": headers.get("x-repo-commit")})
        return super().redirect_request(request, fp, code, message, headers, newurl)


def open_url(url, *, method="GET", headers=None, header_trace=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("MIRROR_REQUIRES_HTTPS_WITHOUT_CREDENTIALS")
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, method=method, headers=request_headers)
    # Hub resolve endpoints can put x-repo-commit on an intermediate 302/307;
    # retain only that selected header, never signed CDN redirect URLs.
    opener = urllib.request.build_opener(RevisionTraceRedirect(header_trace))
    response = opener.open(request, timeout=90)
    if header_trace is not None:
        header_trace.append({"status": response.status, "x-repo-commit": response.headers.get("x-repo-commit")})
    return response


def snapshot_metadata(mirror, evidence):
    """Independently verify the pinned revision and LFS identity before reuse."""
    api = f"{mirror}/api/models/{REPO}/revision/{REVISION}?blobs=true"
    with open_url(api) as response:
        metadata = json.load(response)
    write_json(evidence / "snapshot-api.json", metadata)
    if metadata.get("sha") != REVISION:
        raise RuntimeError("SNAPSHOT_REVISION_MISMATCH")
    files = {item["rfilename"]: item for item in metadata.get("siblings", [])}
    weight = files.get("model.safetensors", {})
    lfs = weight.get("lfs", {})
    if lfs.get("sha256") != WEIGHT_SHA256 or lfs.get("size") != TOTAL:
        raise RuntimeError("SNAPSHOT_LFS_IDENTITY_MISMATCH")
    # HEAD the immutable config URL: do not silently trust the earlier main HEAD.
    config_url = f"{mirror}/{REPO}/resolve/{REVISION}/config.json"
    trace = []
    with open_url(config_url, method="HEAD", header_trace=trace) as response:
        observed = [hop["x-repo-commit"] for hop in trace if hop.get("x-repo-commit")]
        head = {"requested_revision": REVISION, "observed_repo_commits": observed, "header_trace": trace,
                "status": response.status, "checked_at": timestamp()}
    write_json(evidence / "snapshot-header.json", head)
    if not observed or any(commit != REVISION for commit in observed):
        raise RuntimeError("SNAPSHOT_HEADER_REVISION_UNCONFIRMED")
    return files


def git_blob_sha1(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def download_part(url, part, start, end, total, attempts=3):
    """Retry transient transfers only; keep an original part until replacement."""
    partial = part.with_name(part.name + ".download")
    expected = end - start + 1
    prior = part.with_name(part.name + ".download")
    part_bytes = part.stat().st_size if part.is_file() else 0
    prior_bytes = prior.stat().st_size if prior.is_file() else 0
    if 0 < part_bytes < expected:
        prefix_bytes, prefix_source = part_bytes, part
    elif 0 < prior_bytes < expected:
        # Resume an interrupted working file in place. The original part,
        # when present but invalid, remains untouched.
        prefix_bytes, prefix_source = prior_bytes, prior
    else:
        prefix_bytes, prefix_source = 0, None
    if prefix_source == part:
        # An interrupted exact-range file is a candidate prefix, not a trusted
        # object. Keep the original, append into a separate working file, and
        # still require the full assembled LFS hash before serving anything.
        shutil.copyfile(part, partial)
    elif prefix_source == prior:
        # Keep an existing .download prefix. It is still untrusted until the
        # exact range and final full-file hash pass.
        pass
    else:
        partial.write_bytes(b"")
    for attempt in range(1, attempts + 1):
        try:
            offset = partial.stat().st_size
            if offset == expected:
                partial.replace(part)
                return {"attempts": attempt - 1, "resumed_prefix_bytes": prefix_bytes}
            if not 0 <= offset < expected:
                raise RuntimeError("PART_WORKING_FILE_OVERSIZED_" + part.name)
            range_start = start + offset
            split = urlsplit(url)
            cache_query = urlencode({"download": "true", "recovery_range": f"{range_start}-{end}",
                                     "recovery_nonce": str(time.time_ns())})
            request_url = urlunsplit((split.scheme, split.netloc, split.path,
                                     split.query + ("&" if split.query else "") + cache_query, split.fragment))
            headers = {"Range": f"bytes={range_start}-{end}", "Accept-Encoding": "identity"}
            with open_url(request_url, headers=headers) as response:
                if response.status != 206 or response.headers.get("Content-Range", "") != f"bytes {range_start}-{end}/{total}":
                    raise RuntimeError("RANGE_NOT_HONORED_" + part.name)
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding not in ("", "identity"):
                    raise RuntimeError("RANGE_RESPONSE_MUST_NOT_BE_COMPRESSED")
                written = offset
                deadline = time.monotonic() + 900
                with partial.open("ab") as output:
                    while written <= expected:
                        if time.monotonic() > deadline:
                            raise TimeoutError("PART_TRANSFER_EXCEEDED_900_SECONDS")
                        block = response.read(min(8 * 1024 * 1024, expected + 1 - written))
                        if not block:
                            break
                        written += len(block)
                        output.write(block)
                if written > expected:
                    raise RuntimeError("RANGE_RESPONSE_EXCEEDS_EXPECTED_BYTES")
                if written < expected:
                    raise http.client.IncompleteRead(b"", expected - written)
            partial.replace(part)
            return {"attempts": attempt, "resumed_prefix_bytes": prefix_bytes}
        except urllib.error.HTTPError as exc:
            if attempt == attempts or exc.code not in (408, 429, 500, 502, 503, 504):
                raise
        except (OSError, http.client.IncompleteRead):
            if attempt == attempts:
                raise
        time.sleep(min(2 ** attempt, 8))


def recover_weights(args):
    model = Path(args.model_dir).resolve()
    parts = Path(args.parts_dir).resolve()
    evidence = Path(args.evidence_dir).resolve() / ("weights_" + timestamp())
    for folder in (model, parts, evidence):
        folder.mkdir(parents=True, exist_ok=True)
    files = snapshot_metadata(args.mirror.rstrip("/"), evidence)
    operations = []
    weight_path = model / "model.safetensors"
    if weight_path.is_file() and weight_path.stat().st_size == TOTAL and sha256(weight_path) == WEIGHT_SHA256:
        operations.append({"file": "model.safetensors", "operation": "reused_verified_complete_weight"})
    else:
        for index in range(16):
            start = index * CHUNK
            end = min(TOTAL, start + CHUNK) - 1
            expected = end - start + 1
            part = parts / f"part_{index:02d}"
            if part.is_file() and part.stat().st_size == expected:
                operations.append({"file": part.name, "operation": "reused_size_matched_part", "bytes": expected})
                continue
            # Old logs or .download files are never concatenated. A bad-size
            # original part remains intact until a complete replacement exists.
            url = f"{args.mirror.rstrip('/')}/{REPO}/resolve/{REVISION}/model.safetensors"
            print(f"Downloading only {part.name}, bytes {start}-{end}", flush=True)
            transfer = download_part(url, part, start, end, TOTAL)
            operations.append({"file": part.name, "operation": "downloaded_exact_range", "bytes": expected,
                               **transfer})
        if shutil.disk_usage(model).free < TOTAL:
            raise RuntimeError("INSUFFICIENT_SPACE_FOR_VERIFIED_ASSEMBLY")
        assembled = model / "model.safetensors.assembling"
        digest = hashlib.sha256()
        with assembled.open("wb") as output:
            # Explicit numeric order, never part_* (which includes .log).
            for index in range(16):
                part = parts / f"part_{index:02d}"
                expected = min(CHUNK, TOTAL - index * CHUNK)
                if part.stat().st_size != expected:
                    raise RuntimeError("PART_CHANGED_BEFORE_ASSEMBLY")
                with part.open("rb") as stream:
                    for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                        digest.update(block)
                        output.write(block)
        if assembled.stat().st_size != TOTAL or digest.hexdigest() != WEIGHT_SHA256:
            raise RuntimeError("ASSEMBLED_WEIGHT_HASH_MISMATCH_DO_NOT_SERVE")
        assembled.replace(weight_path)
        operations.append({"file": weight_path.name, "operation": "assembled_and_sha256_verified"})
    for name in ANCILLARY:
        item = files.get(name, {})
        blob = item.get("blobId")
        if not blob:
            raise RuntimeError("MISSING_SNAPSHOT_BLOB_ID_" + name)
        destination = model / name
        valid = destination.is_file() and git_blob_sha1(destination.read_bytes()) == blob
        if not valid:
            url = f"{args.mirror.rstrip('/')}/{REPO}/resolve/{REVISION}/{name}"
            with open_url(url) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024 or git_blob_sha1(data) != blob:
                raise RuntimeError("CONFIG_BLOB_MISMATCH_" + name)
            temporary = destination.with_name(name + ".download")
            temporary.write_bytes(data)
            temporary.replace(destination)
        operations.append({"file": name, "operation": "reused_verified_blob" if valid else "downloaded_verified_blob"})
    manifest = {"repository": REPO, "revision": REVISION, "total_bytes": TOTAL,
                "weight_sha256": WEIGHT_SHA256, "verified_at": timestamp(),
                "files": {name: sha256(model / name) for name in (*ANCILLARY, "model.safetensors")},
                "operations": operations}
    write_json(model / "sensehome-verified-snapshot.json", manifest)
    write_json(evidence / "verified-snapshot.json", manifest)
    print(json.dumps({"weights_verified": True, "revision": REVISION, "evidence": str(evidence)}))


def save_command(command, destination):
    with Path(destination).open("wb") as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=120)


def port_available(port):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def http_json(url, body=None, timeout=90, error_evidence=None):
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("PREFLIGHT_HTTP_MUST_USE_LOOPBACK")
    raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"})
    # A proxy inherited from the rental image must never receive commands.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if error_evidence is not None:
            content = exc.read(262144)
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeError):
                payload = {"error": "NON_JSON_HTTP_ERROR", "response_bytes": len(content)}
            write_json(error_evidence, {"http_status": exc.code, "body": payload})
        raise


def preflight(args, *, snapshot_validator=None, served_model="sensehome-small", upstream_port=8001,
              wrapper_port=18092, tier="gateway", model_size="small"):
    if os.name != "posix":
        raise RuntimeError("PREFLIGHT_REQUIRES_AUTODL_LINUX")
    root = Path(args.adapter_dir).resolve()
    model = Path(args.model_dir).resolve()
    evidence = Path(args.evidence_dir).resolve() / (model_size + "_preflight_" + timestamp())
    evidence.mkdir(parents=True)
    status = {"status": "started", "scope": "draft_" + model_size + "_model_pipeline_preflight_only",
              "label_status": "draft_labels_not_human_verified", "started_at": timestamp()}
    write_json(evidence / "status.json", status)
    processes = []
    logs = []
    try:
        manifest = json.loads((model / "sensehome-verified-snapshot.json").read_text(encoding="utf-8"))
        if snapshot_validator is None:
            if manifest.get("revision") != REVISION or manifest.get("weight_sha256") != WEIGHT_SHA256:
                raise RuntimeError("RUN_WEIGHTS_VERIFICATION_FIRST")
            for name in (*ANCILLARY, "model.safetensors"):
                if sha256(model / name) != manifest["files"][name]:
                    raise RuntimeError("VERIFIED_MODEL_FILE_CHANGED_" + name)
            revision = REVISION
        else:
            revision = snapshot_validator(model, manifest)
        required = ("eval_model.py", "backends/openai_backend.py", "backends/device_actions.py", "backends/eval_commands.jsonl")
        for name in required:
            if not (root / name).is_file():
                raise RuntimeError("MISSING_CORRECT_LAYOUT_" + name)
        # Preserve, but make the earlier machine-created reviewed copies
        # unavailable to this run. No row is promoted to human_reviewed=true.
        quarantine = evidence / "quarantine_untrusted_reviewed"
        bases = {root, root / "backends", Path(args.legacy_adapter_dir).resolve(),
                 Path(args.legacy_adapter_dir).resolve() / "backends"}
        for base in sorted(bases):
            for candidate in base.glob("*reviewed*.jsonl"):
                quarantine.mkdir(exist_ok=True)
                prefix = hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:8]
                candidate.rename(quarantine / (prefix + "__" + candidate.name))
        cases = root / "backends/eval_commands.jsonl"
        rows = [json.loads(line) for line in cases.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows or any(row.get("human_reviewed") is not False for row in rows):
            raise RuntimeError("PREFLIGHT_REQUIRES_UNMODIFIED_DRAFT_LABELS")
        port_available(upstream_port)
        port_available(wrapper_port)
        save_command(["nvidia-smi"], evidence / "nvidia-smi-before.txt")
        save_command([sys.executable, "-m", "pip", "freeze"], evidence / "runtime.lock.txt")
        versions = {"python": sys.version, "python_executable": sys.executable,
                    "vllm": importlib.metadata.version("vllm"), "torch": importlib.metadata.version("torch")}
        write_json(evidence / "versions.json", versions)
        write_json(evidence / "source-sha256.json", {name: sha256(root / name) for name in required})
        shutil.copy2(model / "sensehome-verified-snapshot.json", evidence / "model-snapshot.json")
        shutil.copy2(cases, evidence / "eval_commands.draft.jsonl")
        command = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(model),
                   "--served-model-name", served_model, "--host", "127.0.0.1", "--port", str(upstream_port),
                   "--dtype", "auto", "--max-model-len", "4096", "--gpu-memory-utilization", "0.9", "--seed", "20261103"]
        write_json(evidence / "vllm-command.json", command)
        runtime_environment = os.environ.copy()
        runtime_environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                                   VLLM_NO_USAGE_STATS="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
        log = (evidence / "vllm.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                   env=runtime_environment, start_new_session=True)
        processes.append(process)
        ready = False
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("VLLM_EXITED_SEE_VLLM_LOG")
            try:
                response = http_json(f"http://127.0.0.1:{upstream_port}/v1/models", timeout=2)
                ready = any(item.get("id") == served_model for item in response.get("data", []))
                if ready:
                    write_json(evidence / "upstream-models.json", response)
                    break
            except (OSError, ValueError):
                pass
            time.sleep(2)
        if not ready:
            raise RuntimeError("VLLM_STARTUP_TIMEOUT")
        gpu_info = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], text=True).strip()
        environment = runtime_environment.copy()
        environment.update(SENSEHOME_UPSTREAM_URL=f"http://127.0.0.1:{upstream_port}/v1", SENSEHOME_MODEL=served_model,
                           SENSEHOME_TIER=tier, SENSEHOME_WRAPPER_HOST="127.0.0.1", SENSEHOME_WRAPPER_PORT=str(wrapper_port),
                           SENSEHOME_UPSTREAM_TIMEOUT_S="60", SENSEHOME_GPU_INFO=gpu_info,
                           SENSEHOME_MODEL_REVISION=revision)
        # Local-only fresh vLLM has no API key; ignore inherited unrelated keys.
        environment.pop("SENSEHOME_API_KEY", None)
        environment.pop("SENSEHOME_WRAPPER_TOKEN", None)
        log = (evidence / "adapter.log").open("wb")
        logs.append(log)
        wrapper = subprocess.Popen([sys.executable, str(root / "backends/openai_backend.py")],
                                   stdout=log, stderr=subprocess.STDOUT, env=environment, cwd=root, start_new_session=True)
        processes.append(wrapper)
        health = None
        for _ in range(30):
            if wrapper.poll() is not None:
                raise RuntimeError("WRAPPER_EXITED_SEE_ADAPTER_LOG")
            try:
                health = http_json(f"http://127.0.0.1:{wrapper_port}/health", timeout=3)
                if health.get("ready") is True:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        if not health or health.get("ready") is not True:
            raise RuntimeError("WRAPPER_NOT_READY")
        write_json(evidence / "health.json", health)
        write_json(evidence / "probe.json", http_json(f"http://127.0.0.1:{wrapper_port}/probe"))
        warmup = http_json(f"http://127.0.0.1:{wrapper_port}/infer", {"command": "打开客厅的灯", "context": {}},
                           error_evidence=evidence / "warmup-error.json")
        write_json(evidence / "warmup-infer.json", warmup)
        if warmup.get("source") != "real-model" or warmup.get("model_revision") != revision:
            raise RuntimeError("WARMUP_REAL_MODEL_PROVENANCE_MISSING")
        evaluation = [sys.executable, str(root / "eval_model.py"), "--url", f"http://127.0.0.1:{wrapper_port}",
                      "--cases", str(cases), "--out", str(evidence / "eval.draft.json"),
                      "--repeat", str(args.repeat), "--allow-draft-labels"]
        write_json(evidence / "eval-command.json", evaluation)
        with (evidence / "eval.stdout.log").open("wb") as output:
            subprocess.run(evaluation, stdout=output, stderr=subprocess.STDOUT, env=environment, cwd=root, check=True)
        report = json.loads((evidence / "eval.draft.json").read_text(encoding="utf-8"))
        actual = report.get("rows", [])
        provenance_rows_match = len(actual) == len(rows) * args.repeat and all(
            row.get("source") == "real-model"
            and row.get("configured_model") == served_model and row.get("model_revision") == revision for row in actual)
        all_http_success = bool(actual) and all(row.get("interface_ok") is True for row in actual)
        if not provenance_rows_match:
            raise RuntimeError("PREFLIGHT_PROVENANCE_OR_ROW_COUNT_MISMATCH_SEE_EVAL")
        unavailable = {"UPSTREAM_UNREACHABLE", "UPSTREAM_TIMEOUT", "INVALID_UPSTREAM_JSON", "INVALID_UPSTREAM_OBJECT"}
        if any(row.get("error") in unavailable or str(row.get("error", "")).startswith("UPSTREAM_HTTP_") for row in actual):
            raise RuntimeError("PREFLIGHT_UPSTREAM_UNAVAILABLE_SEE_EVAL")
        capture_script = getattr(args, "gateway_capture_script", None)
        if capture_script:
            inject_lan_ok = bool(getattr(args, "gateway_capture_inject_lan_ok", False))
            if tier == "cloud" and not inject_lan_ok:
                raise RuntimeError("CLOUD_CAPTURE_REQUIRES_EXPLICIT_INJECT_LAN_OK")
            capture_script = Path(capture_script).resolve(strict=True)
            if not capture_script.is_file():
                raise RuntimeError("GATEWAY_CAPTURE_SCRIPT_MUST_BE_A_FILE")
            capture = [sys.executable, str(capture_script), "--url", f"http://127.0.0.1:{wrapper_port}",
                       "--tier", tier, "--out", str(evidence / "gateway_capture")]
            if inject_lan_ok:
                capture.append("--inject-lan-ok")
            write_json(evidence / "gateway-capture-command.json", capture)
            write_json(evidence / "gateway-capture-source.json", {"path": str(capture_script), "sha256": sha256(capture_script),
                                                                    "inject_lan_ok": inject_lan_ok,
                                                                    "inject_lan_ok_source": "explicit_preflight_flag" if inject_lan_ok else "not_injected"})
            with (evidence / "gateway-capture.stdout.log").open("wb") as output:
                subprocess.run(capture, stdout=output, stderr=subprocess.STDOUT, env=environment, cwd=root, check=True)
        completion = "completed" if all_http_success else "completed_with_interface_errors"
        status.update(status=completion, provenance_rows_match=provenance_rows_match, all_http_success=all_http_success,
                      all_requests_from_real_model=provenance_rows_match and all_http_success,
                      formal_accuracy_evidence=False, gateway_capture_executed=bool(capture_script),
                      finished_at=timestamp(), summary=report.get("summary"))
        write_json(evidence / "status.json", status)
        print(json.dumps({"preflight_completed": True, "status": completion, "all_http_success": all_http_success,
                          "formal_accuracy_evidence": False, "evidence": str(evidence)}))
    except BaseException as exc:
        status.update(status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                      error_class=type(exc).__name__, error=str(exc), finished_at=timestamp())
        write_json(evidence / "status.json", status)
        raise
    finally:
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            # Also reap descendants if the group leader exited first.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("weights", "preflight"))
    parser.add_argument("--model-dir", default="/root/autodl-tmp/models/small")
    parser.add_argument("--parts-dir", default="/root/autodl-tmp/models/small_parts")
    parser.add_argument("--adapter-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--legacy-adapter-dir", default="/root/autodl-tmp/sensehome-adapter")
    parser.add_argument("--evidence-dir", default="/root/autodl-tmp/sensehome-evidence")
    parser.add_argument("--mirror", default="https://hf-mirror.com")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--startup-timeout", type=int, default=1200)
    parser.add_argument("--gateway-capture-script", help="Explicit local Python file to run after successful draft evaluation")
    parser.add_argument("--gateway-capture-inject-lan-ok", action="store_true",
                        help="Pass --inject-lan-ok to a cloud capture; this is an injected fixture, not a LAN measurement")
    args = parser.parse_args()
    if args.repeat < 1 or args.startup_timeout < 1:
        parser.error("repeat and startup-timeout must be positive")
    if args.operation == "weights":
        recover_weights(args)
    else:
        preflight(args)


if __name__ == "__main__":
    main()
