"""A durable reservation or publisher lock must never permit late actions."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

NET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NET))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from durable_receipts import SQLiteReceiptExecutor
from mqtt_receipts import LocalMQTTSession, MQTTReceiptTransport
from test_durable_receipts import SoftwareTransport
from test_mqtt_lifecycle import ACTION, STATE, envelopes


class ReceiptDeadlineTests(unittest.TestCase):
    def test_database_reservation_wait_cannot_submit_after_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gateway.sqlite3'
            transport = SoftwareTransport()
            executor = SQLiteReceiptExecutor(path, transport, platform_id='software-home')
            context = {'command_id': 'waited-db', '_receipt_command': '客厅灯60%',
                       '_deadline_monotonic': time.monotonic() + .05}
            responses = []
            with closing(sqlite3.connect(path)) as lock:
                lock.execute('BEGIN IMMEDIATE')
                worker = threading.Thread(target=lambda: responses.append(executor(ACTION, context)))
                worker.start()
                time.sleep(.16)
                lock.rollback()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(transport.publications, 0)
            self.assertEqual(responses[0]['execution_state'], 'unknown')
            self.assertEqual(responses[0]['error'], 'DEADLINE_EXCEEDED_BEFORE_SUBMISSION')
            # The reservation persists and a fresh allowance cannot turn this
            # same request into a new send after an application restart.
            reopened = SQLiteReceiptExecutor(path, transport, platform_id='software-home')
            again = reopened(ACTION, {**context, '_deadline_monotonic': time.monotonic() + 10})
            self.assertEqual(again['error'], 'DEADLINE_EXCEEDED_BEFORE_SUBMISSION')
            self.assertEqual(transport.publications, 0)

    def test_mqtt_publisher_wait_rechecks_deadline_before_real_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            with LocalMQTTSession(Path(tmp), initial_state=deepcopy(STATE)) as session:
                transport = MQTTReceiptTransport('127.0.0.1', session.port, session.platform_id)
                executor = SQLiteReceiptExecutor(Path(tmp) / 'gateway.sqlite3', transport,
                                                platform_id=session.platform_id)
                responses, entered = [], threading.Event()
                context = {'command_id': 'waited-publisher', '_receipt_command': '客厅灯60%',
                           '_deadline_monotonic': time.monotonic() + .05}
                def execute():
                    entered.set()
                    responses.append(executor(ACTION, context))
                try:
                    with transport.lock:
                        worker = threading.Thread(target=execute)
                        worker.start()
                        self.assertTrue(entered.wait(1))
                        time.sleep(.16)
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(responses[0]['execution_state'], 'unknown')
                    self.assertEqual(responses[0]['error'], 'DEADLINE_EXCEEDED_BEFORE_SUBMISSION')
                    self.assertEqual(transport.snapshot()['state_version'], 0)
                    self.assertFalse(any(v['operation'] == 'submit' for v in envelopes(tmp)))
                    self.assertEqual(executor.lookup('waited-publisher')['error'],
                                     'DEADLINE_EXCEEDED_BEFORE_SUBMISSION')
                    timely = executor(ACTION, {**context, 'command_id': 'timely-publisher',
                                               '_deadline_monotonic': time.monotonic() + 2})
                    self.assertEqual(timely['execution_state'], 'applied')
                    self.assertEqual(transport.snapshot()['state_version'], 1)
                    self.assertEqual([v['payload']['command_id'] for v in envelopes(tmp)
                                      if v['operation'] == 'submit'], ['timely-publisher'])
                finally:
                    transport.close()


if __name__ == '__main__':
    unittest.main()
