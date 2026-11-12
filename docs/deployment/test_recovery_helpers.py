"""Offline contract checks. Fake tiny weights are never inference evidence."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
from urllib.parse import urlparse
import urllib.error
import urllib.request


DIRECTORY = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


recovery = module("autodl_small_recovery")
packager = module("prepare_autodl_recovery")


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class RecoveryContractTests(unittest.TestCase):
    def test_weight_sha256_constant_is_64_lowercase_hex(self):
        self.assertRegex(recovery.WEIGHT_SHA256, r"\A[0-9a-f]{64}\Z")

    def test_api_correct_lfs_hash_is_accepted(self):
        # Literal recorded from the actual API, independent of our constant.
        api_sha256 = "dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee"
        self.assertEqual(recovery.WEIGHT_SHA256, api_sha256)
        snapshot = {"sha": recovery.REVISION, "siblings": [{"rfilename": "model.safetensors", "lfs": {
            "sha256": api_sha256, "size": 3087467144}}]}

        def fake_open(url, *, method="GET", headers=None, header_trace=None):
            if method == "HEAD":
                header_trace.append({"status": 200, "x-repo-commit": recovery.REVISION})
                return Response(b"", headers={"x-repo-commit": recovery.REVISION})
            return Response(json.dumps(snapshot).encode())

        with tempfile.TemporaryDirectory() as temporary, patch.object(recovery, "open_url", fake_open):
            files = recovery.snapshot_metadata("https://example.invalid", Path(temporary))
        self.assertEqual(files["model.safetensors"]["lfs"]["sha256"], api_sha256)

    def test_only_missing_parts_downloaded_and_logs_excluded_then_full_weight_reused(self):
        # Preserve the real 16-part shape; use 154 bytes, never GPU weights.
        payload = bytes(range(154))
        digest = hashlib.sha256(payload).hexdigest()
        auxiliary = b'{"contract_test":true}\n'
        snapshot = {"sha": recovery.REVISION, "siblings": [
            {"rfilename": "model.safetensors", "lfs": {"sha256": digest, "size": len(payload)}},
            *({"rfilename": name, "blobId": recovery.git_blob_sha1(auxiliary)} for name in recovery.ANCILLARY)]}
        ranges = []

        def fake_open(url, *, method="GET", headers=None, header_trace=None):
            if "/api/models/" in url:
                return Response(json.dumps(snapshot).encode())
            if method == "HEAD":
                if header_trace is not None:
                    header_trace.append({"status": 200, "x-repo-commit": recovery.REVISION})
                return Response(b"", headers={"x-repo-commit": recovery.REVISION})
            if urlparse(url).path.endswith("model.safetensors"):
                request_range = headers["Range"].removeprefix("bytes=")
                start, end = map(int, request_range.split("-"))
                ranges.append((start, end))
                return Response(payload[start:end + 1], status=206,
                                headers={"Content-Range": f"bytes {start}-{end}/154"})
            return Response(auxiliary)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            parts = base / "parts"
            parts.mkdir()
            for index in range(16):
                if index not in (0, 12):
                    (parts / f"part_{index:02d}").write_bytes(payload[index * 10:(index + 1) * 10])
            (parts / "part_00.log").write_bytes(b"MUST NOT BE CONCATENATED")
            args = SimpleNamespace(model_dir=base / "model", parts_dir=parts,
                                   evidence_dir=base / "evidence", mirror="https://example.invalid")
            with patch.multiple(recovery, TOTAL=154, CHUNK=10, WEIGHT_SHA256=digest), patch.object(recovery, "open_url", fake_open):
                with contextlib.redirect_stdout(io.StringIO()):
                    recovery.recover_weights(args)
                self.assertEqual(ranges, [(0, 9), (120, 129)])
                self.assertEqual((base / "model/model.safetensors").read_bytes(), payload)
                ranges.clear()
                with contextlib.redirect_stdout(io.StringIO()):
                    recovery.recover_weights(args)
                self.assertEqual(ranges, [])

    def test_revision_header_mismatch_blocks_weights(self):
        snapshot = {"sha": recovery.REVISION, "siblings": [{"rfilename": "model.safetensors", "lfs": {
            "sha256": recovery.WEIGHT_SHA256, "size": recovery.TOTAL}}]}

        def fake_open(url, *, method="GET", headers=None, header_trace=None):
            if method == "HEAD":
                if header_trace is not None:
                    header_trace.append({"status": 200, "x-repo-commit": "wrong"})
                return Response(b"", headers={"x-repo-commit": "wrong"})
            return Response(json.dumps(snapshot).encode())

        with tempfile.TemporaryDirectory() as temporary, patch.object(recovery, "open_url", fake_open):
            with self.assertRaisesRegex(RuntimeError, "HEADER_REVISION_UNCONFIRMED"):
                recovery.snapshot_metadata("https://example.invalid", Path(temporary))

    def test_range_ignored_cannot_replace_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            args = SimpleNamespace(model_dir=base / "model", parts_dir=base / "parts",
                                   evidence_dir=base / "evidence", mirror="https://example.invalid")
            with patch.object(recovery, "snapshot_metadata", return_value={}), patch.object(
                    recovery, "open_url", return_value=Response(b"ignored", status=200)):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "RANGE_NOT_HONORED"):
                    recovery.recover_weights(args)
            self.assertFalse((base / "parts/part_00").exists())
            self.assertFalse((base / "model/model.safetensors").exists())

    def test_partial_part_resumes_tail_and_retries_truncated_transfer(self):
        payload = b"abcdefghij"
        ranges = []
        urls = []

        def fake_open(url, *, headers=None):
            urls.append(url)
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            ranges.append((start, end))
            result = payload[start:6] if len(ranges) == 1 else payload[start:end + 1]
            return Response(result, status=206, headers={"Content-Range": f"bytes {start}-{end}/10"})

        with tempfile.TemporaryDirectory() as temporary:
            part = Path(temporary) / "part_00"
            part.write_bytes(payload[:3])
            with patch.object(recovery, "open_url", fake_open), patch.object(recovery.time, "sleep"):
                result = recovery.download_part("https://example.invalid/model.safetensors", part, 0, 9, 10)
            self.assertEqual(part.read_bytes(), payload)
            self.assertEqual(ranges, [(3, 9), (6, 9)])
            self.assertEqual(result, {"attempts": 2, "resumed_prefix_bytes": 3})
            self.assertTrue(all("recovery_range=" in url and "recovery_nonce=" in url for url in urls))
            self.assertNotEqual(urls[0], urls[1])

    def test_existing_download_file_resumes_without_discarding_prefix(self):
        payload = b"abcdefghij"
        with tempfile.TemporaryDirectory() as temporary:
            part = Path(temporary) / "part_00"
            partial = part.with_name(part.name + ".download")
            partial.write_bytes(payload[:4])
            ranges = []

            def fake_open(url, *, headers=None):
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                ranges.append((start, end))
                return Response(payload[start:end + 1], status=206,
                                headers={"Content-Range": f"bytes {start}-{end}/10"})

            with patch.object(recovery, "open_url", fake_open):
                recovery.download_part("https://example.invalid/model.safetensors", part, 0, 9, 10)
            self.assertEqual(ranges, [(4, 9)])
            self.assertEqual(part.read_bytes(), payload)

    def test_redirect_keeps_head_range_and_selected_commit_header(self):
        trace = []
        handler = recovery.RevisionTraceRedirect(trace)
        original = urllib.request.Request("https://example.invalid/resolve/config.json", method="HEAD",
                                          headers={"Range": "bytes=1-2", "User-Agent": recovery.USER_AGENT})
        redirected = handler.redirect_request(original, None, 302, "Found",
                                              {"x-repo-commit": recovery.REVISION}, "https://cdn.invalid/config.json?secret=signed")
        self.assertEqual(redirected.get_method(), "HEAD")
        self.assertEqual(redirected.get_header("Range"), "bytes=1-2")
        self.assertEqual(redirected.get_header("User-agent"), recovery.USER_AGENT)
        self.assertEqual(trace, [{"status": 302, "x-repo-commit": recovery.REVISION}])
        self.assertNotIn("secret", json.dumps(trace))
        with self.assertRaisesRegex(RuntimeError, "UNSAFE_MIRROR_REDIRECT"):
            handler.redirect_request(original, None, 302, "Found", {}, "http://cdn.invalid/config.json")

    def test_http_error_json_preserved_and_loopback_proxy_disabled(self):
        failure = urllib.error.HTTPError("http://127.0.0.1:18092/infer", 502, "Bad Gateway", {},
                                        io.BytesIO(b'{"error":"UPSTREAM_BAD_JSON","actions":[]}'))
        opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(failure))
        with tempfile.TemporaryDirectory() as temporary, patch.object(recovery.urllib.request, "build_opener", return_value=opener) as builder:
            evidence = Path(temporary) / "error.json"
            with self.assertRaises(urllib.error.HTTPError):
                recovery.http_json("http://127.0.0.1:18092/infer", {}, error_evidence=evidence)
            saved = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(saved["http_status"], 502)
            self.assertEqual(saved["body"]["error"], "UPSTREAM_BAD_JSON")
            self.assertEqual(builder.call_args.args[0].proxies, {})
        with self.assertRaisesRegex(ValueError, "MUST_USE_LOOPBACK"):
            recovery.http_json("http://external.invalid/infer")

    def test_size_matched_corrupt_part_cannot_publish_weight(self):
        payload = bytes(range(154))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            parts = base / "parts"
            parts.mkdir()
            for index in range(16):
                (parts / f"part_{index:02d}").write_bytes(payload[index * 10:(index + 1) * 10])
            (parts / "part_12").write_bytes(b"X" * 10)
            args = SimpleNamespace(model_dir=base / "model", parts_dir=parts,
                                   evidence_dir=base / "evidence", mirror="https://example.invalid")
            with patch.multiple(recovery, TOTAL=154, CHUNK=10, WEIGHT_SHA256=hashlib.sha256(payload).hexdigest()), patch.object(
                    recovery, "snapshot_metadata", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "ASSEMBLED_WEIGHT_HASH_MISMATCH"):
                    recovery.recover_weights(args)
            self.assertFalse((base / "model/model.safetensors").exists())

    def test_bundle_import_layout_draft_labels_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundle = base / "recovery.zip"
            with contextlib.redirect_stdout(io.StringIO()):
                packager.build(bundle)
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(base / "unpacked")
                self.assertEqual(len(archive.namelist()), 7)
                self.assertFalse(any("reviewed" in name for name in archive.namelist()))
            unpacked = base / "unpacked"
            manifest = json.loads((unpacked / "bundle-manifest.json").read_text(encoding="utf-8"))
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256((unpacked / name).read_bytes()).hexdigest(), digest)
            labels = [json.loads(line) for line in (unpacked / "backends/eval_commands.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["human_reviewed"] is False for row in labels))
            result = subprocess.run([sys.executable, str(unpacked / "eval_model.py"), "--help"],
                                    cwd=unpacked, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--allow-draft-labels", result.stdout)


if __name__ == "__main__":
    unittest.main()
