import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import SubprocessPolicyClient  # noqa: E402


class PolicyCliTimeoutTests(unittest.TestCase):
    def test_startup_read_has_a_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slow_cli.py"
            path.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
            started = time.perf_counter()
            with self.assertRaises(TimeoutError):
                SubprocessPolicyClient([sys.executable, "-u", str(path)], timeout_s=0.25)
            self.assertLess(time.perf_counter() - started, 2.0)

    def test_command_timeout_kills_cli_and_close_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slow_cli.py"
            path.write_text(
                "import sys, time\nprint('ready', flush=True)\n"
                "for line in sys.stdin:\n    time.sleep(3)\n    print('ok', flush=True)\n",
                encoding="utf-8",
            )
            client = SubprocessPolicyClient([sys.executable, "-u", str(path)], timeout_s=0.25)
            try:
                started = time.perf_counter()
                with self.assertRaises(TimeoutError):
                    client._cmd("decide complex")
                self.assertLess(time.perf_counter() - started, 2.0)
                self.assertIsNotNone(client.proc.poll())
            finally:
                started = time.perf_counter()
                client.close()
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_close_does_not_wait_forever_for_quit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slow_cli.py"
            path.write_text(
                "import sys, time\nprint('ready', flush=True)\n"
                "for line in sys.stdin:\n    time.sleep(3)\n",
                encoding="utf-8",
            )
            client = SubprocessPolicyClient([sys.executable, "-u", str(path)], timeout_s=0.25)
            started = time.perf_counter()
            client.close()
            self.assertLess(time.perf_counter() - started, 2.0)
            self.assertIsNotNone(client.proc.poll())


if __name__ == "__main__":
    unittest.main()
