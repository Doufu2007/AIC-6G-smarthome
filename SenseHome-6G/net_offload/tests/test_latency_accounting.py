import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from offload_gateway import LatencyBreakdown  # noqa: E402


class LatencyAccountingTests(unittest.TestCase):
    def test_total_user_latency_includes_probe_policy_and_backend(self):
        breakdown = LatencyBreakdown(probe_ms=410.0, policy_ms=1.5, backend_ms=171.6)
        self.assertAlmostEqual(breakdown.total_user_ms, 583.1, places=3)

    def test_missing_optional_components_are_zero(self):
        breakdown = LatencyBreakdown()
        self.assertEqual(breakdown.total_user_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
