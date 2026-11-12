# Build Note: Gateway Integration

| Rung | Feature | Acceptance check | Tier | Status |
|---|---|---|---|---|
| F0 | Existing command/HTTP/C/state spine baseline | `python -m unittest discover -s openvela_smart_home/dashboard/tests -p test_remote_execution.py -v` | MUST | passed |
| F1 | Default dual-endpoint cache-to-C integration | `python -m unittest discover -s openvela_smart_home/dashboard/tests -v` | MUST | passed |
| F2 | Observation freshness, distinct-sample recovery and trace contract | `python -m unittest discover -s SenseHome-6G/net_offload/tests -v` | MUST | passed |
| F3 | Actual default HTTP-entry matrix/concurrency evidence | `python SenseHome-6G/net_offload/experiment_http_gateway.py --out <new-dir>` | MUST | passed |
| F4 | Full run, audit, result/claim mapping and scoped delivery | `python SenseHome-6G/net_offload/run_experiment_suite.py --out <new-dir>` | MUST | passed; semantic WARN/provisional |

## Run Record

F0: ARIS Python launcher, five baseline remote-execution tests, exit 0.
F1: 27 Dashboard tests, exit 0. First attempt caught a response-key collision;
renamed the flattened telemetry field `resource_observation`, preserving the
existing device-observation string.
F2: 111 gateway tests, exit 0. Repeated-cache recovery test now explicitly
advances distinct observations and verifies eight repeated reads do not recover.
F3 first smoke: HTTP workload ran, audit correctly rejected colliding request
keys across healthy/cloud-down stress cells. Add scenario to correlation key;
preserve pilot and rerun. Fresh review also exposed missing-policy initialization
still permitting local execution; fail closed and add a no-actuation regression.
Fresh sweep additionally reproduced a negative/all-room partial rule action and
model text inserted through innerHTML. Add conservative planning guards, render
logs as text nodes, and test non-execution with a Node DOM fixture.
Second sweep reproduced unsupported absolute-brightness decrement and an
unhandled 不用 negation. Added explicit integer/relative parsing and regressions.
The campaign started before those findings is a pilot: source-freeze verification
must reject it. Only a later unchanged-source rerun may be called final.
Pilot F3: 9,084 actual HTTP rows, all persisted; captured-observation replay and
mechanical audit passed. Whole-suite source-freeze gate rejected the run as intended.
Playwright/Edge: 1440x900 and 390x844, no browser errors or horizontal overflow,
explicit light state confirmed, model-text injection rendered as text. Screenshots
saved; image-view tool unavailable, so no subjective pixel-review claim.
Final `network_compute_20261106_aris_frozen`: complete suite exit 0, unchanged
enumerated source set, 112 gateway + 31 Dashboard + 25 deployment tests, C70+17.
Replay 58,800; direct-thread stress 6,144; sensitivity 9,360; whitelist28;
default HTTP 9,084. All five audits passed; HTTP contains 9,084 persisted events,
with captured observations replayed through actual C in decision order.
The frozen campaign's net-offload test manifest covers `test_experiment*.py`,
not every historical test source. Supplementary test/backend source snapshots
are post-run snapshots and are not retroactive source-binding proof.
Fresh experiment review completed: A/B/C/D/F PASS, E WARN. C1-C5 supported only
at their declared software/fixture scope. Negative performance tradeoffs retained.
Reviewer configured gpt-6.1-sol/xhigh, native fresh context; runtime-attested model
unknown, same-family expected/configured, semantic acceptance provisional.
Runnable source_tree additionally reran all168 Python tests successfully.
Archive verification is the final packaging check, not a new performance result.
Base is not a Git repository; no commits will be invented.

## Deferred

- Physical 6G/household test, paid real-model runs and external cross-family review.
- Demand interviews, final slide/video refresh and official online submission.

## Blockers

None for local fixture-based engineering; external assertions remain unverified.
