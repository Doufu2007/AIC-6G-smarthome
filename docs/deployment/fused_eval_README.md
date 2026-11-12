# 本机真实模型四臂对照

运行器 `fused_eval.py` 使用现有 `ResourcePolicyClient` → `policy_cli` C 进程 → `OffloadGateway` → 真实 HTTP 模型 → 既有设备白名单执行器。没有复制 Python 决策表，也没有使用合成模型答案。四臂 `fixed-local` / `fixed-cloud` / `network-only` / `joint` 分别映射 C 原生 `static-local` / `static-cloud` / `network-only` / `joint`。网关对简单及安全任务在四臂统一强制本地规则，比较复杂规划时须看 `by_task_scope.complex_planning`。

本机端点是两个已运行的共享 llama.cpp b11010 服务：Qwen2.5-1.5B-Instruct-Q4_K_M 与 Qwen3-4B-Q4_K_M。第二端点仅占用策略的 cloud 槽位，实际仍在同一台电脑、同一 RTX 4050 Laptop 6GB 上。运行器不启动、重启或修改模型服务。设备均为隔离临时文件中的 JSON 模拟，无物理执行器；模型清单 HTTP 的墙钟时长仅作可达性/粗粒度路由输入，不能写成实际 WAN、空口 RTT、吞吐或 6G 测量。

从仓库根目录一条命令运行原20条冻结集（新输出目录不得已有 `rows.jsonl`）：

```powershell
python docs/deployment/fused_eval.py --local-upstream-url http://127.0.0.1:62352/v1 --local-model Qwen2.5-1.5B-Instruct-Q4_K_M --cloud-upstream-url http://127.0.0.1:42354/v1 --cloud-model Qwen3-4B-Q4_K_M --max-tokens 256 --timeout-s 60 --repeat 3 --out docs/experiments/20261111_fused_real/rerun_reviewed_01
```

默认先执行6次非评测 warmup，保存 `calibration.json`，用各模型 HTTP 实测中位数更新 `local_infer_ms` / `cloud_infer_ms`。其余 C 阈值来自既有设计配置，不冒充实测。warmup 和评测分别计数。`--calibration-file <calibration.json>` 可冻结并复用已保存校准；正式结果的校准原始行包括模型保护拒绝，模型确实产生响应即保留其推理时长。共享 GPU、其他进程和缓存都可能影响时延，结果只适用于本次本机共享环境。

只读健康探测在相同命令上增加 `--probe-only`，仅向直接模型端点请求 `GET /v1/models`。也支持已有 SenseHome 适配器的 `--local-url http://127.0.0.1:18091` / `--cloud-url ...`（`GET /health`、`POST /infer`）；wrapper 内部未暴露的原始模型文本会标为不可用。不能同时给同一槽位指定 wrapper 和 direct upstream。缺少第二端点时 `fixed-cloud` 留存 `skipped` 行，不伪造云端模型输出。非回环端点地址在报告中脱敏，API key 仅从指定环境变量读取，不保存 Authorization。

12条组合挑战集已在任何挑战推理前冻结于 `docs/experiments/20261111_fused_real/challenge_frozen/`，SHA256 `908c302a1e50a892301773d82ce1e0687086f6d73abba1c0de83e811bf74a9b0`。标签为 `programmatically_derived_not_human_reviewed`；它是房间×动作×数值组合、歧义、否定和越权的接口检查，不能称人工独立保留集，也不能证明真实用户分布上的泛化。首次 `challenge_12_final_repeat3/` 暴露歧义泛称设备和不支持设备复合请求的部分执行缺口，随后已用于开发保护规则；**之后所有运行均为 development regression，不能再称未见泛化**。原始冻结文件、首次推理与旧源码快照保留，标签未改。

```powershell
python docs/deployment/fused_eval.py --local-upstream-url http://127.0.0.1:62352/v1 --local-model Qwen2.5-1.5B-Instruct-Q4_K_M --cloud-upstream-url http://127.0.0.1:42354/v1 --cloud-model Qwen3-4B-Q4_K_M --max-tokens 256 --timeout-s 60 --repeat 3 --cases docs/experiments/20261111_fused_real/challenge_frozen/challenge.programmatic.jsonl --allow-draft-labels --dataset-use-scope development_regression --calibration-file docs/experiments/20261111_fused_real/reviewed_20_final_repeat3/calibration.json --out docs/experiments/20261111_fused_real/rerun_challenge_01
```

受控故障矩阵只选人工集 C01/C04 两条复杂命令，分别注入本地模型未就绪、第二模型未就绪、健康观测过期，共2×4×3×3=72行。注入的是策略观测，模型进程始终真实可用；因此可检查路由/安全退化，不能据此宣称真实断网、硬件故障或拥塞的性能收益。每块原始健康响应与覆盖字段分别保存。

```powershell
python docs/deployment/fused_eval.py --local-upstream-url http://127.0.0.1:62352/v1 --local-model Qwen2.5-1.5B-Instruct-Q4_K_M --cloud-upstream-url http://127.0.0.1:42354/v1 --cloud-model Qwen3-4B-Q4_K_M --max-tokens 256 --timeout-s 60 --repeat 3 --case-ids C01,C04 --conditions-file docs/experiments/20261111_fused_real/controlled_conditions.json --calibration-file docs/experiments/20261111_fused_real/reviewed_20_final_repeat3/calibration.json --out docs/experiments/20261111_fused_real/rerun_conditions_01
```

每条指令每次重复使用相同命令、上下文、初始状态和块内冻结健康观测；四臂顺序按指令索引及重复轮换，保留实际顺序。共享的只读健康探测在块级单独计时，不向每臂重复摊入。`gateway_wall_ms` 包含真实 C 决策、后端和模拟执行，`blocks.jsonl` 另存健康探测时长。未观测的模型排队、空闲内存、抖动、丢包和带宽保留 null，绝不填健康零值。

产物可直接审阅：

- `rows.jsonl`：每臂逐行保存请求、HTTP状态/原始响应、保护前模型动作、归一化动作、网关最终动作、保护拒绝、失败/回退、模拟执行前后状态、时延和调用次数。即时追加，失败不丢失。
- `blocks.jsonl`：块顺序、只读健康响应、观测来源、注入条件和公共探测时长。
- `report.json` / `summary.json` / `summary.md`：标签来源、配置、模型身份来源、源码哈希和分组摘要。
- `source_snapshot/`：当轮实际加载的运行器、网关、适配器、C策略源码、二进制及模拟执行器快照。二进制哈希不等于可复现构建证明；模型文件 revision 未独立提供时仍为 unknown。

计分必须区分：真实模型 HTTP 成功、原始动作匹配、归一化动作匹配、网关最终结构匹配、保护拒绝、非空任务执行完成。状态恰好相符只作诊断，初态已达目标不代表命令成功；非空完成还须动作/状态匹配、语义和执行确认，且没有错误或回退。`actions=[]` 只算无动作结构匹配，澄清/拒绝回答文字未经人工审核，不计“正确澄清”。来源分别汇总，不能把规则、真实模型、测试fixture、失败回退混成一个模型准确率。

历史 `reviewed_20_repeat3/` 保留修复前240行，`reviewed_20_prefix_rescored/` 只对旧行用新版计分后处理，明确没有新推理；`reviewed_20_postfix_repeat3/`、`reviewed_20_final_repeat3/` 是开发过程中的中间轮。封版生产版本使用 `reviewed_20_sealed_repeat3/`、`challenge_12_sealed_development_repeat3/`、`controlled_faults_sealed/` 的快照，不能把旧轮追认成封版源码实测。

首次 `preflight_local.json` 因 PowerShell 临时脚本管道编码而把中文传成问号，明确排除正式评测与校准。后续直接运行 UTF-8 Python 文件没有该问题。有效校准的实际提示词约600 token，保留 upstream usage 原始值，能在既有 ctx2048 的两个模型中运行；共享模型服务配置未改。

```powershell
python -m unittest discover -s docs/deployment -p fused_eval_tests.py -v
python -m unittest discover -s SenseHome-6G/net_offload/tests -v
python -m unittest discover -s openvela_smart_home/dashboard/tests -v
```

重复请求相关，禁止按独立用户样本做显著性宣称。配对时延仅描述同输入、同观测且动作均正确的真实模型行；真实请求数、块数、非空任务数和保护拒绝分开报告。

另补一次在生产/提示词/配置冻结后注册的程序标签组合留出检查：固定seed `2026111126`，4条数值跨房组合、2条开关跨房组合、2条双房只读、2条歧义、2条不支持设备混合请求。所有原文与旧20条、旧12条不同；数据SHA256为`7fcd602bd87d631223ddc17008fca022f42f995e38996e230906c85197e9f2a1`。生成器`fused_eval_generate_holdout.py`只创建标签与注册文件，不发推理；`holdout_12_preregistered/registration.json`在模型运行前锁定生成规则、数据、11项源文件与复用校准哈希。结果保存在`holdout_12_programmatic_repeat3/`，只运行一轮四臂三重复，不依据结果修复或调参；它只能称“预注册程序标签组合留出检查”，不能称人工或真实用户语言泛化。

该留出集的审计复现命令如下；本次任务仅实际运行一次，新的审计重跑应另用新输出目录并明示重跑性质：

```powershell
python docs/deployment/fused_eval.py --local-upstream-url http://127.0.0.1:62352/v1 --local-model Qwen2.5-1.5B-Instruct-Q4_K_M --cloud-upstream-url http://127.0.0.1:42354/v1 --cloud-model Qwen3-4B-Q4_K_M --max-tokens 256 --timeout-s 60 --repeat 3 --cases docs/experiments/20261111_fused_real/holdout_12_preregistered/holdout.programmatic.jsonl --allow-draft-labels --dataset-use-scope programmatic_contract_check --calibration-file docs/experiments/20261111_fused_real/reviewed_20_final_repeat3/calibration.json --out docs/experiments/20261111_fused_real/audit_holdout_rerun_01
```
