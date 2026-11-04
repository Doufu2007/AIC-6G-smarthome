import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import BackendResult, LinkSnapshot, OffloadGateway  # noqa: E402


class TaskPolicy:
    def __init__(self):
        self.decisions = []
        self.update_links = mock.Mock()
        self.update_compute = mock.Mock()

    def decide(self, task_class, link):
        self.decisions.append(task_class)
        if task_class in {"simple", "safety"}:
            return "local-rules", "TASK_IS_" + task_class.upper(), "local policy"
        return "cloud-model", "WAN_OK", "complex policy"


class Backend:
    def infer(self, command, context=None):
        return BackendResult(True, True, {"answer": "ok"}, [], source="test")


class LocalRoutingLatencyTests(unittest.TestCase):
    def make_gateway(self, probe):
        policy = TaskPolicy()
        gateway = OffloadGateway(
            policy,
            {"cloud-model": Backend(), "local-rules": Backend()},
            link_snapshot=LinkSnapshot(wan_ok=True, wan_rtt_ms=30, lan_ok=True),
            link_probe=probe,
        )
        return gateway, policy

    def test_local_tasks_do_not_call_slow_or_failing_probe_but_still_call_policy(self):
        def slow_probe():
            time.sleep(0.02)
            return LinkSnapshot(wan_ok=True, lan_ok=True)

        for command, task in (("打开客厅灯", "simple"), ("检测燃气泄漏", "safety")):
            for probe_fn in (slow_probe, lambda: (_ for _ in ()).throw(RuntimeError("probe failed"))):
                with self.subTest(task=task, probe=probe_fn):
                    probe = mock.Mock(side_effect=probe_fn)
                    gateway, policy = self.make_gateway(probe)

                    event = gateway.handle(command)

                    probe.assert_not_called()
                    policy.update_links.assert_not_called()
                    policy.update_compute.assert_not_called()
                    self.assertEqual(policy.decisions, [task])
                    self.assertEqual(event["backend"], "local-rules")
                    self.assertEqual(event["latency"]["probe_ms"], 0.0)
                    self.assertIsNone(event["error"])

    def test_complex_task_still_probes_and_updates_policy(self):
        probe = mock.Mock(return_value=LinkSnapshot(wan_ok=True, lan_ok=True))
        gateway, policy = self.make_gateway(probe)

        event = gateway.handle("有点冷")

        probe.assert_called_once_with()
        policy.update_links.assert_called_once()
        self.assertEqual(policy.decisions, ["complex"])
        self.assertEqual(event["backend"], "cloud-model")

    def test_probe_errors_do_not_leak_between_concurrent_requests(self):
        barrier = threading.Barrier(2)

        def probe():
            if threading.current_thread().name == "bad":
                raise RuntimeError("bad probe")
            return LinkSnapshot(wan_ok=True, wan_rtt_ms=1, lan_ok=True)

        gateway, _ = self.make_gateway(probe)

        def synchronized_decide(task, link, compute=None):
            barrier.wait(timeout=2)
            return "cloud-model", "WAN_OK", "test", 0.0

        gateway._decide = synchronized_decide
        events = {}

        def run(name):
            threading.current_thread().name = name
            events[name] = gateway.handle("有点冷")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, ("bad", "good")))

        self.assertEqual(events["bad"]["reason"], "LINK_PROBE_ERROR")
        self.assertEqual(events["bad"]["error"], "RuntimeError")
        self.assertEqual(events["good"]["reason"], "WAN_OK")
        self.assertIsNone(events["good"]["error"])


if __name__ == "__main__":
    unittest.main()
