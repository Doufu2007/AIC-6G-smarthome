import tempfile
import unittest
from unittest.mock import patch

from evaluation import start_bridge_demo


class FakeProcess:
    _next_pid = 1000

    def __init__(self):
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class StartBridgeDemoTests(unittest.TestCase):
    def run_demo(self, open_browser):
        output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(output_dir.cleanup)
        processes = [FakeProcess(), FakeProcess()]
        ready_urls = []

        def record_ready(url, _process):
            ready_urls.append(url)

        def record_open(url):
            self.assertEqual(ready_urls[-1], 'http://127.0.0.1:8080/api/meta')
            return True

        args = [
            '--out', output_dir.name,
            '--runtime-port', '8090',
            '--bridge-port', '8080',
        ]
        if open_browser:
            args.append('--open-browser')

        with patch.object(start_bridge_demo.subprocess, 'Popen', side_effect=processes), \
             patch.object(start_bridge_demo, 'ready', side_effect=record_ready), \
             patch.object(start_bridge_demo.time, 'sleep', side_effect=KeyboardInterrupt), \
             patch.object(start_bridge_demo, 'webbrowser', create=True) as browser:
            browser.open.side_effect = record_open
            result = start_bridge_demo.main(args)

        self.assertEqual(result, 0)
        return browser.open

    def test_open_browser_after_bridge_is_ready_when_requested(self):
        browser_open = self.run_demo(open_browser=True)
        browser_open.assert_called_once_with('http://127.0.0.1:8080')

    def test_browser_does_not_open_by_default(self):
        browser_open = self.run_demo(open_browser=False)
        browser_open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
