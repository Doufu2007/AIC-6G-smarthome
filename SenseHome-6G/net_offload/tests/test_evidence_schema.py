import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from evidence import validate_results_schema  # noqa: E402
from network_trace import NetworkTrace  # noqa: E402


class EvidenceSchemaTests(unittest.TestCase):
    def test_valid_results_include_trace_and_latency_metadata(self):
        trace = NetworkTrace.generate(seed=2, steps=[30], probes_per_step=1)
        data = {
            "meta": {"generated_at": "now", "note": "stub"},
            "stub_parameters": {"cloud_compute_ms": 300.0},
            "harness": {"infer_n_per_task_per_phase": 1},
            "summaries": [{
                "phase": "P1-normal",
                "arm": "adaptive",
                "overall": {
                    "probe_median_ms": 1.0,
                    "policy_median_ms": 0.1,
                    "backend_median_ms": 3.0,
                    "total_user_median_ms": 4.1,
                },
            }],
            "paired_traces": {"P1-normal": trace.to_dict()},
            "raw_runs": [{"phase": "P1-normal", "arm": arm, "rows": [{
                "task": "complex", "backend": "cloud-model", "trace_id": trace.trace_id,
                "trace_index": 0, "source": "stub-assumption", "probe_ms": 1.0,
                "policy_ms": 0.1, "backend_ms": 3.0, "total_user_ms": 4.1,
            }]} for arm in ("adaptive", "static-cloud")],
        }

        self.assertEqual(validate_results_schema(data), [])

    def test_missing_trace_id_is_reported(self):
        data = {
            "meta": {},
            "stub_parameters": {},
            "harness": {},
            "summaries": [],
            "paired_traces": {"P1": {"samples": []}},
        }

        errors = validate_results_schema(data)

        self.assertTrue(any("trace_id" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
