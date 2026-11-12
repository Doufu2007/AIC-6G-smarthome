# SenseHome：网络—算力感知的家庭 AI 推理卸载

本目录是“6G 链路自适应的家庭 AI 推理卸载系统”场景方案的可复核代码。融合后的 C 策略同时考虑 WAN/LAN 链路观测、本地与云端模型就绪状态、队列/并发/内存约束和可估算的传输成本，再由 Python 网关传递服务端观测、调用后端、执行动作白名单、回退并记录策略元数据。

双端策略的 C 源码、网关适配和实验流水线已完成融合版 r3 构建、全量测试与完整实验审计。验证记录及运行产物见 `../evidence/network_compute/20261111_fused/`；归档副本与验证目录的 328 个运行文件逐项 SHA256 一致。已有 n=20 性能表仍属于旧网络策略历史基线。新实验使用合成轨迹、固定规划合同、本机桩服务和 PC 模拟设备，未接入真实 6G 空口、物理设备或家庭试点。

## 可复核边界

| 项目 | 当前证据 | 不能推出的结论 |
|---|---|---|
| C 策略 | `offload_policy.c`、C 单元测试、`policy_cli` | 不能推出真实无线网络性能 |
| WAN/LAN 台架 | TCP 中转、固定逐请求 trace、raw runs | 不能替代 5G/6G 空口、切片或确定性承载 |
| 模型后端 | `backends/stub_backend.py` 注入 `300/150 ms` | 不能推出模型准确率、GPU 推理时间或模型优劣 |
| Dashboard 控制 | 真实 Python 网关、动作白名单、PC JSON 状态 | 不能替代 MCU、继电器、燃气阀或门禁联锁 |
| 真实模型包 | `backends/openai_backend.py` 与 AutoDL 验证说明 | 未配置实例和模型前不计入准确率 |
| 双端策略新实现 | C 端同时读入 WAN/LAN 观测与 local/cloud 健康度；过期观测闭锁；带宽未知时传输成本保持未知；成本比较带切换迟滞 | r3 构建、回归和新矩阵审计通过；其性能数字仅对应合成轨迹和固定合同 |

## 旧网络策略基线的决策边界

| 任务 | WAN/LAN 状态 | 选择 | 原因 |
|---|---|---|---|
| `safety` | 任意 | `local-rules` | `TASK_IS_SAFETY` |
| `simple` | 任意 | `local-rules` | `TASK_IS_SIMPLE` |
| `complex` | WAN 达标、LAN 可用 | `cloud-model` | `WAN_OK` |
| `complex` | WAN 劣化、LAN 可用 | `local-model` | `WAN_*_HIGH` |
| `complex` | WAN 不可达、LAN 可用 | `local-model` | `WAN_UNREACHABLE` |
| `complex` | LAN 不可用 | `local-rules` | `LAN_DOWN` |

`offload_policy.c` 是策略唯一实现；Python 只做输入校验和行协议适配。新资源策略只有在 `resource_cfg` 被配置时启用；现存 `results/results.json` 仍来自旧策略路径。对于 `simple`/`safety`，网关固定留在 `local-rules`；复杂请求使用带来源和年龄的服务端观测。

## 双端资源策略与新验证流程

新策略先拒绝安全/结构化任务的远端路由，再检查 local/cloud 两端模型就绪、队列、并发和可用内存。只有两侧网络或传输估算都有观测时，才把相应项纳入代价比较；未知值不会被当成零。切换裕量与连续恢复样本用于减少边界抖动。观测过期、乱序或两端都无模型容量时，复杂任务退回规则路径并留下明确原因码。

`experiments/` 中的服务画像、策略配置和合成 trace 驱动 replay、并发、敏感性、安全与 HTTP 网关桩实验。完整 r3 运行及审计通过，结果归档于 `../evidence/network_compute/20261111_fused/`。这些实验验证策略在声明输入下的行为，不构成真实 GPU、家庭设备或 6G 无线性能测量。ZIP 提供的原始补充资料保存在 `../evidence/network_compute/20261106/`，用于追溯，不替代本仓库的重跑结果。

融合版 replay 共 58,800 条记录。`interactive_inference` 中，joint 完成固定规划合同且满足时限的比例为 84.40%（4,254/5,040），network-only 为 62.98%，compute-only 为 61.90%，no-hysteresis 为 84.52%；joint 有 786 条 `UNRESOLVED_NO_NLU`。由于 no-hysteresis 略高，不能声称迟滞提高了时限达标率。HTTP 网关桩共 9,084 个请求和持久化事件，679 条合同未完成（383 条 `CONTRACT_NOT_COMPLETED`、296 条注入下游 `HTTP_503`），外层响应均为 HTTP 200。注入 503 不代表真实网络丢包。

## 旧网络策略历史基线

旧网络策略的权威完整台架结果在 `results/results.json`，原始运行副本在 `_work/evidence_final/`。每个档位包含三类任务，每类 20 次，adaptive/static-cloud 两臂共 480 条业务记录；`raw_runs` 保存逐请求行，`paired_traces` 保存实际 WAN/LAN probe/infer 结果。该表仅是历史基线。双端资源策略必须另行构建、运行并归档，不得覆盖或冒认这组旧结果。

复杂任务四档的旧基线结果如下。用户总时延为 `probe_ms + policy_ms + backend_ms` 的全部请求中位数；桩语义标志只代表合同路径，不代表真实语言理解。

| 档位 | 臂 | 接口 | 语义合同 | 用户总时延 ms | 探测 ms | 后端 ms |
|---|---|---:|---:|---:|---:|---:|
| P1 正常 | adaptive | 20/20 | 20/20（桩） | 405.6 | 62.7 | 338.8 |
| P1 正常 | static-cloud | 20/20 | 20/20（桩） | 346.4 | 0 | 346.4 |
| P2 劣化 | adaptive | 20/20 | 20/20（桩） | 596.2 | 425.5 | 169.1 |
| P2 劣化 | static-cloud | 19/20 | 19/20（桩） | 725.6 | 0 | 725.6 |
| P3 WAN 断 | adaptive | 20/20 | 20/20（桩） | 195.0 | 22.0 | 171.7 |
| P3 WAN 断 | static-cloud | 0/20 | 0/20 | 1.16（失败返回） | 0 | 1.16 |
| P4 全断 | adaptive | 20/20 | 0/20 | 29.6 | 29.5 | 0.02 |
| P4 全断 | static-cloud | 0/20 | 0/20 | 6.46（失败返回） | 0 | 6.46 |

另有独立 Dashboard 证据：四档各 20 次开关灯和只读安防状态查询，共 160 次，均为 PC 模拟 JSON 状态。开关灯 p95 最大 4.434 ms，安防查询 p95 最大 1.711 ms；这两个数字只适用于已授权的演示动作范围。

## 目录说明

| 文件 | 作用 |
|---|---|
| `offload_policy.c/.h` | 纯 C99 策略与链路状态估计 |
| `policy_cli.c` | 常驻行协议适配，供网关和台架调用 |
| `offload_gateway.py` | 后端选择、超时/异常回退、动作执行与事件记录 |
| `link_emulator.py`、`network_trace.py` | TCP 台架与确定性逐请求回放 |
| `offload_bench.py` | 生成 raw runs、summary、trace 和恢复轨迹 |
| `evidence.py` | 结果包 schema 与配对关系检查 |
| `backends/stub_backend.py` | 仅用于本地合同和注入耗时，不能当真实模型 |
| `backends/openai_backend.py` | AutoDL/vLLM/OpenAI 兼容 wrapper，默认不启用 |
| `tests/` | 网关、动作白名单、回退、真实 wrapper 假上游测试 |
| `build.ps1`、`run_evidence.ps1` | 编译、完整测试、证据生成与 manifest |

## 运行

在 Windows PowerShell 中：

```powershell
cd SenseHome-6G/net_offload
./build.ps1
./run_evidence.ps1 -Out ./results -InferN 20 -TraceSeed 20261103
python ../../_work/verify_numbers.py
```

`build.ps1` 会编译三个 C 程序并运行 net_offload 与 Dashboard Python 测试，随后写入 `build_manifest.json`。本次完整 r3 验证使用 Zig 0.17.1 的 `cc` 前端，构建、测试、完整矩阵和审计日志均已归档；精确命令与编译器哈希见归档中的 `validation/verification.json`。`run_evidence.ps1` 生成旧链路台架；双端策略使用 `run_experiment_suite.py --out <新空目录> --compiler <compiler>`，必须保存完整原始输出和审计结果。

若使用便携 Zig，可通过 `--compiler <zig.exe>` 指定；运行器会调用其 `cc` 前端。先用 `--quick` 做接线烟测，完整矩阵另选空目录运行并保留原始证据。

快速查看 JSON：

```powershell
python render_evidence_report.py --input results/results.json --output results/results.md
python ../../_work/check_draft.py
```

## 真实模型补验

先阅读 `AutoDL_真实模型验证.md`。需要一个可访问的 AutoDL/vLLM 或 OpenAI 兼容实例、模型名称/版本、GPU 与运行时记录、冻结并人工复核的命令标签。验收至少保存逐条命令、模型原始输出、动作白名单结果、接口成功率、语义正确率、p50/p95、模型 HTTP 墙钟和失败原因。`semantic_complete` 由本地评测和动作确认决定，不能信任模型自报；没有这些材料时，stub 结果不能写成真实模型成绩。

## 已知限制与下一步

- 目标 MCU、网关内存/能耗/成本、真实设备并发仍未测量。
- 当前网络是 TCP 链路注入，不含无线协议栈、分组重传、切换、切片或确定性承载。
- Dashboard 只验证开关灯和只读安防查询，未授权燃气、门禁等动作必须拒绝。
- 旧网络策略复杂任务的同步探测成本仍可能超过原始 KPI 目标；本次融合版已完成合成 trace 的时延/策略矩阵、并发、敏感性和恢复相关测试，但不能据此推出真实模型服务或真实网络下的端到端收益。固定规划合同、合成观测和本机 HTTP 桩限定了当前结果的外推范围。
- 真实模型、真实网络、集成商需求访谈和授权家庭试点均需单独补证。

提交前请同时检查作品方案中的脱敏字段、许可证/NOTICE、模型与第三方资源披露，以及 Word/PDF/视频/PPT 是否与当前 JSON 事实源一致。
