#!/usr/bin/env python3
"""Capture the current C policy -> HTTP model -> PC JSON device action path.

Run locally after forwarding an actual SenseHome wrapper. Network inputs are
controlled route fixtures, not physical WAN/LAN measurements or outage proof.
Every output is observed; this script has no expected semantic-success labels.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "openvela_smart_home/dashboard"))
import bridge_server as bridge  # noqa: E402
from offload_gateway import (  # noqa: E402
    HttpJsonBackend, LinkSnapshot, OffloadGateway, RuleBackend,
    SubprocessPolicyClient, append_jsonl,
)

COMMANDS = (
    ("simple_light", "打开客厅的灯"),
    ("local_safety_query", "查询安防状态"),
    ("context_light", "客厅有点暗"),
    ("compound_light_curtain", "打开客厅的灯并关闭客厅的窗帘"),
    ("unauthorized_lock", "解锁客厅门锁"),
    ("negation", "不要打开客厅的灯"),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecordingBackend(HttpJsonBackend):
    """Keep original HTTP inference results before gateway rejection/fallback."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = []

    def infer(self, command, context=None):
        started = now()
        begin = time.perf_counter()
        result = super().infer(command, context)
        self.attempts.append({"started_at": started, "command": command,
                              "wall_ms": (time.perf_counter() - begin) * 1000,
                              "result": dataclasses.asdict(result)})
        return result


def source_class(event):
    if event.get("fallback_from"):
        return "fallback"
    if event.get("backend") == "local-rules" and event.get("source") == "local-rules":
        return "rules"
    if event.get("source") == "real-model":
        return "real-model"
    return "unverified-upstream"


def run(args):
    output = Path(args.out).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print("output_directory_exists: use a new --out directory", file=sys.stderr)
        return 2

    cli = Path(args.policy_cli).resolve()
    normal_state = Path(bridge.STATE_FILE)
    normal_before = normal_state.read_bytes() if normal_state.exists() else None
    original = bridge.gateway, bridge.STATE_FILE, bridge.backend_monitor
    backend_name = "local-model" if args.tier == "gateway" else "cloud-model"
    backend = RecordingBackend(args.url, backend_name, timeout_s=args.timeout_s,
                               health_timeout_s=args.health_timeout_s,
                               inference_timeout_s=args.timeout_s)
    report = {
        "meta": {"started_at": now(), "tier": args.tier, "url": args.url,
                 "repeat": args.repeat, "python": sys.version, "platform": platform.platform(),
                 "scope": "actual C policy and Python gateway; HTTP wrapper; isolated PC JSON device state; no physical actuator",
                 "network_scope": "controlled route inputs; not physical WAN/LAN or outage evidence",
                 "model_provenance_scope": "wrapper-reported source/model identity; assess alongside independently saved model deployment evidence",
                 "formal_accuracy_evidence": False,
                 "link_field_sources": {
                     "wan_ok": "injected_route_fixture" if args.tier == "gateway" else "wrapper_probe_transport",
                     "wan_rtt_ms": "unknown" if args.tier == "gateway" else "measured_wrapper_http_probe_wall",
                     "lan_ok": ("wrapper_probe_transport" if args.tier == "gateway" else
                                "injected_route_fixture" if args.inject_lan_ok else "unknown"),
                     "lan_rtt_ms": "measured_wrapper_http_probe_wall" if args.tier == "gateway" else "unknown",
                 }},
        "status": "started", "source_sha256": {}, "probes": [], "rows": [], "failures": [],
    }
    report_path = output / "report.json"
    write_json(report_path, report)
    policy = None
    initial_probe = None

    def probe_links():
        started = now()
        ok, body, elapsed, error = backend._request("GET", "/probe", timeout_s=args.health_timeout_s)
        record = {"started_at": started, "transport_ok": ok, "body": body,
                  "wall_ms": elapsed, "error": error}
        report["probes"].append(record)
        write_json(output / "probe.json", report["probes"])
        if args.tier == "gateway":
            # The WAN-unavailable value is an explicit test input, not an outage.
            return LinkSnapshot(wan_ok=False, lan_ok=ok, lan_rtt_ms=elapsed if ok else None)
        # Preserve unknown LAN as null in evidence. C's boolean protocol encodes
        # null conservatively as unavailable; --inject-lan-ok is separately named.
        return LinkSnapshot(wan_ok=ok, wan_rtt_ms=elapsed if ok else None,
                            lan_ok=True if args.inject_lan_ok else None, lan_rtt_ms=None)

    try:
        sources = {
            "capture_script": Path(__file__).resolve(), "policy_cli": cli,
            "policy_source": ROOT / "SenseHome-6G/net_offload/offload_policy.c",
            "policy_cli_source": ROOT / "SenseHome-6G/net_offload/policy_cli.c",
            "offload_gateway": ROOT / "SenseHome-6G/net_offload/offload_gateway.py",
            "bridge_server": ROOT / "openvela_smart_home/dashboard/bridge_server.py",
            "device_actions": ROOT / "openvela_smart_home/dashboard/device_actions.py",
        }
        report["source_sha256"] = {name: file_hash(path) for name, path in sources.items()}
        report["source_paths"] = {name: str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name
                                  for name, path in sources.items()}
        write_json(output / "source_manifest.json", {"sha256": report["source_sha256"], "paths": report["source_paths"]})
        health = backend.health()
        report["wrapper_health"] = health
        write_json(output / "health.json", health)
        if health.get("ready") is not True:
            report["failures"].append({"stage": "health", "error": "WRAPPER_NOT_READY"})
        initial_probe = probe_links()
        if report["probes"][-1]["transport_ok"] is not True:
            report["failures"].append({"stage": "probe", "error": "WRAPPER_PROBE_FAILED"})
        compute = {"model_ready": health.get("model_ready", health.get("ready")),
                   "queue_ms": health.get("upstream_queue_ms") if health.get("queue_scope") == "wrapper_fail_fast_admission" else health.get("queue_ms"),
                   "free_mem_mb": health.get("free_mem_mb"), "inflight": health.get("inflight"),
                   "source": "wrapper_health_at_capture_start"}
        policy = SubprocessPolicyClient(str(cli))
        bridge.backend_monitor = None
        with tempfile.TemporaryDirectory(prefix="sensehome-real-gateway-") as temporary:
            bridge.STATE_FILE = str(Path(temporary) / "isolated_state.json")
            bridge.save_state(bridge.default_state())
            report["initial_state"] = deepcopy(bridge.load_state())
            write_json(output / "initial_state.json", report["initial_state"])
            bridge.gateway = OffloadGateway(
                policy, {"local-rules": RuleBackend(bridge._gateway_local_handler), backend_name: backend},
                link_snapshot=initial_probe, link_probe=probe_links,
                event_sink=append_jsonl(str(output / "events.jsonl")),
                action_executor=bridge._gateway_action_executor,
            )
            for repeat in range(args.repeat):
                for case_id, command in COMMANDS:
                    first_attempt = len(backend.attempts)
                    before = deepcopy(bridge.load_state())
                    row = {"repeat": repeat, "case_id": case_id, "command": command,
                           "started_at": now(), "state_before": before}
                    begin = time.perf_counter()
                    try:
                        result = bridge.process_command(command, {"compute": compute,
                                                                 "capture_scope": report["meta"]["scope"]})
                        event = result.get("gateway_event") or {}
                        row.update(result=result, gateway_event=event, source_class=source_class(event))
                        reasons = []
                        if event.get("interface_ok") is not True:
                            reasons.append("GATEWAY_INTERFACE_FAILED")
                        if event.get("error"):
                            reasons.append(str(event["error"]))
                        if event.get("fallback_from"):
                            reasons.append("MODEL_FALLBACK")
                        if event.get("backend") != "local-rules" and event.get("source") != "real-model":
                            reasons.append("MODEL_SOURCE_UNVERIFIED")
                        if reasons:
                            report["failures"].append({"stage": "command", "case_id": case_id,
                                                       "repeat": repeat, "errors": reasons})
                    except Exception as exc:
                        row.update(source_class="capture-exception", gateway_event=None,
                                   exception_class=type(exc).__name__, exception=str(exc))
                        report["failures"].append({"stage": "command", "case_id": case_id,
                                                   "repeat": repeat, "error": type(exc).__name__})
                    row.update(wall_ms=(time.perf_counter() - begin) * 1000,
                               state_after=deepcopy(bridge.load_state()),
                               backend_attempts=deepcopy(backend.attempts[first_attempt:]))
                    report["rows"].append(row)
                    with (output / "rows.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    write_json(report_path, report)
            report["final_state"] = deepcopy(bridge.load_state())
            write_json(output / "final_state.json", report["final_state"])
        if not any(row.get("source_class") == "real-model" for row in report["rows"]):
            report["failures"].append({"stage": "summary", "error": "NO_REAL_MODEL_ROUTE_OBSERVED"})
    except Exception as exc:
        report["failures"].append({"stage": "capture", "error": type(exc).__name__, "detail": str(exc)})
    finally:
        bridge.gateway, bridge.STATE_FILE, bridge.backend_monitor = original
        if policy is not None:
            policy.close()
        normal_after = normal_state.read_bytes() if normal_state.exists() else None
        report["normal_state_unchanged"] = normal_before == normal_after
        if not report["normal_state_unchanged"]:
            report["failures"].append({"stage": "isolation", "error": "NORMAL_STATE_CHANGED"})
        report["backend_attempts"] = backend.attempts
        report["finished_at"] = now()
        report["status"] = "failed" if report["failures"] else "completed"
        report["counts_by_source"] = {name: sum(row.get("source_class") == name for row in report["rows"])
                                      for name in ("rules", "real-model", "fallback", "unverified-upstream", "capture-exception")}
        write_json(report_path, report)
    print(json.dumps({"status": report["status"], "out": str(output),
                      "rows": len(report["rows"]), "counts_by_source": report["counts_by_source"],
                      "failure_count": len(report["failures"])}, ensure_ascii=True))
    return 1 if report["failures"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="SenseHome wrapper base URL, not raw vLLM /v1")
    parser.add_argument("--tier", choices=("gateway", "cloud"), required=True)
    parser.add_argument("--out", required=True, help="new output directory; never overwritten")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--inject-lan-ok", action="store_true", help="cloud only: explicitly inject LAN available, RTT stays unknown")
    parser.add_argument("--timeout-s", type=float, default=90)
    parser.add_argument("--health-timeout-s", type=float, default=5)
    parser.add_argument("--policy-cli", default=str(ROOT / "SenseHome-6G/net_offload" /
                                                  ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("--url must be an HTTP(S) base URL without credentials, query or fragment")
    if args.repeat < 1 or args.timeout_s <= 0 or args.health_timeout_s <= 0:
        parser.error("repeat and timeout values must be positive")
    if args.inject_lan_ok and args.tier != "cloud":
        parser.error("--inject-lan-ok applies only to cloud tier")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
