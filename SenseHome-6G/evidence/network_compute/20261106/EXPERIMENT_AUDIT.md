# Experiment Integrity Audit

Date: 2026-11-06. Overall: **WARN**. Integrity: **warn**.

Reviewer: fresh native task `/root/experiment_integrity`, configured
`gpt-6.1-sol/xhigh`, fallback because `gpt-6-astra` is unavailable.
Identity source: native spawn configuration; runtime-attested model: unknown.
Assurance: expected/configured same-family, **provisional**, not cross-family acceptance.

## Findings

No fabricated real-model ground truth, self-normalization, phantom result or current
metric-value mismatch was found. The bounded C1-C5 protocol claims are supported,
with their scope restrictions retained.

| Check | Status | Finding |
|---|---|---|
| A. Ground Truth | PASS | Author-declared software contracts and synthetic faults, accurately labeled. |
| B. Normalization | PASS | Raw type-7 quantiles; completion AND latency for deadline attainment; all failures retained. |
| C. Files/Values | PASS | Five final raw/summary/audit sets exist and pass fresh verification. |
| D. Called Metrics | PASS | Reported statistics and actual C/default HTTP/action paths are reached. |
| E. Scope | WARN | Software-only evidence, few synthetic seeds, negative tradeoffs and partial test-source run binding. |
| F. Type | PASS | All five evaluations are `simulation_only`, with measured software timing subtypes. |

## Verification

The reviewer read the primary scripts, C policy, configurations, gateway and
protocol directly. Parsed row counts are replay 58,800; direct-thread 6,144;
sensitivity 9,360; safety 28; HTTP 9,084, including 2,940 matrix and 6,144 stress.
HTTP persisted events also total 9,084.

All artifact seals, the 15 input-manifest hashes and the archived/current
46-source manifest sets match. Replay has 8,400 complete seven-arm paired units
with identical input hashes. Independently implemented raw arithmetic reproduces
the replay-overall and HTTP-cell sample counts, type-7 quantiles, completion and
deadline numerators. Fresh read-only project audit reruns pass all five bundles,
including exact C replay for replay/sensitivity and captured HTTP observations in
`decision_sequence` order. No HTTP profile/deadline/scenario or local-isolation
mismatch was found; live stress memory stays unknown.

Ground truth is not model output: see `experiment_replay.py:26`, `:41` and
`experiment_http_gateway.py:102`, `:176`. Modeled safety success is a declared
read-only assumption (`experiment_replay.py:44`), not measured five-nines reliability.
Statistics retain failures (`experiment_stats.py:33`, `:35`).

## Qualifications

- The freeze gate covers its 46 enumerated sources. Most broader gateway test and
  backend sources are only preserved in the explicitly post-run supplemental
  snapshot, not retroactively bound to execution (`experiment_replay.py:179`,
  `run_experiment_suite.py:70`, `supplemental_source_snapshot.json:3`).
  `source_tree_verification.json` additionally reports all 168 tests passing from
  the runnable packaged copy; it accurately retains the same post-run limitation.
- Replay has three synthetic seed clusters; sensitivity uses one exploratory
  seed. Wilson intervals are descriptive and bootstrap intervals are not field
  reliability guarantees (`experiment_stats.py:90`, `:92`).
- HTTP arms share configured workloads, not identical measured telemetry.
  Stress is finite-client closed-loop with no coordinated-omission correction
  (`experiment_http_gateway.py:368`, `:369`).
- Joint is not uniformly better: replay deadline attainment is 4,254/5,040 versus
  no-hysteresis 4,260/5,040. Direct-thread joint completion is 1,789/1,848 versus
  network-only 1,848/1,848. HTTP matrix compute-only deadline attainment is
  210/252 versus joint 192/252. HTTP cloud-down c32 is joint 208/231 versus
  network-only 231/231, and c64 is 89/231 versus 102/231. These counterexamples
  remain in the raw data and generated delivery report.
- The parent updated `BUILD_NOTE.md:8` and `:9` during review: F3 passed; F4
  Type-A passed with review/package pending. The deterministic acceptance record
  is `validation/verification.json:2`, completed 2026-11-06T09:27:40.734068+00:00.

This is not real AI accuracy, real GPU/radio capacity, household deployment,
natural-language safety certification, official submission readiness or external
semantic approval. A clean deterministic audit is not a substitute for those.

## Full Trace

Detailed checks, exact source/result file:line evidence, counterexamples and
C1-C5 judgments are in
`../../../.aris/traces/experiment-audit/2026-11-06_run01/reviewer_response.json`.
The input manifest SHA-256 is
`61d8d53e3bfd6d0873aa33bbc52881815f45b15d4736aeca04441f01ebc1a6ad`.
