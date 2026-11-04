import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from backend_monitor import BackendMonitor


class BackendMonitorTests(unittest.TestCase):
    def test_reachable_cloud_is_independent_of_model_readiness_and_local_model(self):
        class Backend:
            def health(self):
                return {'reachable': True, 'ready': False, 'latency_ms': 35.0}
        monitor = BackendMonitor({'cloud-model': Backend()})
        monitor.refresh_once()
        snapshot = monitor.snapshot()
        self.assertTrue(snapshot['link']['wan_ok'])
        self.assertTrue(snapshot['link']['lan_ok'])
        self.assertFalse(snapshot['backends']['cloud-model']['ready'])

    def test_cached_snapshot_does_not_wait_for_health_checks(self):
        entered, release = threading.Event(), threading.Event()
        class SlowBackend:
            def health(self):
                entered.set()
                release.wait(2)
                return {'reachable': False, 'ready': False}
        monitor = BackendMonitor({'cloud-model': SlowBackend()})
        worker = threading.Thread(target=monitor.refresh_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(monitor.snapshot()['status'], 'warming-up')
        finally:
            release.set()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
