# AI+6G 任务感知运行时

`ai6g_runtime` 是 SenseHome-6G 的可运行软件原型。它把自然语言任务送入统一的任务分类、健康观测、端边云路由、模型输出校验和软件设备执行闭环。运行时只改变协议允许的模拟设备状态，并将每次请求写入 JSONL，便于复现和审计。

## 快速开始

从仓库根目录执行：

```powershell
$env:PYTHONPATH=(Resolve-Path 'SenseHome-6G').Path
python -m unittest discover -s SenseHome-6G/ai6g_runtime/tests -p 'test_*.py' -v
python SenseHome-6G/ai6g_runtime/benchmark.py `
  --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl `
  --out SenseHome-6G/ai6g_runtime/evidence/sample.json `
  --strategy rules
```

离线命令不需要 GPU、API key 或外网。`rules`运行协议与执行工程链路的确定性基线。

启动 HTTP 服务：

```powershell
python SenseHome-6G/ai6g_runtime/serve.py --host 127.0.0.1 --port 8090
```

服务默认把设备状态保存在内存中，进程退出后状态会丢失。需要跨进程恢复时，指定状态文件；也可以通过 `AI6G_STATE_PATH` 环境变量提供同一路径：

```powershell
python SenseHome-6G/ai6g_runtime/serve.py `
  --host 127.0.0.1 --port 8090 `
  --state SenseHome-6G/ai6g_runtime/evidence/device-state.json
```

配置 `AI6G_LOCAL_URL` 或 `AI6G_CLOUD_URL` 后，服务启动会用 `HealthMonitor` 向每个真实模型端点发送一次专用健康探测。`--probe-interval N` 可按秒数持续探测，`--probe-log path.jsonl` 会追加保存每次探测报告：

```powershell
python SenseHome-6G/ai6g_runtime/serve.py `
  --port 8090 `
  --calibration SenseHome-6G/ai6g_runtime/evidence/calibration.json `
  --probe-interval 30 `
  --probe-log SenseHome-6G/ai6g_runtime/evidence/probes.jsonl
```

校准文件为每个后端提供`quality_score`（0到1）及来源文件的64位`source_sha256`。下例展示字段结构；部署时填入实际校准结果：

```json
{
  "backends": {
    "local-model": {
      "quality_score": 0.91,
      "source_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  }
}
```

没有`--calibration`时，探测仍会调用模型并记录可达性、延迟和错误，`/v1/health`不发布质量分数。健康探测会丢弃模型响应，不进入任务执行器；每次探测会占用调用时延、token和带宽，并可能产生费用。

### 可选资源遥测与路由

`POST /v1/health` 可在原有健康字段外提供可选 `resources` 对象。字段可以逐项缺省；缺失值保持为未知，不会自动补成零。资源时间戳独立于模型健康时间戳，超过资源 TTL 或时间戳在未来的资源值不会参与路由判断。资源观测只用来排除已知拥塞后端；未提供资源遥测时，路由沿用健康质量、可达性、延迟和任务截止期规则。

```json
{
  "backend": "local-model",
  "reachable": true,
  "latency_ms": 120,
  "quality_score": 0.91,
  "resources": {
    "queue_ms": 35,
    "free_mem_mb": 4096,
    "inflight": 2,
    "source": "node-exporter",
    "observed_at": 1794300000.0
  }
}
```

上例的数值仅用于说明字段格式。资源路由目前读取以下可调工程默认值：`AI6G_RESOURCE_TTL_S=5`、`AI6G_MAX_QUEUE_MS=250`、`AI6G_MIN_FREE_MEM_MB=256`、`AI6G_MAX_INFLIGHT=8`、`AI6G_ROUTE_SWITCH_MARGIN_MS=10`。这些值不是 6G 标准或硬件能力结论，应按部署环境校准。运行时还会记录它自身按后端统计的并发请求数；该计数只覆盖任务模型调用，不包括健康探测。任务记录的 `route.resource_observation` 会标明数据来源、年龄、是否新鲜以及是否用于本次路由。仪表盘对缺值显示“未知”，过期值标明“未用于路由”。

然后提交任务并查看状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/v1/tasks -Method Post `
  -ContentType 'application/json' `
  -Body '{"text":"打开客厅的灯","request_id":"demo-1"}'
Invoke-RestMethod http://127.0.0.1:8090/v1/state
Invoke-RestMethod http://127.0.0.1:8090/v1/logs
```

## 运行时边界

- `protocol.py` 负责任务分类、动作白名单、位置和亮度校验，以及澄清/拒绝结果。
- `router.py` 使用带时间戳的健康观测；超过TTL的观测退出性能排序。
- `models.py` 提供确定性的规则后端和 OpenAI 兼容 HTTP 后端。
- `executor.py` 持久化软件设备状态，并按 `request_id` 幂等处理重复请求。
- `runtime.py` 记录从请求入口开始的分类、路由、模型、校验、执行和落盘计时。
- `evaluator.py` 对冻结任务集执行重复实验；gold 只在运行结束后离线评分，不会发送给模型或路由器。

模型输出必须是 JSON 对象，例如：

```json
{"actions":[{"type":"light_on","location":"客厅","brightness":80}],"explanation":"..."}
```

非法 JSON、未知动作、未知位置、越界亮度或模型不可用会以失败结束，设备状态保持不变。截止期若在状态提交前耗尽则返回失败；原子持久化越过截止点时返回`late_commit`并附`commit_succeeded=true`。安全任务采用fail-closed策略，条件不足时澄清或拒绝。每份持久化回执记录审计状态；日志写入失败后重启重放会返回`audit_unknown`。

## 接入真实模型

运行时接受 OpenAI 兼容的 `POST /v1/chat/completions` 端点：

```powershell
$env:AI6G_LOCAL_URL = 'http://127.0.0.1:8001/v1/chat/completions'
$env:AI6G_LOCAL_MODEL = 'your-local-model'
$env:AI6G_CLOUD_URL = 'http://127.0.0.1:8002/v1/chat/completions'
$env:AI6G_CLOUD_MODEL = 'your-cloud-model'
python SenseHome-6G/ai6g_runtime/serve.py --port 8090
```

`model_real=true` 只有在收到并解析真实 HTTP 模型响应时才会出现。正式实验还必须保存模型版本、提示词版本、端点配置摘要、服务日志、原始 JSONL、冻结任务集哈希和失败样例。AutoDL 启动与公平对照命令见 `deploy/README_autodl.md`。

## 复现与证据

```powershell
python SenseHome-6G/ai6g_runtime/evaluator.py `
  --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl `
  --out SenseHome-6G/ai6g_runtime/evidence/repeated.json `
  --repeats 2 `
  --strategies rules
```

每行 JSONL 包含任务、路由理由、观测年龄、`model_real`、模型负载/错误、动作计划、执行结果、状态快照和完整计时。

双模型v3在同一张AutoDL RTX 4080 SUPER上运行。原始报告含36题×3次；剔除与开发集文本重复的3题后，保留33题×3次。去重结果中，rules/local/cloud/adaptive语义正确分别为30/99、51/99、66/99、57/99；工程通过分别为42/99、93/99、90/99、87/99。adaptive比固定local高6.06个百分点，低于固定cloud。24次探针共耗时16214.106ms、7944 tokens。结果描述该任务集上的共卡远程服务原型。

网络算力联合路由材料还包括合成回放和固定HTTP服务桩，用于检查路由入口、策略分支及故障处理。它们不是无线链路或6G网络测量；服务端点时延也不等同于无线RTT。报告该组结果时同时列出固定策略对照。

