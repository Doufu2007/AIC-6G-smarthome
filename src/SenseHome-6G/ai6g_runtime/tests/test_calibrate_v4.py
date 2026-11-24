"""Calibration resume must retain the identity of every completed request."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from ai6g_runtime.evaluation import calibrate_v4 as calibration
from ai6g_runtime.models import ModelResult


class CalibrationResumeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.tasks = Path(self.folder.name) / "tasks.jsonl"
        self.out = Path(self.folder.name) / "calibration.json"
        self.journal = self.out.with_suffix(".rows.jsonl")
        self.manifest = self.out.with_suffix(".manifest.json")
        rows = [
            {"id": "on", "text": "打开客厅的灯", "gold": {
                "expected_action": [{"type": "light_on", "location": "客厅", "brightness": 100}],
                "expected_state": {"lights": {"客厅": {"is_on": True, "brightness": 100}}}}},
            {"id": "off", "text": "关闭卧室的灯", "gold": {
                "expected_action": [{"type": "light_off", "location": "卧室"}],
                "expected_state": {"lights": {"卧室": {"is_on": False, "brightness": 0}}}}},
        ]
        self.tasks.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8")
        self.env = {
            "AI6G_LOCAL_URL": "http://fake-user:fake-password@127.0.0.1:9001/v1/chat/completions?api_key=fake-query-secret#fake-fragment",
            "AI6G_LOCAL_MODEL": "fixture-small",
            "AI6G_LOCAL_API_KEY": "fake-header-secret",
            "AI6G_CLOUD_URL": "http://127.0.0.1:9002/v1/chat/completions",
            "AI6G_CLOUD_MODEL": "fixture-large",
        }
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.calls = []

        def infer(backend, task, timeout_s):
            self.calls.append((backend.name, task.text))
            return ModelResult(True, {"actions": [{"type": "light_on", "location": "客厅", "brightness": 100}]},
                               backend=backend.name, model_real=True,
                               raw_response={"usage": {"total_tokens": 7}})
        self.infer = infer
        self.infer_patch = mock.patch.object(calibration.OpenAICompatibleBackend, "infer", autospec=True,
                                             side_effect=infer)
        self.infer_mock = self.infer_patch.start()
        self.addCleanup(self.infer_patch.stop)

    def run_calibration(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return calibration.calibrate(self.tasks, self.out)

    def test_completed_resume_reuses_rows_and_manifest_tracks_real_identity(self):
        self.run_calibration()
        before = self.journal.read_bytes()
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(self.manifest.exists(), "new calibration must bind its journal to a frozen manifest")
        envelope = json.loads(self.manifest.read_text(encoding="utf8"))
        frozen = envelope["manifest"]
        self.assertEqual(frozen["task_source_sha256"], hashlib.sha256(self.tasks.read_bytes()).hexdigest())
        entry = frozen["backends"]["local-model"]
        self.assertEqual(entry["endpoint"], "http://127.0.0.1:9001/v1/chat/completions")
        self.assertEqual(entry["model"], "fixture-small")
        self.assertEqual(entry["seed"], 20261104)
        self.assertEqual(entry["max_tokens"], 384)
        self.assertEqual(len(entry["prompt_sha256"]), 64)
        for source in ("evaluation/calibrate_v4.py", "models.py", "protocol.py", "executor.py", "evaluator.py"):
            self.assertEqual(frozen["sources"][source], hashlib.sha256((ROOT / source).read_bytes()).hexdigest())
        serialized = self.manifest.read_text(encoding="utf8")
        for secret in ("fake-user", "fake-password", "fake-query-secret", "fake-fragment", "fake-header-secret"):
            self.assertNotIn(secret, serialized)
        self.run_calibration()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_model_and_endpoint_changes_cannot_resume_completed_rows(self):
        self.run_calibration()
        before = self.journal.read_bytes()
        for field, value in (("AI6G_LOCAL_MODEL", "other-model"),
                             ("AI6G_CLOUD_URL", "http://127.0.0.1:9010/v1/chat/completions"),
                             ("AI6G_LOCAL_URL", self.env["AI6G_LOCAL_URL"].replace("fake-query-secret", "other-query"))):
            with self.subTest(field=field), mock.patch.dict(os.environ, {field: value}):
                with self.assertRaisesRegex(ValueError, "manifest|fingerprint"):
                    self.run_calibration()
                self.assertEqual(self.journal.read_bytes(), before)
                self.assertEqual(len(self.calls), 4)

    def test_prompt_seed_and_token_budget_changes_cannot_resume(self):
        self.run_calibration()
        backend_class = calibration.OpenAICompatibleBackend
        for attribute, value in (("system_prompt", "different prompt"), ("seed", 7), ("max_tokens", 128)):
            def construct(*args, **kwargs):
                backend = backend_class(*args, **kwargs)
                setattr(backend, attribute, value)
                return backend
            with self.subTest(attribute=attribute), mock.patch.object(calibration, "OpenAICompatibleBackend", side_effect=construct):
                with self.assertRaisesRegex(ValueError, "manifest|fingerprint"):
                    self.run_calibration()
                self.assertEqual(len(self.calls), 4)

    def test_core_source_change_cannot_resume(self):
        self.run_calibration()
        with mock.patch.object(calibration, "_source_hashes", create=True,
                               return_value={"models.py": "0" * 64}):
            with self.assertRaisesRegex(ValueError, "manifest|fingerprint"):
                self.run_calibration()
        self.assertEqual(len(self.calls), 4)

    def test_duplicate_completed_key_is_rejected_without_truncation(self):
        self.run_calibration()
        with self.journal.open("ab") as handle:
            handle.write(self.journal.read_bytes().splitlines(keepends=True)[0])
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)

    def test_unbound_legacy_rows_are_never_silently_adopted(self):
        self.run_calibration()
        if self.manifest.exists():
            self.manifest.unlink()
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "manifest|legacy|unbound"):
            self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(len(self.calls), 4)

    def test_partial_final_row_can_be_repaired_without_repeating_requests(self):
        self.run_calibration()
        before = self.journal.read_bytes()
        with self.journal.open("ab") as handle:
            handle.write(b'{"backend": "local-model", "task_id":')
        self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(len(self.calls), 4)

    def test_corruption_before_final_row_is_rejected_without_modifying_journal(self):
        self.run_calibration()
        lines = self.journal.read_bytes().splitlines(keepends=True)
        self.journal.write_bytes(lines[0] + b'{"incomplete":\n' + b"".join(lines[1:]))
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "corrupt|journal"):
            self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)

    def test_crash_after_complete_unterminated_row_resumes_with_separator(self):
        def crash_on_cloud(backend, task, timeout_s):
            if backend.name == "cloud-model":
                raise RuntimeError("simulated process interruption")
            return self.infer(backend, task, timeout_s)
        self.infer_mock.side_effect = crash_on_cloud
        try:
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                self.run_calibration()
        finally:
            self.infer_mock.side_effect = self.infer
        self.journal.write_bytes(self.journal.read_bytes().rstrip(b"\r\n"))
        self.run_calibration()
        rows = [json.loads(line) for line in self.journal.read_bytes().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({(r["backend"], r["task_id"]) for r in rows}), 4)
        self.assertEqual(len(self.calls), 4)

    def test_duplicate_task_ids_are_rejected_before_model_calls(self):
        first = self.tasks.read_text(encoding="utf8").splitlines()[0]
        self.tasks.write_text(first + "\n" + first + "\n", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "duplicate.*task"):
            self.run_calibration()
        self.assertEqual(self.calls, [])

    def test_rows_from_another_manifest_are_rejected_without_modification(self):
        self.run_calibration()
        rows = [json.loads(line) for line in self.journal.read_text(encoding="utf8").splitlines()]
        rows[0]["calibration_fingerprint"] = "0" * 64
        self.journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(len(self.calls), 4)

    def test_invalid_complete_tail_is_not_treated_as_a_crash(self):
        self.run_calibration()
        with self.journal.open("ab") as handle:
            handle.write(b'{"backend": invalid}')
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "corrupt|journal"):
            self.run_calibration()
        self.assertEqual(self.journal.read_bytes(), before)

    def test_crash_in_json_literal_or_numeric_value_is_repairable(self):
        self.run_calibration()
        before = self.journal.read_bytes()
        for tail in (b'{"raw":{"ok":tru', b'{"raw":{"ok":fal', b'{"raw":nul',
                     b'{"elapsed_ms":-', b'{"elapsed_ms":1.', b'{"elapsed_ms":1.2e+',
                     b'{"elapsed_ms":1e'):
            with self.subTest(tail=tail):
                self.journal.write_bytes(before + tail)
                self.run_calibration()
                self.assertEqual(self.journal.read_bytes(), before)
                self.assertEqual(len(self.calls), 4)


if __name__ == "__main__":
    unittest.main()
