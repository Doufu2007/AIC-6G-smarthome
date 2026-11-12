# Network/Compute Experiment Report

This is synthetic design-proxy replay, not physical 6G, field traffic, or model inference.
Service latencies below are explicitly modeled. C policy IPC runtime is recorded separately.
All routes retain local safety isolation. Failed or unresolved requests violate deadlines.

## Overall Fixed Planner Contract

| Service | Arm | n | p50 ms | p95 ms | p99 ms | Contract % | Deadline % |
|---|---|---:|---:|---:|---:|---:|---:|
| interactive_inference | compute-only | 5040 | 103.53 | 801.40 | 801.40 | 68.37 | 61.90 |
| interactive_inference | joint | 5040 | 65.78 | 104.78 | 104.94 | 84.40 | 84.40 |
| interactive_inference | network-only | 5040 | 64.53 | 801.40 | 801.40 | 70.24 | 62.98 |
| interactive_inference | no-hysteresis | 5040 | 65.76 | 104.78 | 104.96 | 84.52 | 84.52 |
| interactive_inference | rules-only | 5040 | 1.40 | 1.40 | 1.40 | 0.00 | 0.00 |
| interactive_inference | static-cloud | 5040 | 76.98 | 801.40 | 801.40 | 61.23 | 47.62 |
| interactive_inference | static-local | 5040 | 103.83 | 801.40 | 801.40 | 70.24 | 63.10 |
| safety_control | compute-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | joint | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | network-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | no-hysteresis | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | rules-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | static-cloud | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| safety_control | static-local | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | compute-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | joint | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | network-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | no-hysteresis | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | rules-only | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | static-cloud | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |
| sensor_batch | static-local | 1680 | 1.30 | 1.30 | 1.30 | 100.00 | 100.00 |

## Switch Evidence

Complex-task route changes: {'joint': 93, 'network-only': 75, 'compute-only': 15, 'no-hysteresis': 207}

See switches.json for exact before/after indices, reasons and endpoint observations.

## Statistical Limits

- Quantiles: linear/type-7 over all requests, including failures.
- Completion is checked against a fixed authorized device contract, not NLU quality.
- Wilson intervals are descriptive; trace events are correlated.
- Paired effects use identical request hashes and seed-cluster bootstrap.
- A few synthetic seeds cannot support population-level reliability claims.
- Service reliability targets are design goals, not achieved radio guarantees.
- Model service times, memory and trace queue states are declared injected parameters.
- Concurrency results are a separate real-thread stub test, not these modeled values.
