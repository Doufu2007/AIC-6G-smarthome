# SenseHome 国一冲刺升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将 SenseHome 从独立链路策略台架升级为可复现的端到端家庭 AI 卸载场景，并修正实验口径、补齐安全/健康/可复现证据，为场景设计赛道国一竞争准备材料。

**Architecture:** 保留 `offload_policy.c` 作为唯一决策源，在 Python 侧新增 `offload_gateway.py`，统一接收任务、缓存链路探测、调用 C 策略、选择真实或桩后端、执行规则动作并记录统一事件。测量台架改为记录探测、决策、后端和用户总时延；所有对照共享预生成网络 trace。

**Tech Stack:** C99/GCC、Python 3.13 标准库、HTTP JSON、unittest、TCP 链路模拟器、可选 AutoDL OpenAI-compatible 模型服务。

**Spec:** `国一冲刺差距审计与升级方案.md`、`gap_technical_audit.md`、赛事官方附件2场景设计评分标准。

## Global Constraints

- 真实模型未运行前，任何桩耗时必须标记为 STUB，不能进入“模型实测”结论。
- 场景方案必须区分实测、设计目标和未验证；不能声称真实 6G 空口或真实家庭试点已完成。
- `offload_policy.c` 是唯一策略权威实现，Python 不得复制决策表。
- 安全关键任务必须始终经过端侧规则和白名单校验。
- 当前工作区已有未提交材料，修改应保留现有文档、原始运行 JSON 和合规文件。
- Windows 中文路径的 GCC 编译必须支持 ASCII 临时目录回退。

## Review Focus

- 每次请求的总用户时延必须包含链路探测和策略决策成本。
- 自适应和静态对照必须使用同一预生成网络 trace。
- 策略选择必须实际影响后端和设备动作，而不是只在独立 CLI 中输出。
- 模型进程不可用、超时、异常 JSON 和越权动作必须安全回退并留痕。
- 事件日志必须能重放并区分接口成功率与语义完成率。

---

### Task 1: 建立 P0 测试入口和失败基线

**Files:**
- Create: `SenseHome-6G/net_offload/tests/test_offload_gateway.py`
- Create: `SenseHome-6G/net_offload/tests/test_latency_accounting.py`
- Create: `SenseHome-6G/net_offload/tests/__init__.py`

**Interfaces:**
- Tests consume the planned `OffloadGateway`, `BackendResult`, `LinkSnapshot`, and `LatencyBreakdown` interfaces.
- Tests produce the exact behavior contract used by Tasks 2–4.

- [x] **Step 1: Write failing tests** for task classification, safe fallback, event fields, and total latency accounting.
- [x] **Step 2: Run `python -m unittest discover -s SenseHome-6G/net_offload/tests -v` and verify import/attribute failures.**
- [ ] **Step 3: Commit only the failing tests.** — 未执行；当前仅交付本地改动与审阅快照，保留既有未提交内容，不做历史改写。

### Task 2: Add the real gateway integration layer

**Files:**
- Create: `SenseHome-6G/net_offload/offload_gateway.py`
- Modify: `openvela_smart_home/dashboard/bridge_server.py`
- Modify: `openvela_smart_home/dashboard/web_panel.html`

**Interfaces:**
- `OffloadGateway.handle(command: str) -> dict` returns `event_id`, `task_class`, `backend`, `reason`, `link`, `compute`, `latency`, `semantic_complete`, `actions`, and `error`.
- `OffloadGateway.health() -> dict` returns backend health and cached link state.
- Dashboard `/api/command` delegates to the gateway and exposes the same event JSON.

- [x] **Step 1: Implement the smallest gateway that starts `policy_cli`, caches link probes, routes simple/safety tasks locally, and records JSON events.**
- [x] **Step 2: Add real backend adapters for HTTP JSON endpoints with timeout, schema validation, health checks, and safe fallback.**
- [x] **Step 3: Add gateway tests for normal, WAN degraded, WAN down, LAN down, backend timeout, malformed model output, and unsafe action rejection.**
- [x] **Step 4: Wire Dashboard command handling and display backend/reason/latency fields.**
- [x] **Step 5: Run the gateway unittest suite and the existing 70 C policy tests.**

### Task 3: Correct latency accounting and paired traces

**Files:**
- Modify: `SenseHome-6G/net_offload/offload_bench.py`
- Modify: `SenseHome-6G/net_offload/link_emulator.py`
- Create: `SenseHome-6G/net_offload/network_trace.py`
- Modify: `SenseHome-6G/net_offload/results/results.md`
- Modify: `SenseHome-6G/net_offload/results/results.json`

**Interfaces:**
- `LatencyBreakdown` fields: `probe_ms`, `policy_ms`, `backend_ms`, `total_user_ms`.
- `NetworkTrace` provides deterministic per-request conditions to both arms.

- [x] **Step 1: Add failing accounting tests proving probe cost is included in total user latency.**
- [x] **Step 2: Add deterministic trace generation/replay with seed and JSON serialization.**
- [x] **Step 3: Measure probe, policy, backend and total user latency separately.**
- [x] **Step 4: Regenerate results without claiming “4.2x” for total user latency.**
- [x] **Step 5: Verify static and adaptive arms replay identical trace IDs.**

### Task 4: Add compute health, recovery and security evidence

**Files:**
- Modify: `SenseHome-6G/net_offload/offload_policy.h`
- Modify: `SenseHome-6G/net_offload/offload_policy.c`
- Modify: `SenseHome-6G/net_offload/policy_cli.c`
- Modify: `SenseHome-6G/net_offload/test_offload_policy.c`
- Create: `SenseHome-6G/net_offload/tests/test_gateway_resilience.py（另有独立 timeout/action 回归）`

**Interfaces:**
- `compute_state_t` contains `model_ready`, `queue_ms`, `free_mem_mb`, and `inflight`.
- Policy CLI accepts compute-health observations and emits a reason code when a model is unavailable or overloaded.

- [x] **Step 1: Add failing C tests for model unavailable, queue saturation, and safe fallback.**
- [x] **Step 2: Implement compute state and policy integration while preserving safety/simple hard rules.**
- [x] **Step 3: Add Python fault/security tests for timeout, process death, malformed JSON, prompt injection, and unsafe device actions.**
- [x] **Step 4: Run all C and Python tests.**

### Task 5: Add reproducible build and evidence checks

**Files:**
- Create: `SenseHome-6G/net_offload/build.ps1`
- Create: `SenseHome-6G/net_offload/run_evidence.ps1`
- Create: `SenseHome-6G/net_offload/tests/test_evidence_schema.py`
- Modify: `提交材料差距审计.md`

- [x] **Step 1: Write failing schema tests for result metadata, trace IDs, total latency fields, and source labels.**
- [x] **Step 2: Implement ASCII TEMP/TMP fallback, compiler/version metadata, schema validation, and one-command evidence run.**
- [x] **Step 3: Run current-source build, C tests, Python tests, and evidence generation.**

### Task 6: Update competition materials from verified outputs

**Files:**
- Modify: `SenseHome-6G/作品方案_场景设计_v1.md`
- Modify: `演示视频_分镜脚本_v1.md`
- Create: `答辩PPT_页面大纲.md`
- Modify: `提交材料差距审计.md`

- [x] **Step 1: Replace invalid “4.2x user latency” and unsupported slice claims with verified wording.**
- [x] **Step 2: Add end-to-end gateway screenshots/log references, real-model status, trace method and limitations.**
- [x] **Step 3: Add final PPT page outline and 4-minute video sequence.**
- [ ] **Step 4: Run脱敏、数字一致性、PDF/视频格式检查 and list any remaining unverifiable claims.** — 方案PDF、脱敏、数字门已通过；最终视频/PPT文件尚未制作，格式检查待成品生成。



## 2026-11-03 执行检查点

任务1—5的本地实现与验证已落地，任务6的事实源MD、Word/PDF、PPT大纲和视频分镜已更新。最终验证为C 70+17条断言、Python 68+15项测试、数字门3项回归全通过；n=1一键烟测通过，不替代已锁定n=20完整运行。

当前源码与历史实验版本的边界、真实模型/真实网络/目标硬件待补证、最终成片和报名上传缺口，详见 docs/execution/2026-11-03-execution-checkpoint.md 与 提交材料差距审计.md。没有把所有任务或国一目标标为已完成。
