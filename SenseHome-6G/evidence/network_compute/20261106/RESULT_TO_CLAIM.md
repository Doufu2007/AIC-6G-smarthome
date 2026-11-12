# Result-to-Claim Mapping

Date: 2026-11-06. Bounded claim support: **yes**. Integrity: **warn**.
Semantic review: expected/configured same-family, **provisional**.
Configured reviewer is `gpt-6.1-sol/xhigh`; runtime-attested model is unknown.

| Claim | Decision | Required Scope |
|---|---|---|
| C1. Default entry consumes both endpoints and invokes C | Supported | Default software HTTP path with recorded fixture/operator overrides. |
| C2. Faults change routes with reasons | Supported | Declared synthetic/application faults and endpoint observations, not real radio traces. |
| C3. Rules and models retain structured whitelist | Supported | Schema/action/range/room validation and atomicity on isolated PC state. |
| C4. Joint information affects attainment under declared loads | Supported | Tradeoffs, not uniform superiority; separate modeled/direct-thread/HTTP populations. |
| C5. Counts and quantiles are recomputable | Supported | Raw-statistic and enumerated-source assurance, not independent semantic acceptance. |

Evidence is bound in `EXPERIMENT_AUDIT.json` and the full raw reviewer response.
All five completed bundles pass fresh reviewer mechanical checks. The raw counts
are 58,800 replay, 6,144 direct-thread, 9,360 sensitivity, 28 safety and 9,084 HTTP.
HTTP consists of 2,940 matrix rows and 6,144 stress rows with 9,084 persisted events.

## Counterevidence

Replay complex deadline attainment is joint 4,254/5,040 versus no-hysteresis
4,260/5,040. Direct-thread complex completion is joint 1,789/1,848 versus
network-only 1,848/1,848. HTTP matrix complex deadline attainment is joint 192/252
versus compute-only 210/252. HTTP cloud-down deadline attainment at 32 clients is
joint 208/231 versus network-only 231/231, and at 64 clients 89/231 versus 102/231.

These are retained outcomes, not exclusions. Favorable differences in other
metrics or conditions cannot erase them. The HTTP arms have different measured
health telemetry and do not support paired causal timing claims.

## Remaining Limits

Three synthetic replay seeds and one exploratory sensitivity seed cannot support
field reliability claims. Wilson intervals are descriptive; seed-cluster bootstrap
is exploratory. Fixed sleep services, injected matrix memory/queues and process-local
devices are not real-model/GPU/radio/household observations. Unknown live memory is
not zero. Modeled safety success is not empirical five-nines reliability.

The unchanged-source gate covers 46 enumerated source files. The broader 20-file
test/backend snapshot is correctly labeled post-run, not retroactive execution
binding. The final validation is complete; `BUILD_NOTE.md` was updated during
review to distinguish passed execution from the review/package stage.

No official submission readiness, demand validation, real-world deployment,
full natural-language/physical safety certification or independent external
semantic acceptance follows from this mapping.
