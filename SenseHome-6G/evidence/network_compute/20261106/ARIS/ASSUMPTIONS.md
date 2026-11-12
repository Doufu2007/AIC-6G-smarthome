# Assumption Ledger: Gateway Integration

ASK mode: never. Sources distinguish user scope from executor defaults.

| ID | Under-determined choice | Chosen | Class | Source |
|---|---|---|---|---|
| A-001 | Actual household versus software test | Execute only isolated PC state and loopback fixtures; no physical actuation | semantic | default |
| A-002 | Meaning of model completion | Fixed fixture contract/state confirmation is not model accuracy; no new real-model claims | semantic | default |
| A-003 | Network trace provenance | Synthetic application-level events; connection failure is not radio packet loss; record origins | semantic | default |
| A-004 | Default gateway routing | Enable the existing dual-endpoint C resource policy by default; preserve legacy policy clients for existing call sites | interface | default |
| A-005 | Telemetry authority | Server-owned monitor builds decision observations; HTTP request context cannot override network/compute/policy inputs | interface | default |
| A-006 | Missing telemetry | Preserve unknown queue/memory/bandwidth fields; configured inference budgets are estimates, never GPU measurements | semantic | default |
| A-007 | Freshness and recovery | Monotonic sample ages; stale endpoints unavailable; recovery counts distinct monitor observations, not request reads | semantic | default |
| A-008 | Static baseline scope | Static complex-task routes; every arm retains safety/simple local isolation and action whitelist | semantic | default |
| A-009 | End-to-end latency | HTTP client wall clock includes server request handling and event persistence; server phases are separately measured | semantic | default |
| A-010 | Stress load | Finite-client closed-loop HTTP requests; report capacity shedding and missed deadlines as failures | semantic | default |
| A-011 | Review assurance | Native fresh reviewer; same-family review remains provisional, not external acceptance | semantic | default |
| A-012 | Scope of completion | Core engineering and evidence only; real demand confirmation, media refresh and official submission remain separate gates | semantic | user |
| A-013 | Policy configurability | Local config/env controlled, no public API can disable safety isolation | interface | default |
| A-014 | HTTP concurrency and exposure | Default server is threaded with a 128-connection accept queue; bind loopback unless explicitly configured | interface | default |
| A-015 | Failed gateway initialization | Fail closed with an unavailable response; do not silently use legacy permissive command handling | semantic | default |
| A-016 | Shared device state | All gateway local/model actions and patrol transactions use the same state lock | interface | default |
| A-017 | HTTP fixture pairing | Arms share configured trace/workload, not identical measured HTTP telemetry; do not compute paired-network causal claims from those timings | semantic | default |
| A-018 | Concurrent action confirmation | Check returned allowlisted execution effect; a later GET may already reflect another authorized request | semantic | default |
| A-019 | Network impairment mechanism | Fixture applies application-level response delay/503 and body-rate delay; not a packet/radio stack | semantic | default |
| A-020 | Test telemetry authority | Harness controls private fixture state; live stress uses measured queue counters, matrix queue/memory are explicitly injected | semantic | default |
| A-021 | Device LAN in HTTP test | Device state lives in the gateway process; HTTP backend impairment is not a measured physical device LAN outage | semantic | default |
| A-022 | Request-time backend failure | Fall back to local rules on the same request; do not promise a second model attempt | semantic | sweep |
| A-023 | Objective and budgets | C minimizes estimated transport/queue/inference cost with admission gates and hysteresis; deadline/reliability fields evaluate outcomes, not guaranteed reservations | semantic | sweep |
| A-024 | Replay workload | Fixed 60/20/20 complex/simple/safety mix, equal phase counts and fixed phase order; trace at_ms orders samples but does not pace HTTP matrix | semantic | sweep |
| A-025 | Network-only ablation | Keep the joint cost objective/hysteresis but omit compute health gates and queue costs; fix earlier legacy cloud-preference confound before final runs | semantic | sweep |
| A-026 | Fixture calibration | HTTP matrix uses local/cloud service estimates 12/35ms; stress inflight64 versus shipped default8; disclose effective configs | semantic | sweep |
| A-027 | Timeout scope | Modeled replay timeout800ms; real HTTP health0.5s/infer2s/client8s are socket waiting budgets, not a strict total response deadline | semantic | sweep |
| A-028 | Statistical estimand | Type-7 quantiles include failures; completion AND deadline for attainment; Wilson descriptive; paired seed-cluster bootstrap2000 on deterministic replay only | semantic | sweep |
| A-029 | Other ablations | Compute-only omits reachability/transport/network recovery; no-hysteresis removes both cost margin and recovery hold, not just one parameter | semantic | sweep |
| A-030 | Compound/negative/bulk control | Route for planning; local rules refuse instead of partial execution. No guarantee that all-room quick actions can complete without a model | semantic | sweep |
| A-031 | Rule actuation boundary | Default gateway rules permit explicit supported light operations, temperature reads and read-only security; unknown-room/fuzzy/legacy scene mutations do not bypass whitelist | semantic | default |

## Notes

- A-006: zero transfer cost without bandwidth telemetry is an omitted estimate,
  not a measured zero. Events must record missing fields and estimate source.
- A-007: retain legacy probe EWMA; exact cache/trace observations use `observe`.
  The decision history must distinguish a new observation from another read.
- A-009: fixtures may inject sleeps/failures to expose branches. Preserve injected
  parameters separately from measured request/queue/HTTP time.
