import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from network_trace import NetworkTrace  # noqa: E402


class NetworkTraceTests(unittest.TestCase):
    def test_same_seed_produces_same_trace_id_and_conditions(self):
        a = NetworkTrace.generate(seed=17, steps=[30, 120, None, 60], probes_per_step=2)
        b = NetworkTrace.generate(seed=17, steps=[30, 120, None, 60], probes_per_step=2)

        self.assertEqual(a.trace_id, b.trace_id)
        self.assertEqual(a.samples, b.samples)

    def test_trace_round_trip_preserves_replay_data(self):
        trace = NetworkTrace.generate(seed=3, steps=[30, None], probes_per_step=1)
        restored = NetworkTrace.from_dict(json.loads(json.dumps(trace.to_dict())))

        self.assertEqual(restored.trace_id, trace.trace_id)
        self.assertEqual(restored.samples, trace.samples)
        self.assertEqual(restored.seed, 3)


if __name__ == "__main__":
    unittest.main()
