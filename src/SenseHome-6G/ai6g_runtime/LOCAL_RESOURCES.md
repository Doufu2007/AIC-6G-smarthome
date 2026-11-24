# 本机真实只读资源观测

`local_resources.py` 为现有 `HealthMonitor(resource_provider=...)` 提供不依赖第三方包的真实资源输入，不发送模型推理，不执行设备操作，也不制造负载。它与冻结的 v5 回放实验互相独立；真实观测不能倒填或替换既有注入轨迹。

## 指标含义

- `free_mem_mb` 来自 `nvidia-smi memory.free`，单位 MiB，是选定物理 GPU 的**全局共享余量**。同卡两个模型会看见同一数值；它不是各自独占容量，也不证明 Vulkan 分配行为、模型可容纳 batch 或并发上限。不得把两后端的 free 相加。
- `inflight` 是对应 llama.cpp `/slots` 中 `is_processing=true` 的槽位数，只代表该服务正在处理的 slots，不是候排 HTTP 请求数。
- `queue_ms` 未测量，始终为 `null`。不能根据 busy slot、GPU 利用率或推理时延反推排队时延。
- slots 缺失、禁用、空列表、字段缺失或字段非布尔，`inflight=null`；nvidia-smi 超时、非零退出、GPU 不匹配或无合法显存读数时，`free_mem_mb=null`。不得补成 0。
- `source` 标识只读 nvidia-smi 和 llama slots 路线；`errors` 明确本轮哪些来源失败。`observed_at` 使用真实 `time.time()`，综合元组保守取较早的 GPU 采样完成时间，避免慢 slots 请求把旧显存读数刷新为新鲜。

## 只读运行

```powershell
python SenseHome-6G/ai6g_runtime/local_resources.py --slots local-model=http://127.0.0.1:62341/slots --slots cloud-model=http://127.0.0.1:62342/slots --out _work/gap_closure_v5/telemetry_live_snapshots.jsonl --samples 3 --interval-s 0.2
```

默认选 GPU `0`，可用 `--gpu GPU-UUID` 指定单卡。仅接受不带凭据、query 的 loopback URL；所有 HTTP 显式禁代理且禁止重定向，不能通过302跳到其他服务。默认单次请求/进程超时 2 秒。采样记录为 append-only JSONL，包含原始 nvidia-smi stdout/stderr、返回码、slots JSON、分源与总体采集耗时、观测时间、错误及 `model_inference_requests=0`。slots 中 prompt/content/messages/text 和凭据字段会递归脱敏；不保存用户指令或模型文本。

`collection_duration_ms` 包含本轮 nvidia-smi 子进程和 slots GET 的等待，不包含后续证据文件写入和健康 API 发布。独立发布另报 `publication_duration_ms`。这是实际观测开销，不是 GPU 计算时间；短暂空闲样本不能证明高负载时的分布。

## 接到已有健康监控

```python
from ai6g_runtime.local_resources import LocalResourceProvider
from ai6g_runtime.health_monitor import HealthMonitor

resources = LocalResourceProvider(
    {"local-model": "http://127.0.0.1:62341/slots",
     "cloud-model": "http://127.0.0.1:62342/slots"},
    evidence_path="telemetry_monitor.jsonl")
monitor = HealthMonitor(health_url=runtime_health_url, backends=actual_backends,
                        calibration=frozen_measured_calibration,
                        resource_provider=resources)
```

`HealthMonitor.run_once()` 会另外调用真实模型健康探针，产生推理负载；资源 provider 自身不调用模型。provider 返回的是当前采样、没有缓存占位。已存在的 HealthMonitor 会捕获资源异常并保留模型健康探测结果，缺指标不会让健康模型变成不健康。

## 资源单独发布

`--health-url .../v1/health --health-input measured_health.json` 可把本轮真实资源快照附加到先前真实测得的完整健康 payload 后发布。输入必须是 `alias -> payload`，包含 `backend/reachable/latency_ms/quality_score/checked_at`。发布成功要求HTTP 2xx且JSON响应的 `ok` 严格为 `true`；空响应、无确认或 `ok=false` 均记录失败。工具不会自动补质量、可达性，不会刷新健康 `checked_at`，不会把遥测读取成功当作模型健康。过期的模型健康仍保持原本过期状态。只想采资源时不要传这两参数。

真实闭环验收需要保存：真实探针响应与用量、当时资源原始记录、校准来源、健康 API 发布响应、生产路由结果。软件fixture测试只能证明接线行为；本模块不构成真实家庭设备、6G空口或跨主机端云验证。
