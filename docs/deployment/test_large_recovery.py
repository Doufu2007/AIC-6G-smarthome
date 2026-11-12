"""Offline large-model identity/download and shared preflight contracts."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import autodl_large_recovery as large
import autodl_small_recovery as small
import prepare_autodl_recovery as packager


class LargeRecoveryTests(unittest.TestCase):
    def test_missing_lfs_metadata_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "REQUIRES_EXPLICIT_LFS"):
            large.identity({"blobId": "a" * 40}, "model-00001-of-00001.safetensors", require_lfs=True)
        with self.assertRaisesRegex(RuntimeError, "SHA256_MISSING_OR_INVALID"):
            large.identity({"lfs": {"sha256": "a" * 65, "size": 10}}, "bad", require_lfs=True)

    def test_frozen_revision_mismatch_blocks_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model, evidence = root / "model", root / "evidence"
            model.mkdir()
            evidence.mkdir()
            metadata = {"id": large.REPO, "sha": "a" * 40, "siblings": [{"rfilename": "config.json", "blobId": "b" * 40}]}
            other = {**metadata, "sha": "c" * 40}
            with patch.object(large, "api_request", side_effect=[metadata, other]):
                with self.assertRaisesRegex(RuntimeError, "IMMUTABLE_API_REVISION_MISMATCH"):
                    large.freeze_snapshot(model, "https://example.invalid", evidence)
            self.assertFalse((model / large.FROZEN_API).exists())

    def test_download_only_indexed_shards_max_four_workers_resume_and_verify(self):
        revision = "d" * 40  # Synthetic identity confined to this temporary test.
        shard_data = {"model-00001-of-00002.safetensors": bytes(range(25)),
                      "model-00002-of-00002.safetensors": bytes(range(25, 40))}
        index = json.dumps({"weight_map": {"a": list(shard_data)[0], "b": list(shard_data)[1]}}).encode()
        auxiliary = {name: index if name.endswith("index.json") else b"{}" for name in large.ANCILLARY}
        metadata = {"id": large.REPO, "sha": revision, "siblings": [
            *({"rfilename": name, "blobId": small.git_blob_sha1(data)} for name, data in auxiliary.items()),
            *({"rfilename": name, "lfs": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
              for name, data in shard_data.items()),
            {"rfilename": "unrequested.safetensors", "lfs": {"sha256": "e" * 64, "size": 1}}]}
        api_calls, transfers = [], []
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def api(mirror, requested_revision):
            api_calls.append(requested_revision)
            return metadata

        def open_aux(url, *, method="GET", header_trace=None):
            if method == "HEAD":
                header_trace.append({"status": 200, "x-repo-commit": revision})
                return io.BytesIO(b"")
            return io.BytesIO(auxiliary[url.rsplit("/", 1)[1]])

        def transfer(url, part, start, end, total):
            name = url.rsplit("/", 1)[1]
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                transfers.append((name, start, end))
            time.sleep(0.01)
            part.write_bytes(shard_data[name][start:end + 1])
            with lock:
                state["active"] -= 1
            return {"attempts": 1, "resumed_prefix_bytes": 0}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(model_dir=root / "model", parts_dir=root / "parts", evidence_dir=root / "evidence",
                                   mirror="https://example.invalid", workers=4)
            with patch.object(large, "CHUNK", 10), patch.object(large, "api_request", api), patch.object(
                    large.small, "open_url", open_aux), patch.object(large.small, "download_part", transfer):
                with contextlib.redirect_stdout(io.StringIO()):
                    large.recover_weights(args)
                self.assertEqual(len(transfers), 5)
                self.assertTrue(1 < state["maximum"] <= 4)
                self.assertNotIn("unrequested.safetensors", [item[0] for item in transfers])
                manifest = json.loads((args.model_dir / "sensehome-verified-snapshot.json").read_text())
                self.assertEqual(large.verify_large_snapshot(args.model_dir, manifest), revision)
                transfers.clear()
                with contextlib.redirect_stdout(io.StringIO()):
                    large.recover_weights(args)
                self.assertEqual(transfers, [])
                self.assertEqual(api_calls.count("main"), 1)
                self.assertEqual(api_calls.count(revision), 2)
            for name, data in shard_data.items():
                self.assertEqual((args.model_dir / name).read_bytes(), data)
            bad = args.model_dir / list(shard_data)[0]
            bad.write_bytes(b"X" * bad.stat().st_size)
            with self.assertRaisesRegex(RuntimeError, "FILE_VERIFICATION_FAILED"):
                large.verify_large_snapshot(args.model_dir, manifest)

    def test_large_preflight_has_distinct_ports_identity_and_small_stop_gate(self):
        args = SimpleNamespace()
        with patch.object(large, "assert_small_stopped") as gate, patch.object(large.small, "preflight") as shared:
            large.preflight(args)
        gate.assert_called_once_with(args)
        self.assertEqual(shared.call_args.kwargs, {"snapshot_validator": large.verify_large_snapshot,
            "served_model": "sensehome-large", "upstream_port": 8002, "wrapper_port": 18093, "tier": "cloud", "model_size": "large"})

    def test_large_bundle_contains_both_scripts_and_draft_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "large.zip"
            with contextlib.redirect_stdout(io.StringIO()):
                packager.build(bundle, include_large=True)
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(len(archive.namelist()), 8)
                self.assertIn("autodl_large_recovery.py", archive.namelist())
                labels = [json.loads(line) for line in archive.read("backends/eval_commands.jsonl").decode().splitlines()]
                self.assertEqual(len(labels), 20)
                self.assertTrue(all(item["human_reviewed"] is False for item in labels))


class SharedPreflightTests(unittest.TestCase):
    def run_preflight(self, root, *, interface_error=False, upstream_error=False, bad_revision=False, use_hook=True,
                      served_model="sensehome-large", upstream_port=8002, wrapper_port=18093, tier="cloud", size="large",
                      inject_lan_ok=True):
        deployment = root / "adapter"
        model = root / "model"
        deployment.mkdir()
        model.mkdir()
        (deployment / "backends").mkdir()
        for name in ("eval_model.py", "backends/openai_backend.py", "backends/device_actions.py"):
            (deployment / name).write_text("# offline contract fixture\n")
        labels = [{"id": "T01", "human_reviewed": False, "command": "test"}]
        (deployment / "backends/eval_commands.jsonl").write_text(json.dumps(labels[0]) + "\n")
        (model / "sensehome-verified-snapshot.json").write_text("{}")
        hook = root / "capture.py"
        hook.write_text("# explicit offline capture fixture\n")
        args = SimpleNamespace(adapter_dir=deployment, model_dir=model, evidence_dir=root / "evidence",
                               legacy_adapter_dir=root / "absent", repeat=1, startup_timeout=30,
                               gateway_capture_script=str(hook) if use_hook else None,
                               gateway_capture_inject_lan_ok=inject_lan_ok)
        revision = "f" * 40
        processes, calls = [], []

        def popen(command, **kwargs):
            process = SimpleNamespace(pid=1000 + len(processes), poll=lambda: None, wait=lambda **kw: 0)
            processes.append((command, kwargs, process))
            return process

        def http(url, body=None, **kwargs):
            if url.endswith("/v1/models"):
                return {"data": [{"id": served_model}]}
            if url.endswith("/health"):
                return {"ready": True}
            return {"source": "real-model", "configured_model": served_model, "model_revision": revision}

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1].endswith("eval_model.py"):
                row = {"interface_ok": not (interface_error or upstream_error), "source": "real-model",
                       "configured_model": served_model, "model_revision": "bad" if bad_revision else revision,
                       "error": "UPSTREAM_UNREACHABLE" if upstream_error else "INVALID_MODEL_ACTIONS" if interface_error else None,
                       "action_correct": not (interface_error or upstream_error)}
                out = Path(command[command.index("--out") + 1])
                out.write_text(json.dumps({"rows": [row], "summary": {"n_http_ok": int(row["interface_ok"])}}))
            return SimpleNamespace(returncode=0)

        fake_os = SimpleNamespace(name="posix", environ={}, killpg=Mock())
        with patch.object(small, "os", fake_os), patch.object(small, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9)), patch.object(
                small, "port_available") as ports, patch.object(small, "save_command", lambda cmd, out: Path(out).write_text("fixture")), patch.object(
                small.importlib.metadata, "version", return_value="offline_fixture"), patch.object(small.subprocess, "check_output", return_value="fixture GPU"), patch.object(
                small.subprocess, "Popen", popen), patch.object(small.subprocess, "run", run), patch.object(small, "http_json", http), contextlib.redirect_stdout(io.StringIO()):
            error = None
            try:
                small.preflight(args, snapshot_validator=lambda *ignored: revision, served_model=served_model,
                                upstream_port=upstream_port, wrapper_port=wrapper_port, tier=tier, model_size=size)
            except RuntimeError as exc:
                error = exc
        evidence = next((root / "evidence").glob(size + "_preflight_*"))
        return SimpleNamespace(status=json.loads((evidence / "status.json").read_text()), error=error,
                               processes=processes, calls=calls, evidence=evidence, ports=ports, hook=hook)

    def test_parameterized_interface_error_still_captures_but_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_preflight(Path(temporary), interface_error=True)
            self.assertIsNone(result.error)
            self.assertEqual(result.status["status"], "completed_with_interface_errors")
            self.assertTrue(result.status["provenance_rows_match"])
            self.assertFalse(result.status["all_http_success"])
            self.assertFalse(result.status["all_requests_from_real_model"])
            self.assertFalse(result.status["formal_accuracy_evidence"])
            self.assertTrue(result.status["gateway_capture_executed"])
            result.ports.assert_any_call(8002)
            result.ports.assert_any_call(18093)
            self.assertIn("8002", result.processes[0][0])
            environment = result.processes[1][1]["env"]
            self.assertEqual(environment["SENSEHOME_MODEL"], "sensehome-large")
            self.assertEqual(environment["SENSEHOME_TIER"], "cloud")
            self.assertEqual(environment["SENSEHOME_WRAPPER_PORT"], "18093")
            capture, options = result.calls[-1]
            self.assertEqual(capture[1], str(result.hook))
            self.assertIn("http://127.0.0.1:18093", capture)
            self.assertIn("cloud", capture)
            self.assertTrue(options["check"])
            self.assertFalse(options.get("shell", False))
            self.assertTrue((result.evidence / "gateway-capture-source.json").is_file())
            command = json.loads((result.evidence / "gateway-capture-command.json").read_text(encoding="utf-8"))
            self.assertIn("--inject-lan-ok", command)

    def test_cloud_capture_without_explicit_injection_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_preflight(Path(temporary), inject_lan_ok=False)
            self.assertRegex(str(result.error), "REQUIRES_EXPLICIT_INJECT_LAN_OK")
            self.assertEqual(result.status["status"], "failed")
            self.assertEqual(len(result.calls), 1)

    def test_upstream_unavailable_blocks_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_preflight(Path(temporary), upstream_error=True)
            self.assertRegex(str(result.error), "UPSTREAM_UNAVAILABLE")
            self.assertEqual(result.status["status"], "failed")
            self.assertEqual(len(result.calls), 1)

    def test_wrong_revision_blocks_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_preflight(Path(temporary), bad_revision=True)
            self.assertRegex(str(result.error), "PROVENANCE_OR_ROW_COUNT_MISMATCH")
            self.assertEqual(len(result.calls), 1)

    def test_small_identity_and_no_optional_hook_remain_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_preflight(Path(temporary), served_model="sensehome-small", upstream_port=8001,
                                        wrapper_port=18092, tier="gateway", size="small", use_hook=False)
            self.assertIsNone(result.error)
            self.assertEqual(result.status["status"], "completed")
            self.assertFalse(result.status["gateway_capture_executed"])
            self.assertEqual(len(result.calls), 1)
            self.assertIn("8001", result.processes[0][0])


if __name__ == "__main__":
    unittest.main()
