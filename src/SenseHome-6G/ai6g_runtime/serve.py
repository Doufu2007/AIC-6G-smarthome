"""Small HTTP server for the SenseHome AI+6G runtime."""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai6g_runtime.executor import DeviceStore
    from ai6g_runtime.health_monitor import HealthMonitor, ProbeReport, load_calibration
    from ai6g_runtime.models import OpenAICompatibleBackend, RuleBackend
    from ai6g_runtime.router import ObservationCache, TaskRouter
    from ai6g_runtime.runtime import TaskRuntime
else:
    from .executor import DeviceStore
    from .health_monitor import HealthMonitor, ProbeReport, load_calibration
    from .models import OpenAICompatibleBackend, RuleBackend
    from .router import ObservationCache, TaskRouter
    from .runtime import TaskRuntime


def build_runtime(log_path: str | Path, state_path: str | Path | None = None) -> TaskRuntime:
    cache = ObservationCache(ttl_s=float(os.getenv("AI6G_OBSERVATION_TTL_S", "5")))
    trusted = {s.strip() for s in os.getenv("AI6G_TRUSTED_BACKENDS", "").split(",") if s.strip()}
    router = TaskRouter(
        cache,
        trusted_backends=trusted,
        resource_ttl_s=float(os.getenv("AI6G_RESOURCE_TTL_S", "5")),
        max_queue_ms=float(os.getenv("AI6G_MAX_QUEUE_MS", "250")),
        min_free_mem_mb=float(os.getenv("AI6G_MIN_FREE_MEM_MB", "256")),
        max_inflight=int(os.getenv("AI6G_MAX_INFLIGHT", "8")),
        switch_margin_ms=float(os.getenv("AI6G_ROUTE_SWITCH_MARGIN_MS", "10")),
    )
    backends = {"local-rules": RuleBackend()}
    local_url = os.getenv("AI6G_LOCAL_URL")
    cloud_url = os.getenv("AI6G_CLOUD_URL")
    if local_url:
        backends["local-model"] = OpenAICompatibleBackend(
            "local-model", local_url, os.getenv("AI6G_LOCAL_MODEL", "local"), os.getenv("AI6G_LOCAL_API_KEY")
        )
    if cloud_url:
        backends["cloud-model"] = OpenAICompatibleBackend(
            "cloud-model", cloud_url, os.getenv("AI6G_CLOUD_MODEL", "cloud"), os.getenv("AI6G_CLOUD_API_KEY")
        )
    resolved_state_path = state_path if state_path is not None else os.getenv("AI6G_STATE_PATH")
    return TaskRuntime(
        router=router,
        backends=backends,
        store=DeviceStore(state_path=resolved_state_path),
        log_path=log_path,
        trusted_backends=trusted,
    )


def _model_backends(runtime: TaskRuntime) -> dict[str, OpenAICompatibleBackend]:
    """Return configured network model adapters, excluding the rule backend."""

    return {
        name: backend
        for name, backend in runtime.backends.items()
        if name in {"local-model", "cloud-model"}
    }


def _write_probe_log(path: str | Path | None, reports: Sequence[ProbeReport]) -> None:
    if path is None:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        for report in reports:
            handle.write(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def start_probe_thread(
    runtime: TaskRuntime,
    *,
    health_url: str,
    calibration: Mapping[str, object] | None = None,
    interval_s: float | None = None,
    probe_log: str | Path | None = None,
    timeout_s: float = 5.0,
    resource_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> tuple[threading.Event, threading.Thread]:
    """Start real model health probes and return a stop event and thread.

    The first probe runs immediately.  ``interval_s`` controls optional
    repeated probes; omitting it keeps the startup probe one-shot.  Reports
    are append-only JSONL evidence and are never routed through ``TaskRuntime``.
    """

    if interval_s is not None and interval_s < 0:
        raise ValueError("interval_s must be non-negative when provided")
    if interval_s == 0:
        interval_s = None
    model_backends = _model_backends(runtime)
    ttl_s = float(interval_s) if interval_s is not None else 5.0
    monitor = HealthMonitor(
        health_url=health_url,
        backends=model_backends,
        calibration=calibration or {},
        timeout_s=timeout_s,
        ttl_s=ttl_s,
        resource_provider=resource_provider,
    )
    stop_event = threading.Event()

    def worker() -> None:
        reports = monitor.run_once()
        _write_probe_log(probe_log, reports)
        if interval_s is None:
            return
        while not stop_event.wait(float(interval_s)):
            reports = monitor.run_due()
            _write_probe_log(probe_log, reports)

    thread = threading.Thread(target=worker, name="ai6g-health-probe", daemon=True)
    thread.start()
    return stop_event, thread


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a finite non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the SenseHome AI+6G HTTP runtime.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--log", default="SenseHome-6G/ai6g_runtime/evidence/requests.jsonl")
    parser.add_argument("--state", default=os.getenv("AI6G_STATE_PATH"), help="persistent device state JSON path")
    parser.add_argument("--calibration", help="JSON file with per-backend quality_score and source_sha256")
    parser.add_argument(
        "--probe-interval",
        type=_nonnegative_float,
        help="repeat real model health probes every N seconds; 0 runs only the startup probe",
    )
    parser.add_argument("--probe-log", help="append probe reports to this JSONL file")
    return parser


def _loopback_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _health_url(host: str, port: int) -> str:
    target = _loopback_host(host)
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"
    return f"http://{target}:{port}/v1/health"


def _json_safe_value(value):
    """Represent unavailable numeric observations as JSON null on the wire."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


class RuntimeHandler(BaseHTTPRequestHandler):
    runtime: TaskRuntime

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(_json_safe_value(payload), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1024*1024:
                self.close_connection = True
                return {}
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/state":
            self._send(200, self.runtime.state())
        elif self.path == "/v1/logs":
            self._send(200, self.runtime.logs())
        elif self.path == "/v1/health":
            response = {"backends": sorted(self.runtime.backends), "time": time.time()}
            capacity = getattr(self.runtime.router, "inflight_snapshot", None)
            if callable(capacity):
                response["inflight_by_backend"] = capacity()
            self._send(200, response)
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/tasks":
            body = self._body()
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send(400, {"ok": False, "error_code": "invalid_task", "error": "text is required"})
                return
            result = self.runtime.handle(text, request_id=body.get("request_id"))
            code = result.get('error_code')
            status = 200 if result.get('ok') else 409 if code == 'request_id_conflict' else 400 if code in ('invalid_request_id', 'invalid_task') else 422
            self._send(status, result)
            return
        if self.path == "/v1/health":
            body = self._body()
            try:
                if not isinstance(body['reachable'], bool):
                    raise ValueError('reachable must be boolean')
                resources = body.get("resources")
                if resources is not None and not isinstance(resources, dict):
                    raise ValueError("resources must be an object")
                self.runtime.health(
                    str(body["backend"]), reachable=bool(body["reachable"]),
                    latency_ms=float(body["latency_ms"]), quality_score=float(body["quality_score"]),
                    checked_at=float(body.get("checked_at", time.time())),
                    error_rate=float(body.get("error_rate", 0.0)), detail=str(body.get("detail", "")),
                    quality_by_family=body.get("quality_by_family"),
                    resources=resources,
                )
            except (KeyError, OverflowError, TypeError, ValueError):
                self._send(400, {"ok": False, "error_code": "invalid_health"})
                return
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not_found"})

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        calibration = load_calibration(args.calibration) if args.calibration else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid calibration: {exc}")
    runtime = build_runtime(args.log, state_path=args.state)
    handler = type("BoundRuntimeHandler", (RuntimeHandler,), {"runtime": runtime})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    actual_port = int(server.server_address[1])
    server_thread = threading.Thread(target=server.serve_forever, name="ai6g-http-server", daemon=True)
    server_thread.start()
    stop_event: threading.Event | None = None
    probe_thread: threading.Thread | None = None
    if _model_backends(runtime):
        stop_event, probe_thread = start_probe_thread(
            runtime,
            health_url=_health_url(args.host, actual_port),
            calibration=calibration,
            interval_s=args.probe_interval,
            probe_log=args.probe_log,
        )
    print(f"AI6G runtime listening on http://{args.host}:{actual_port}")
    try:
        while server_thread.is_alive():
            server_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if stop_event is not None:
            stop_event.set()
        if probe_thread is not None:
            probe_thread.join()
        if server_thread.is_alive():
            server.shutdown()
        server_thread.join(timeout=2.0)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
