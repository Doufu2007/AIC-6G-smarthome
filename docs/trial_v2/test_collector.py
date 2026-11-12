"""Behavior checks for consent, truthful counting, withdrawal and local boundaries."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def collector():
    path = Path(__file__).with_name('collector.py')
    assert path.is_file(), 'The trial collector has not been implemented'
    spec = importlib.util.spec_from_file_location('trial_collector', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectorTests(unittest.TestCase):
    def test_disagreement_never_creates_a_session_or_disk_record(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            with self.assertRaises(ValueError):
                store.start({'consent': False, 'role': 'user'})
            self.assertEqual(list(Path(directory).rglob('*.json')), [])
            self.assertEqual(store.summary()['participant_feedback_count'], 0)

    def test_cancellation_discards_unsaved_answers_and_requests(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            session = store.start({'consent': True, 'role': 'user'})
            store.cancel(session['session_id'])
            with self.assertRaises(ValueError):
                store.submit(session['session_id'], {'clear_or_confusing': 'I stopped'})
            self.assertEqual(list(Path(directory).rglob('*.json')), [])

    def test_automation_submission_does_not_increase_participant_count(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'automation_test')
            session = store.start({'consent': True, 'role': 'user'})
            record = store.submit(session['session_id'], {'clear_or_confusing': 'AUTOMATED TEST'})
            self.assertEqual(record['record_kind'], 'automation_test')
            self.assertFalse(record['valid_connected_trial'])
            self.assertEqual(store.summary()['participant_feedback_count'], 0)
            self.assertEqual(store.summary()['automation_test_count'], 1)

    def test_practice_cannot_claim_gateway_execution_or_a_valid_trial(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            session = store.start({'consent': True, 'role': 'user'})
            task = store.execute(session['session_id'], {'command': 'Open a light', 'origin': 'self_authored'})
            self.assertEqual(task['transport'], 'practice_no_gateway')
            self.assertIsNone(task['task_completed'])
            record = store.submit(session['session_id'], {'clear_or_confusing': 'No gateway was connected'})
            self.assertFalse(record['valid_connected_trial'])
            self.assertEqual(store.summary()['valid_connected_trial_count'], 0)

    def test_http_success_and_model_claim_cannot_become_state_confirmation(self):
        m = collector()
        result = m.assess_response(200, {'answer': 'Done', 'semantic_complete': True, 'actions': []}, None, None)
        self.assertIsNone(result['task_completed'])
        self.assertFalse(result['state_execution_confirmed'])

    def test_partial_plan_does_not_pass_when_omitted_target_already_matches(self):
        m = collector()
        before = {'lights': {'living': {'brightness': 0}, 'bed': {'brightness': 30}}}
        after = {'lights': {'living': {'brightness': 60}, 'bed': {'brightness': 30}}}
        result = m.assess_response(200, {'semantic_complete': True, 'interface_ok': True,
            'actions': [{'name': 'light_control', 'params': {'location': 'living', 'brightness': 60}}]},
            before, after, [('lights', 'living', 'brightness', 60), ('lights', 'bed', 'brightness', 30)])
        self.assertFalse(result['task_completed'])

    def test_complete_plan_and_changed_target_states_confirm_only_simulated_execution(self):
        m = collector()
        before = {'lights': {'living': {'brightness': 0}, 'bed': {'brightness': 0}}}
        after = {'lights': {'living': {'brightness': 60}, 'bed': {'brightness': 30}}}
        result = m.assess_response(200, {'semantic_complete': True, 'interface_ok': True,
            'actions': [{'name': 'light_control', 'params': {'location': 'living', 'brightness': 60}},
                        {'name': 'light_control', 'params': {'location': 'bed', 'brightness': 30}}]},
            before, after, [('lights', 'living', 'brightness', 60), ('lights', 'bed', 'brightness', 30)])
        self.assertTrue(result['task_completed'])

    def test_csv_formula_strings_are_text_and_raw_json_remains_verbatim(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            session = store.start({'consent': True, 'role': 'user'})
            store.submit(session['session_id'], {'clear_or_confusing': '=1+2'})
            exported = list(csv.DictReader(io.StringIO(store.export_csv())))
            self.assertEqual(exported[0]['clear_or_confusing'], "'=1+2")
            self.assertEqual(exported[0]['valid_connected_trial'], 'False')
            raw = json.loads(store.export_json())
            self.assertEqual(raw['records'][0]['feedback']['clear_or_confusing'], '=1+2')

    def test_external_gateway_credentials_custom_path_and_traversal_are_rejected(self):
        m = collector()
        for value in ['https://example.com', 'http://127.0.0.1:8080/x',
                      'http://user:secret@127.0.0.1:8080', 'http://127.0.0.1:8080/?x=y']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.validate_gateway(value)
        self.assertEqual(m.validate_gateway('http://127.0.0.1:8080'), 'http://127.0.0.1:8080')
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            with self.assertRaises(ValueError):
                store.cancel('../outside')

    def test_empty_feedback_and_unbounded_fields_are_not_written(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            session = store.start({'consent': True, 'role': 'user'})
            for feedback in [{}, {'clear_or_confusing': 'x' * 1201}]:
                with self.subTest(feedback_size=len(str(feedback))), self.assertRaises(ValueError):
                    store.submit(session['session_id'], feedback)
            self.assertEqual(list(Path(directory).rglob('*.json')), [])

    def test_withdrawal_removes_previously_submitted_record(self):
        m = collector()
        with tempfile.TemporaryDirectory() as directory:
            store = m.TrialStore(Path(directory), 'participant')
            session = store.start({'consent': True, 'role': 'user'})
            store.submit(session['session_id'], {'clear_or_confusing': 'A real answer'})
            self.assertEqual(store.summary()['participant_feedback_count'], 1)
            store.cancel(session['session_id'])
            self.assertEqual(store.summary()['participant_feedback_count'], 0)
            self.assertEqual(list(Path(directory).rglob('*.json')), [])

    def test_state_http_errors_cannot_confirm_empty_target_no_mutation(self):
        m = collector()
        class UnavailableState(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                raw = b'{"error":"state_unavailable"}'
                self.send_response(503)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                raw = b'{"actions":[],"error":"gateway_unavailable"}'
                self.send_response(503)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), UnavailableState)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = m.TrialStore(Path(directory), 'automation_test', f'http://127.0.0.1:{server.server_port}')
                session = store.start({'consent': True, 'role': 'user'})
                task = store.execute(session['session_id'], {'command': m.PRESETS['T4']['command'],
                    'origin': 'preset', 'preset_id': 'T4'})
                self.assertIsNone(task['empty_target_no_action_no_mutation'])
                self.assertFalse(task['state_execution_confirmed'])
                self.assertEqual(task['state_before']['http_status'], 503)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
