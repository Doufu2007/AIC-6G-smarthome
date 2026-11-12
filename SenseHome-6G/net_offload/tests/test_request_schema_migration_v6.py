"""Two independently constructed journals safely upgrade one legacy database."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from command_requests import RequestJournal


class RequestMigrationTests(unittest.TestCase):
    def test_concurrent_legacy_upgrade_preserves_both_initializers(self):
        first_read, second_attempted, release = threading.Event(), threading.Event(), threading.Event()
        results = []

        class PausedJournal(RequestJournal):
            def _connect(self):
                connection = super()._connect()

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(connection, name)
                    def __enter__(self):
                        connection.__enter__()
                        return self
                    def __exit__(self, *args):
                        return connection.__exit__(*args)
                    def execute(self, sql, *args):
                        second = threading.current_thread().name == 'second-migration'
                        if sql == 'BEGIN IMMEDIATE' and second:
                            second_attempted.set()
                        cursor = connection.execute(sql, *args)
                        if sql == 'PRAGMA table_info(requests)':
                            rows = list(cursor)
                            cursor.close()
                            (second_attempted if second else first_read).set()
                            if not release.wait(3):
                                raise TimeoutError('migration test was not released')
                            return iter(rows)
                        return cursor
                return Proxy()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE requests (command_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, '
                           'command TEXT NOT NULL, context_json TEXT NOT NULL, result_json TEXT, '
                           'created_at REAL NOT NULL, updated_at REAL NOT NULL)')
            def initialize():
                try:
                    PausedJournal(path)
                    results.append('ok')
                except Exception as exc:
                    results.append(type(exc).__name__ + ': ' + str(exc))
            workers = [threading.Thread(target=initialize, name=name)
                       for name in ('first-migration', 'second-migration')]
            workers[0].start()
            try:
                self.assertTrue(first_read.wait(3))
                workers[1].start()
                self.assertTrue(second_attempted.wait(3))
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(4)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(results, ['ok', 'ok'])
            self.assertEqual(PausedJournal(path).lookup('missing')['request_state'], 'not_found')


if __name__ == '__main__':
    unittest.main()
