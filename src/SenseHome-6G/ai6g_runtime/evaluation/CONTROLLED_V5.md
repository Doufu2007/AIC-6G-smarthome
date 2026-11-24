# v5：资源路由公平实验接口

本版本与 `controlled.py` 的 v4 记录独立。它复用产品 `TaskRuntime`、`TaskRouter`、协议校验和软件设备状态库；每个策略保留一个生产路由器，跨任务保存滞回状态，并在日志断点恢复时还原。不会据路由标签宣称独立端、边、云硬件。

## 策略与共同约束

9 个默认策略：`rules`、`fixed_local`、`legal_cloud`、`static`、`fixed_ttl`、`adaptive`、`full_resource`、`no_resource`、`no_hysteresis`。

- 所有策略共享规则和安全快速路径、相同隐私信任域、协议、执行器、任务顺序与故障步骤；无隐藏重试。
- `adaptive` 是原 TTL 自适应强基线，禁止使用资源观测，切换滞回为 0。
- `full_resource` 在相同 TTL 基础上增加新鲜资源容量筛选与默认 10ms 切换滞回。
- `no_resource` 与完整策略仅资源观测不同；`no_hysteresis` 与完整策略仅切换滞回为 0。
- `static` 只信首个探测；`fixed_ttl` 按固定周期探测；探测均占业务 deadline 并计成本。
- 默认主比较为 `full_resource - no_resource`、`full_resource - adaptive` 的按期语义正确率，保留簇配对 CI95，并另给两比较 Bonferroni CI97.5。`no_hysteresis` 为次要消融。区间是描述性估计，不是外部因果保证。

`trusted_backends` 必须按物理部署显式配置。两个模型如果实际都在本机，则可设为 `local-model,cloud-model`，同时说明 `cloud-model` 只是逻辑大模型别名。

## 真实响应采集

先设置 `AI6G_LOCAL_URL`、`AI6G_LOCAL_MODEL`、`AI6G_CLOUD_URL`、`AI6G_CLOUD_MODEL`，URL 是完整 `/v1/chat/completions` 地址。若需要鉴权，可在环境变量中设置 `AI6G_LOCAL_API_KEY`、`AI6G_CLOUD_API_KEY`；密钥不写入 manifest。

```powershell
python SenseHome-6G/ai6g_runtime/evaluation/controlled_v5.py --collect --tasks calibration.jsonl --out calibration_capture.jsonl --trusted-backends local-model,cloud-model --capture-timeout-ms 30000 --identity deployment_identity.json
```

也可调用：

```python
capture_outputs(tasks_path, out_path, backends=backends,
                trusted_backends=("local-model", "cloud-model"),
                timeout_ms=30000, resume=False, include_probes=True,
                stop_after=None, identity_path="deployment_identity.json")
```

输出 `*.jsonl` 和对应 `*.manifest.json`。每模型采集 1 条独立 `__probe__`，再按任务逐条采集双模型响应；gold 不传给模型，strict 任务只允许显式可信部署。JSONL 行包括：

```json
{"schema":"ai6g.model_capture.v5","task_id":"...","backend":"local-model","phase":"task","text_sha256":"...","result":{"ok":true,"payload":{},"error":null,"latency_ms":0,"model_real":true,"backend":"local-model","raw_response":{}},"elapsed_ms":123.0,"skipped":false,"actual_endpoint_dispatch":true,"capture_fingerprint":"...","captured_at":0,"gold_online":false}
```

manifest 绑定任务文件、源码、端点/模型、prompt hash、max_tokens、seed、temperature、JSON 输出约束、信任配置和超时。`--identity` 额外绑定部署身份文件的完整内容及 SHA256，须由部署侧提供实际 GGUF 权重、推理程序哈希、服务参数和 PID；采集开始/恢复前应由操作脚本核对现运行进程与身份文件一致，单靠 URL/alias 不能检测同址换权重。`--resume` 只能用于完全相同指纹，不重复已完成调用。正式采集后不能修改源码再宣称是同一冻结配置。采集中断行损坏须保留原文件另行核对，不自动吞掉未知的已派发调用。

## 受控回放

```powershell
python SenseHome-6G/ai6g_runtime/evaluation/controlled_v5.py --tasks test.jsonl --out result_v5.json --calibration calibration_summary.json --trace resource_trace.jsonl --replay test_capture.jsonl --trusted-backends local-model,cloud-model --deadline-ms 3500
```

`--calibration` 保持 v4 可读格式，每模型包含实际测量的 `quality_score`、`quality_by_family` 和源文件 SHA256；不可借用其他量化或其他硬件的质量值。期限必须在正式测试前冻结。`--limit` 用于小规模冒烟，写入配置指纹；不改变原 gold。在线交叉验证用 `--online` 替代 `--replay`。

回放逐任务消费采集响应和其完整实测耗时，并叠加共同扰动。它执行零次 HTTP 推理：`source_kind=real_output_controlled_replay`，`model_real=false`，原采集标记保留为 `captured_model_real`。`capture_workload` 记录一次实际采集开销；策略 `cost` 是假设按此调度时的逻辑调用、探针、token 与耗时，不是新发生的账单。假设推理中途超时的 token 成本保持未知，不用完整响应的 token 数冒充截断成本；总 token 是已知用量之和，须同时报告 `unknown_usage_requests`。金额始终 unknown，不能把本机电费/折旧当 0。

`invoke_elapsed_ms` 包含服务注入和端点推理；`inference_wall_budget_ms` 是 HTTP 推理墙钟区间（回放时为截止前消耗的采集时长），`injected_service_ms` 单列软件 RTT/排队/传输。历史兼容字段 `model_elapsed_ms` 实际也是完整 invoke 墙钟时长，不能写成 GPU 算力时间。以上均不测 GPU kernel 时间或电量。

单一探针响应的多次回放只是固定探针输出与时延的场景假设；不是多次真实在线探测样本。需要另行真实在线抽样才能评价连续探针时延分布。

## 扰动格式与边界

每行：

```json
{"step":12,"backend":"local-model","rtt_ms":60,"queue_ms":320,"resources":{"queue_ms":320,"free_mem_mb":128,"inflight":9,"source":"injected_resource_scenario","age_ms":0}}
```

`rtt_ms/queue_ms/bandwidth_kbps/reachable/disconnect_ms` 是服务侧注入；`resources` 是路由可见的独立观测，可以与服务真实状态不同，用来测缺失/陈旧/误差。`resources=null` 清除观测，`age_ms=null` 为未知时间戳；`age_ms` 超过 `resource_ttl_ms` 时不可据此剔除后端。各字段缺失保持未知，不能补 0。资源映射读取不发起真实网络采样，额外遥测网络/计算开销没有在此测量。

仅当前及过去 `step` 的事件可见，事件持续直到覆盖。`step=repeat*task_count+index`，重复不会自动重置轨迹；若需要每轮完整故障阶段，必须显式扩展 trace 并冻结。不允许策略读取未来事件。固定正逻辑时钟保留首步的陈旧资源年龄。

输出保留全量失败、原始模型响应、注入值、实际/逻辑调用、完整总时延与所有策略行。P95 包含失败，不做成功筛选；费用没有凭据就保持 unknown。报告不能解释为实体家居、真实 6G 空口、跨主机算力或外部用户泛化实测。
