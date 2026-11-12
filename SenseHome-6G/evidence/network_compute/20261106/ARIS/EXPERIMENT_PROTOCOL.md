# Experiment Protocol: ARIS Continuation

Date: 2026-11-06. This documents the current campaign before its frozen final run
is accepted; it is not an independently preregistered study.

## Intended Claims and Evidence

| ID | Bounded claim | Primary test | Cannot establish |
|---|---|---|---|
| C1 | Default command entry consumes both model-endpoint observations and invokes C | Default-init tests, persisted HTTP events, captured-observation C replay | Real model accuracy or physical deployment |
| C2 | Observed network/resource failures change route with reasons | 14-phase replay, matrix, switches | General superiority on captured household traffic |
| C3 | Rule and model command paths retain the structured whitelist | Explicit local-action test, unsafe-plan tests, atomicity cases | Full natural-language/physical safety certification |
| C4 | Joint information affects contract attainment under declared loads | Seven matched arms; live bounded-queue stress | Guaranteed improvement for every scene, load or metric |
| C5 | Reported quantiles/counts are recomputable from raw records | Five mechanical audits and source-freeze gate | Independent semantic approval |

## Workloads

- Replay: 14 synthetic phases, three seeds, 200 samples/phase, seven arms.
  58,800 raw rows; 60/20/20 complex/simple/safety request mix.
- Direct-thread stress: 1/8/32/64 clients, 128 requests/cell, three repetitions,
  healthy and cloud-down, joint/network-only: 6,144 rows.
- Sensitivity: 12 OFAT configurations on a distinct exploratory seed; 780 samples
  each, including isolated queue/RTT boundary probes: 9,360 rows.
- Safety: 25 rejected structured input cases and three positive controls.
- Default HTTP matrix: three different seeds, ten samples/phase and seven arms:
  2,940 rows. Actual endpoint health/transport timing varies between arms.
- Default HTTP stress: the same finite-client cell layout: 6,144 rows.

Static cloud/local choices apply only to complex tasks. Every arm retains local
safety/simple isolation. Network-only shares the estimated-cost objective while
omitting compute health and queue inputs. Compute-only omits network eligibility,
transport and recovery gating. No-hysteresis removes both hold and margin.

## Failure and Timing

Completion is the authorized fixture action/state contract, not a model self-report.
All failures remain in latency populations; deadline attainment requires completion
AND client latency within the service budget. Fast failures cannot count as good SLA.
Modeled service costs, direct-thread wall times, and HTTP client wall times are
separate populations and must not be pooled.

Runtime budgets and hardware assumptions are design/test settings. HTTP timestamps
are not packet captures or wireless KPIs. The stress process uses fixed service
sleeps and real queue counters; matrix queue/memory are injected. Unknown live
GPU memory and bandwidth stay unknown.

## Verification and Review

Every run uses a new empty directory; failed/pilot runs remain available.
Raw traces, commands, effective configurations, endpoint records, decision sequence,
events and source snapshots must be present. Captured observations replay in actual
C decision order, not event completion order. The full suite rejects source edits
during execution.

The native fresh ARIS review is same-family/provisional. A clean assumption sweep
does not substitute for experiment integrity review or official competition acceptance.
The official submission, demand evidence and final media remain separately open.
