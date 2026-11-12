"""Build, test, replay, stress, sweep and audit without cloud/device credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from experiment_inputs import HERE, write_json
from experiment_replay import seal_artifacts, source_manifest


def compiler_invocation(compiler: str) -> list[str]:
    if Path(compiler).name.lower() in {"zig", "zig.exe"}:
        return [compiler, "cc"]
    return [compiler]


def run(command: list[str], log: Path, cwd: Path = HERE) -> None:
    print("RUN " + subprocess.list2cmdline(command), flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=900)
    def decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gb18030" if os.name == "nt" else "utf-8", errors="replace")
    stdout, stderr = decode(result.stdout), decode(result.stderr)
    log.write_text(stdout + stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log}")
    print(stdout[-700:] or stderr[-700:], flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compiler", help="gcc, clang, tcc, or Zig executable path")
    parser.add_argument("--per-phase", type=int, default=200)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--inflight-max", type=int, default=64)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("choose a new empty output directory")
    compiler = args.compiler or shutil.which("gcc") or shutil.which("clang") or shutil.which("tcc")
    portable = HERE / ".toolchain/tcc/tcc.exe"
    if compiler is None and portable.is_file():
        compiler = str(portable)
    if compiler is None:
        parser.error("no C compiler found; install gcc/clang/tcc or pass --compiler")
    compiler = str(Path(compiler).resolve())
    compiler_args = compiler_invocation(compiler)
    validation = out / "validation"
    validation.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    # Build where the existing integration tests expect binaries.
    for name, source in (("policy_cli", "policy_cli.c"),
                         ("test_offload_policy", "test_offload_policy.c"),
                         ("test_compute_policy", "test_compute_policy.c")):
        flags = ["-Wall"] if "tcc" in Path(compiler).name.lower() else ["-std=c99", "-Wall", "-Wextra", "-O2"]
        run([*compiler_args, *flags, "-o", name + suffix, source, "offload_policy.c"],
            validation / (name + "_build.txt"))
        if name != "policy_cli":
            run([str(HERE / (name + suffix))], validation / (name + "_tests.txt"))
    root = HERE.parents[1]
    cli = str(HERE / ("policy_cli" + suffix))
    frozen_sources = source_manifest(cli)
    for name, path in (("gateway", HERE / "tests"),
                       ("dashboard", root / "openvela_smart_home/dashboard/tests"),
                       ("deployment", root / "docs/deployment")):
        run([sys.executable, "-m", "unittest", "discover", "-s", str(path), "-v"],
            validation / (name + "_tests.txt"))
    per_phase = 10 if args.quick else args.per_phase
    requests = 64 if args.quick else args.requests
    repetitions = 1 if args.quick else args.repetitions
    for script, name, extra in (
        ("experiment_replay.py", "replay", ["--per-phase", str(per_phase)]),
        ("experiment_concurrency.py", "concurrency", ["--requests", str(requests),
             "--repetitions", str(repetitions), "--inflight-max", str(args.inflight_max)]),
        ("experiment_sensitivity.py", "sensitivity", ["--per-phase", str(5 if args.quick else 50)]),
        ("experiment_safety.py", "safety", []),
        ("experiment_http_gateway.py", "http_gateway", ["--quick"] if args.quick else []),
    ):
        run([sys.executable, str(HERE / script), "--out", str(out / name), "--cli", cli, *extra],
            validation / (name + "_run.txt"))
        extra_audit = [] if name in {"concurrency", "safety"} else ["--rerun"]
        run([sys.executable, str(HERE / "audit_experiments.py"), str(out / name),
             "--cli", cli, *extra_audit], validation / (name + "_audit.txt"))
    if source_manifest(cli) != frozen_sources:
        raise RuntimeError("SOURCE_CHANGED_DURING_SUITE: preserve outputs but do not claim frozen verification")
    write_json(validation / "verification.json", {
        "passed": True, "completed_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "platform": platform.platform(), "command": sys.argv,
        "compiler": compiler, "compiler_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
        "quick_mode": args.quick,
        "sources_unchanged_during_run": True,
        "checks": ["C build and both C suites", "gateway, dashboard and deployment unittests",
                   "full matrix and exact C rerun", "bounded wall-clock concurrency",
                   "OFAT sensitivity and exact C rerun", "whitelist red-team atomicity",
                   "artifact/source/statistical audit"],
    })
    seal_artifacts(validation)
    print(f"COMPLETE: {out}", flush=True)


if __name__ == "__main__":
    main()
