# SenseHome：链路感知的家庭 AI 推理卸载

本目录是“6G 链路自适应的家庭 AI 推理卸载系统”场景方案的可复核代码。它把家庭网关到云端的 WAN、网关到设备/本地模型的 LAN 分开观测，由唯一的 C 策略进程按任务类别和链路状态选择 `local-rules`、`local-model` 或 `cloud-model`，再由 Python 网关完成后端适配、动作白名单、回退和事件记录。

它当前证明的是一条可运行、可回放的软件闭环：网络轨迹、策略决策、后端响应和错误原因可以逐请求复核。模型后端仍是注入耗时的桩，设备状态是 PC 进程内模拟，尚未接入真实 6G 空口、真实物理设备或家庭试点；README 和作品方案均不把这些内容写成已完成部署。

## 可复核边界

| 项目 | 当前证据 | 不能推出的结论 |
|---|---|---|
| C 策略 | `offload_policy.c`、C 单元测试、`policy_cli` | 不能推出真实无线网络性能 |
| WAN/LAN 台架 | TCP 中转、固定逐请求 trace、raw runs | 不能替代 5G/6G 空口、切片或确定性承载 |
| 模型后端 | `backends/stub_backend.py` 注入 `300/150 ms` | 不能推出模型准确率、GPU 推理时间或模型优劣 |
| Dashboard 控制 | 真实 Python 网关、动作白名单、PC JSON 状态 | 不能替代 MCU、继电器、燃气阀或门禁联锁 |
| 真实模型包 | `backends/openai_backend.py` 与 AutoDL 验证说明 | 未配置实例和模型前不计入准确率 |

## 策略边界

| 任务 | WAN/LAN 状态 | 选择 | 原因 |
|---|---|---|---|
| `safety` | 任意 | `local-rules` | `TASK_IS_SAFETY` |
| `simple` | 任意 | `local-rules` | `TASK_IS_SIMPLE` |
| `complex` | WAN 达标、LAN 可用 | `cloud-model` | `WAN_OK` |
| `complex` | WAN 劣化、LAN 可用 | `local-model` | `WAN_*_HIGH` |
| `complex` | WAN 不可达、LAN 可用 | `local-model` | `WAN_UNREACHABLE` |
| `complex` | LAN 不可用 | `local-rules` | `LAN_DOWN` |

`offload_policy.c` 是策略唯一实现；Python 不复制决策表。对于 `simple`/`safety`，网关不做同步链路探测，避免把本地支持路径的探测时间误算为云端成本。复杂请求才按台架轨迹探测并执行模型后端。

## 当前权威证据

权威完整台架结果在 `results/results.json`，原始运行副本在 `_work/evidence_final/`。每个档位包含三类任务，每类 20 次，adaptive/static-cloud 两臂共 480 条业务记录；`raw_runs` 保存逐请求行，`paired_traces` 保存实际 WAN/LAN probe/infer 结果。`results/results.md` 由 JSON 渲染，不手工维护数字。

复杂任务四档的当前结果如下。用户总时延为 `probe_ms + policy_ms + backend_ms` 的全部请求中位数；桩语义标志只代表合同路径，不代表真实语言理解。

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

`build.ps1` 会编译三个 C 程序并运行 net_offload 与 Dashboard Python 测试，随后写入 `build_manifest.json`。`run_evidence.ps1` 生成新的台架结果并做 schema 检查；正式材料使用已锁定的完整运行目录，不应在没有保存新 raw runs 的情况下手改数字。

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
- 复杂任务的同步探测成本在当前四档中仍可能超过原始 KPI 目标；下一步是引入有有效期的观测缓存，并用真实模型重新测 p50/p95。
- 真实模型、真实网络、集成商需求访谈和授权家庭试点均需单独补证。

提交前请同时检查作品方案中的脱敏字段、许可证/NOTICE、模型与第三方资源披露，以及 Word/PDF/视频/PPT 是否与当前 JSON 事实源一致。
