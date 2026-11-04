# SenseHome 链路自适应推理卸载：最新四档链路验证结果

> 生成时间：2026-11-03 03:57:04；每档每任务 n=20。
> 用户总时延 = 探测耗时 + 策略耗时 + 后端耗时；`backend_e2e_ms` 仅保留后端段原始耗时。
> 网络与策略路径来自本机 TCP 台架实测；模型推理耗时是注入的桩参数，不能解释为真实 GPU/模型测量。
> 语义字段来自合成任务合同；local-rules 的小循环仅为计时代理，不能代表完整 Dashboard 规则能力。
> 本结果不声称真实 5G/6G 空口、真实家庭试点或网络切片已完成。

## 桩参数与复现实验

| 项目 | 值 | 状态 |
|---|---:|---|
| cloud model compute | 300 ms | 注入桩参数 |
| gateway model compute | 150 ms | 注入桩参数 |
| paired traces | 每档 adaptive/static 共用同一 trace_id | 可重放 |

## 表 A：按任务的用户总时延与完成率

| 档位 | 臂 | 任务 | 后端 | 接口成功率 | 语义合同完成率 | 探测中位 ms | 策略中位 ms | 后端中位 ms | 用户总时延中位 ms | n |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| P1 链路正常 | adaptive | simple（结构化） | local-rules | 100% | 100% | 0 | 0.07 | 0.01 | 0.08 | 20 |
| P1 链路正常 | adaptive | complex（复杂） | cloud-model | 100% | 100% | 62.7 | 0.09 | 338.8 | 405.6 | 20 |
| P1 链路正常 | adaptive | safety（安全） | local-rules | 100% | 100% | 0 | 0.05 | 0.01 | 0.06 | 20 |
| P1 链路正常 | static-cloud | simple（结构化） | cloud-model | 100% | 100% | 0 | 0 | 350.7 | 350.7 | 20 |
| P1 链路正常 | static-cloud | complex（复杂） | cloud-model | 100% | 100% | 0 | 0 | 346.4 | 346.4 | 20 |
| P1 链路正常 | static-cloud | safety（安全） | cloud-model | 100% | 100% | 0 | 0 | 347.6 | 347.6 | 20 |
| P2 WAN 劣化 | adaptive | simple（结构化） | local-rules | 100% | 100% | 0 | 0.03 | 0.01 | 0.04 | 20 |
| P2 WAN 劣化 | adaptive | complex（复杂） | local-model | 100% | 100% | 425.5 | 0.09 | 169.1 | 596.2 | 20 |
| P2 WAN 劣化 | adaptive | safety（安全） | local-rules | 100% | 100% | 0 | 0.05 | 0.01 | 0.06 | 20 |
| P2 WAN 劣化 | static-cloud | simple（结构化） | cloud-model | 90% | 90% | 0 | 0 | 707.6 | 707.6 | 20 |
| P2 WAN 劣化 | static-cloud | complex（复杂） | cloud-model | 95% | 95% | 0 | 0 | 725.6 | 725.6 | 20 |
| P2 WAN 劣化 | static-cloud | safety（安全） | cloud-model | 85% | 85% | 0 | 0 | 716.7 | 716.7 | 20 |
| P3 WAN 断链（LAN 尚存） | adaptive | simple（结构化） | local-rules | 100% | 100% | 0 | 0.03 | 0.01 | 0.04 | 20 |
| P3 WAN 断链（LAN 尚存） | adaptive | complex（复杂） | local-model | 100% | 100% | 22 | 0.09 | 171.7 | 195 | 20 |
| P3 WAN 断链（LAN 尚存） | adaptive | safety（安全） | local-rules | 100% | 100% | 0 | 0.05 | 0.01 | 0.05 | 20 |
| P3 WAN 断链（LAN 尚存） | static-cloud | simple（结构化） | cloud-model | 0% | 0% | 0 | 0 | 13.5 | 13.5 | 20 |
| P3 WAN 断链（LAN 尚存） | static-cloud | complex（复杂） | cloud-model | 0% | 0% | 0 | 0 | 1.16 | 1.16 | 20 |
| P3 WAN 断链（LAN 尚存） | static-cloud | safety（安全） | cloud-model | 0% | 0% | 0 | 0 | 14.4 | 14.4 | 20 |
| P4 全网断链 | adaptive | simple（结构化） | local-rules | 100% | 100% | 0 | 0.08 | 0.01 | 0.09 | 20 |
| P4 全网断链 | adaptive | complex（复杂） | local-rules | 100% | 0% | 29.5 | 0.08 | 0.02 | 29.6 | 20 |
| P4 全网断链 | adaptive | safety（安全） | local-rules | 100% | 100% | 0 | 0.07 | 0.01 | 0.08 | 20 |
| P4 全网断链 | static-cloud | simple（结构化） | cloud-model | 0% | 0% | 0 | 0 | 14.6 | 14.6 | 20 |
| P4 全网断链 | static-cloud | complex（复杂） | cloud-model | 0% | 0% | 0 | 0 | 6.46 | 6.46 | 20 |
| P4 全网断链 | static-cloud | safety（安全） | cloud-model | 0% | 0% | 0 | 0 | 1.02 | 1.02 | 20 |

## 表 B：自适应复杂 NLU 的档位对比

| 档位 | 后端 | 接口成功率 | 语义合同完成率 | 用户总时延中位 ms | 探测中位 ms | 后端中位 ms |
|---|---|---:|---:|---:|---:|---:|
| P1 链路正常 | cloud-model | 100% | 100% | 405.6 | 62.7 | 338.8 |
| P2 WAN 劣化 | local-model | 100% | 100% | 596.2 | 425.5 | 169.1 |
| P3 WAN 断链（LAN 尚存） | local-model | 100% | 100% | 195 | 22 | 171.7 |
| P4 全网断链 | local-rules | 100% | 0% | 29.6 | 29.5 | 0.02 |

## 表 C：可追溯性与限制

- 每个阶段的 adaptive/static-cloud 两臂共享 `paired_traces` 中的同一 `trace_id`，可按 seed 重放。
- `interface_ok` 表示收到后端响应；当前 `semantic_complete` 由桩合同给出。全网断链时合成 complex 的接口仍可用，但合同完成率为 0。真实命令语义必须单独评测。
- 当前最大额外成本是劣化档探测；后续优化方向是异步探测缓存和真实模型复测。
- 真实模型、真实 GPU、真实 5G/6G 空口、真实家庭试点和网络切片均属于待验证项。

## 复现命令

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File SenseHome-6G/net_offload/run_evidence.ps1 -Out _work/new_evidence_run -InferN 20 -TraceSeed 20261103
python -m unittest discover -s SenseHome-6G/net_offload/tests -v
```
