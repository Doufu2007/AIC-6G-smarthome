# Network/Compute Gateway Integration

Date: 2026-11-06. ARIS workflow: research-implement-feature, balanced, ask=never.
Base commit: none (not a git repo). Existing files and historical evidence preserved.

Target: connect the existing default Dashboard command entry to the C dual-endpoint
resource policy, with cached trustworthy observations and reproducible experiments.
Inputs: service profiles, policy configuration, synthetic traces, HTTP backend health,
and authorized household commands. Outputs: versioned events, raw JSONL, trace files,
matrix/ablation/concurrency statistics, policy-switch evidence, and scoped reports.
Success command: run the project test and experiment suite against a new output directory.
Scope: PC isolated state, loopback HTTP contract fixtures and declared synthetic
network impairments. No paid providers, GPU rental, physical actuators or invented
field data. Not a claim that official submission, interviews or final media are complete.

Files to audit: offload_policy.c/.h, policy_cli.c, offload_gateway.py,
resource_policy.py, experiment_inputs.py, experiment_stats.py, experiment_*.py,
dashboard/bridge_server.py, backend_monitor.py, dashboard/tests, and net_offload/tests.
