# AutoDL 真实模型部署与公平实验说明

本目录只保存部署配置和可审计命令，不保存 API key、不写入模型输出，也不把启动成功当成实验结果。模型响应是否真实由运行时收到 OpenAI 兼容 HTTP 响应后写入 `model_real=true` 决定。

## 1. AutoDL 环境准备

在 AutoDL Linux 实例中确认 CUDA、Python 和显存，再安装与镜像匹配的 vLLM。不要盲目复制一个与 CUDA 不兼容的固定版本：

```bash
nvidia-smi
python --version
python -m pip install -U vllm
python -c 'import vllm; print(vllm.__version__)'
```

如果模型已经下载到本地，将 `--model-path` 指向该目录；否则 vLLM 会按 Hugging Face ID 下载，下载时间和磁盘占用应单独记录。模型候选和端口见 [`model_matrix.json`](model_matrix.json)。

## 2. 启动、健康检查、停止

给脚本执行权限：

```bash
chmod +x SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh
```

启动 1.5B 端侧候选：

```bash
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh start \
  --model qwen2.5-1.5b --port 8001
```

启动 7B 云端候选（建议另一张 GPU 或另一台 AutoDL 实例；同一张卡同时启动两个模型时要先确认显存）：

```bash
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh start \
  --model qwen2.5-7b --port 8002
```

如果模型在本地目录：

```bash
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh start \
  --model qwen2.5-7b --model-path /root/autodl-tmp/models/Qwen2.5-7B-Instruct \
  --port 8002 --gpu-memory-utilization 0.90
```

启动后必须先执行健康检查：

```bash
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh health --port 8001
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh health --port 8002
```

健康检查只确认 `/health` 和 `/v1/models` 可访问，不代表模型语义正确。服务日志在 `deploy/state/vllm-<port>.log`，应作为实验附件保留。停止时只允许脚本停止它自己写入 PID 文件、且命令行确实包含 `vllm.*api_server` 的进程：

```bash
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh stop --port 8001
bash SenseHome-6G/ai6g_runtime/deploy/start_vllm.sh stop --port 8002
```

脚本不会读取或写入密钥；若服务端启用了鉴权，请在当前 shell 中临时设置 `AI6G_LOCAL_API_KEY` / `AI6G_CLOUD_API_KEY`，不要把值写入仓库或命令历史。

## 3. 接入运行时

在运行评测的机器上只设置端点和模型名：

```bash
export AI6G_LOCAL_URL='http://127.0.0.1:8001/v1/chat/completions'
export AI6G_LOCAL_MODEL='qwen2.5-1.5b'
export AI6G_CLOUD_URL='http://127.0.0.1:8002/v1/chat/completions'
export AI6G_CLOUD_MODEL='qwen2.5-7b'
export AI6G_OBSERVATION_TTL_S='5'
```

若 AutoDL 与评测机不在同一台机器，将 `127.0.0.1` 换成已授权的内网地址，并记录端点地址、端口转发和网络限制。评测机不能把未来故障、任务 gold 或模型答案传给路由器。

固定超时统一为 **5000 ms**；任务文件内的安全任务有更短截止期，运行时仍以任务截止期为最终约束。温度固定为 `0`，模型输出必须是 JSON 对象，且通过 `protocol.validate_plan()` 后才可执行。

## 4. 公平实验命令（36 条任务 × 3 次）

先确认 `tasks.jsonl` 的 SHA-256 和行数（必须是 36），并保存命令输出：

```bash
sha256sum SenseHome-6G/ai6g_runtime/tasks.jsonl
wc -l SenseHome-6G/ai6g_runtime/tasks.jsonl
```

当前 `evaluator.py` 使用策略名 `rules`、`local`、`cloud`、`adaptive`；报告中将它们分别标注为 `rules`、`fixed-local`、`fixed-cloud`、`adaptive`。四种策略共享同一任务顺序、同一执行器、同一请求超时和同一端点配置：

```bash
python SenseHome-6G/ai6g_runtime/evaluator.py \
  --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl \
  --out SenseHome-6G/ai6g_runtime/evidence/autodl-qwen25.json \
  --repeats 3 \
  --strategies adaptive,rules,local,cloud
```

等价的单策略复核命令：

```bash
python SenseHome-6G/ai6g_runtime/benchmark.py --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl --out /tmp/rules.json --strategy rules
python SenseHome-6G/ai6g_runtime/benchmark.py --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl --out /tmp/fixed-local.json --strategy local
python SenseHome-6G/ai6g_runtime/benchmark.py --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl --out /tmp/fixed-cloud.json --strategy cloud
python SenseHome-6G/ai6g_runtime/benchmark.py --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl --out /tmp/adaptive.json --strategy adaptive
```

### Adaptive 评测前的健康观测

`adaptive` 只能使用带时间戳的健康观测；空观测缓存会按设计返回 `route_unavailable`，不能被解释为模型失败或语义结果。当前 `evaluator.py` 的直接 `build_runtime()` 路径不会自动探测端点，因此正式跑 adaptive 前必须在评测包装层完成一次真实健康探测并把结果写入该 run 的 `ObservationCache`，或在运行时服务上用 `POST /v1/health` 注入观测。探测只允许读取 `/health`、`/v1/models`、耗时和错误，不得读取任务 gold。

最小服务式健康注入示例（另开终端启动 `serve.py` 后执行）：

```bash
python SenseHome-6G/ai6g_runtime/serve.py --host 127.0.0.1 --port 8090 \
  --log SenseHome-6G/ai6g_runtime/evidence/autodl-service.jsonl
curl -fsS -X POST http://127.0.0.1:8090/v1/health \
  -H 'Content-Type: application/json' \
  -d '{"backend":"local-model","reachable":true,"latency_ms":120,"quality_score":0.82}'
curl -fsS -X POST http://127.0.0.1:8090/v1/health \
  -H 'Content-Type: application/json' \
  -d '{"backend":"cloud-model","reachable":true,"latency_ms":250,"quality_score":0.96}'
```

上面示例中的延迟和质量值只是**观测输入示例**，不能直接当作结果；正式实验应由健康探测脚本实测并固定记录。若只跑 `evaluator.py` 而没有注入观测，请把 adaptive 结果标为配置不完整，不能与固定策略作公平比较。

## 5. 必须保存的证据

每次正式运行至少保存：

- `model_matrix.json` 的副本、任务集 SHA-256、Python/vLLM/CUDA/PyTorch 版本；
- vLLM 启动命令、模型下载路径、启动日志和 `/v1/models` 响应；
- 端点配置摘要（URL 可脱敏，模型名和提示词版本不能省略）；
- 每个策略每次重复的原始 JSONL，以及 evaluator JSON/Markdown；
- 失败、超时、非法 JSON、模型不可达和状态未改变的记录。

没有 `model_real=true` 的记录时，只能报告规则或工程基线；不能把成功执行率写成模型语义准确率。真实模型结果也要用冻结任务的离线 gold 评测，并报告成功率、完整 P50/P95、错误构成和重复波动。
