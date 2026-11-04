# AutoDL 真实模型接入与验收

截至 2026-11-03，AutoDL 新入口已恢复，Qwen2.5-1.5B-Instruct 完整权重及固定版本通过校验，已完成三轮真实模型预检。最新提示词与透明归一化版本 HTTP 成功 17/20、动作一致 17/20，回环请求中位数 290.679 ms、p95 522.643 ms；A01/A02/A04 的 HTTP 422 是房间缺失或白名单拒绝。20 条标签已由参赛者确认并冻结，正式报告见 `../../docs/experiments/autodl_20261103_normalized_v2/eval.formal.json`；该报告是冻结后对已捕获真实模型响应的重新标注，没有重新推理。原始结果、失败日志及最新进展见 `../../docs/real_model_attempt_20261103.md`、`../../docs/experiments/autodl_20261103_normalized_v2/`。

本次实际运行环境为 RTX 4080 SUPER（32760 MiB）、驱动 595.71.05、Python 3.12.3、Torch 2.14.0+cu130、vLLM 0.33.0，解释器是 `/root/miniconda3/bin/python`，未创建独立 venv。使用非登录 SSH 命令时须把 `/root/miniconda3/bin` 加入 PATH，以便找到已有 `ninja`。现有环境恢复与草案预检优先使用 `../../docs/deployment/AutoDL_小模型恢复与证据回收.md`，下文 venv 命令保留为新环境部署示例。

目标是把两个逻辑模型档位接入已有网关：小模型规划家庭动作，大模型处理同一份命令集，设备动作最终由 Dashboard 白名单执行。小模型部署在 AutoDL 时只是 `local-model` 的逻辑角色，不能据此声称已实现真实家庭网关部署、真实 6G 空口或物理边云协同。

## 文件与接口

| 文件 | 用途 |
|---|---|
| `backends/openai_backend.py` | 标准库 HTTP 适配器，对接 `/v1/chat/completions` |
| `openvela_smart_home/dashboard/device_actions.py` | 唯一设备动作白名单；适配器用隔离状态预校验，不修改真实设备 |
| `backends/model_backend.env.example` | 不含密钥的配置示例 |
| `backends/eval_commands.reviewed.jsonl` | 20 条已人工核对并冻结的正式验收标签；审核人、日期和 SHA256 见冻结记录 |
| `backends/eval_commands.jsonl` | 20 条原始草案历史，仅用于回溯和预检，不作为正式准确率标签 |
| `eval_model.py` | 按参考动作判分，忽略模型自报的 `semantic_complete` |
| `tests/test_openai_backend.py` | 本地假上游的真实 HTTP 测试 |

适配器接口：

- `GET /probe`：只测到适配器的网络往返，不调用模型。
- `GET /health`：查询上游 `/v1/models`，检查配置模型是否存在，返回 `ready`、适配器当前 `inflight` 等观测。
- `POST /infer`：输入 `{"command":"客厅有点暗","context":{}}`；调用上游真实推理，返回 `answer`、经过校验的 `actions`、模型来源和耗时。

`model_ms` 是适配器到上游模型服务的一次 HTTP 墙钟实测，包含上游排队、token 生成和 HTTP 开销；它不是纯 GPU 内核时间。`gpu_info`、`model_revision` 来自部署环境声明，字段带有 `environment_declared` 来源；提交时另保存实际 `nvidia-smi`、模型下载 commit 和软件版本。适配器不提供伪造的空闲显存数值，`upstream_queue_ms=null` 表示上游队列没有遥测。

Dashboard 可直接消费这种部分健康观测：`ready` 映射到 `model_ready`，已知 `inflight` 独立送给 C 策略；`ready=false` 触发模型不可用降级，已知并发超预算触发过载降级。未知显存和上游队列在事件中保持 `null`，通过 `compute_partial` 协议以 `-1` 送入 C，不参与对应阈值判定。适配器的 `queue_ms=0` 仅表示其自身无等待队列，Dashboard 不把它当作上游模型排队时间。事件包含 `known_fields`、`unknown_fields`、`field_sources`；`inflight` 标为 `wrapper_inflight`，未知字段来源为 `unknown`。完整四字段旧接口继续可用。

模型规划成功不等于设备执行成功：适配器返回 `semantic_complete=false`，本地白名单执行后才可由网关更新完成状态。

适配器对明确单房间的“有点亮/有点暗”和温湿度只读请求保留模型原始动作，并在必要时写入 `action_normalization` 后补齐动作；模糊窗帘“调整一下”会移除模型猜测并要求澄清。任何归一化都先经过原始动作白名单校验，再经过一次相同白名单校验，响应会保留 `raw_model_actions` 以便审计。

## 1. 先在普通电脑验证接入包

在 `SenseHome-6G/net_offload` 运行：

```powershell
python -m unittest discover -s tests -p 'test_openai_backend.py' -v
python -m unittest discover -s tests -p 'test_eval_model.py' -v
```

这些测试只访问回环地址的假上游，不消耗外部 API。测试覆盖正常 JSON、动作白名单、参数边界、畸形输出、上游 HTTP 异常和超时。不能把测试输出写成真实模型成绩。

## 2. 准备服务器与权重

推荐先在同一块 GPU 上依次验收小、大模型，以便控制租用时长。可选起点是 `Qwen/Qwen2.5-1.5B-Instruct` 和 `Qwen/Qwen2.5-7B-Instruct`；它们是模型选择建议，不是已验证结果。先查看所选权重的许可、实际显存需求和当前 vLLM 支持情况，再下载并冻结实际 revision。记录具体 GPU、显存、驱动、CUDA、Python、vLLM 和权重 commit。

服务器侧可准备一个独立虚拟环境：

```bash
python -m venv /root/autodl-tmp/sensehome-venv
source /root/autodl-tmp/sensehome-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install vllm
python -m pip freeze > /root/autodl-tmp/sensehome-runtime.lock.txt
nvidia-smi > /root/autodl-tmp/sensehome-nvidia-smi.txt
```

把适配器和**当前版本**的 `device_actions.py` 复制到服务器的同一目录，例如 `/root/autodl-tmp/sensehome-adapter/`。保留两文件 SHA256，避免服务器的动作协议落后于 Dashboard。保留整个仓库目录也可，适配器会自动找到仓库中的权威文件。

模型下载后分别放到 `/root/autodl-tmp/models/small` 与 `/root/autodl-tmp/models/large`。以下命令假设这两个目录已经是完整且验证过的模型目录；下载过程中的真实 commit 应记录到各自证据目录。

## 3. 运行小模型及适配器

第一个服务器终端运行模型服务：

```bash
source /root/autodl-tmp/sensehome-venv/bin/activate
vllm serve /root/autodl-tmp/models/small --served-model-name sensehome-small --host 127.0.0.1 --port 8001 --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.9 --seed 20261103
```

第二个服务器终端运行适配器：

```bash
source /root/autodl-tmp/sensehome-venv/bin/activate
export SENSEHOME_UPSTREAM_URL=http://127.0.0.1:8001/v1
export SENSEHOME_MODEL=sensehome-small
export SENSEHOME_TIER=gateway
export SENSEHOME_WRAPPER_PORT=18092
export SENSEHOME_UPSTREAM_TIMEOUT_S=60
export SENSEHOME_GPU_INFO="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader)"
export SENSEHOME_MODEL_REVISION=unknown
python /root/autodl-tmp/sensehome-adapter/openai_backend.py
```

将 `SENSEHOME_MODEL_REVISION` 的 `unknown` 改为下载时记录的实际 commit。`GPU_INFO` 是启动时观察值，不会被冒充为持续显存遥测。

适配器默认只绑定 `127.0.0.1`。在本机终端建立 SSH 隧道；主机和端口使用 AutoDL 控制台实际提供的值：

```powershell
ssh -N -p <SSH端口> -L 18092:127.0.0.1:18092 root@<AutoDL主机>
```

本机新终端验证：

```powershell
Invoke-RestMethod http://127.0.0.1:18092/probe
Invoke-RestMethod http://127.0.0.1:18092/health
$body = @{command='客厅有点暗'; context=@{}} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18092/infer -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

确认 `/health.ready=true`、`configured_model=sensehome-small`，以及 `/infer` 有合法动作和明确来源。适配器不会执行设备动作。

## 4. 大模型同样验收

单 GPU 的默认流程是停止小模型 vLLM 后再运行大模型，避免两套默认 90% 显存预算同时竞争。同一验收集、同一生成参数、相同重复次数用于两模型评测。

```bash
vllm serve /root/autodl-tmp/models/large --served-model-name sensehome-large --host 127.0.0.1 --port 8000 --dtype auto --max-model-len 4096 --gpu-memory-utilization 0.9 --seed 20261103
```

适配器环境改为上游 `http://127.0.0.1:8000/v1`、模型 `sensehome-large`、`SENSEHOME_TIER=cloud`、端口 `18091` 和大模型的真实 revision，再启动适配器。隧道使用 `-L 18091:127.0.0.1:18091`。

如果最后需要两端同时在线演示，必须先验证显存足够并分别设置总和可承受的显存预算，或使用两台实例。双进程在同一 GPU 上共享资源会互相影响时延，应如实记录；只做顺序评测时不能声称双模型实时切换已经通过。

## 5. 密钥处理

回环地址的 vLLM 可通过 SSH 隧道使用，无需外部付费 API 密钥。接入已有需要鉴权的兼容服务时，适配器只从 `SENSEHOME_API_KEY` 环境变量读取密钥并放入上游 Authorization 头。

服务器终端可交互读取，避免把密钥写入脚本或 shell 历史：

```bash
read -rsp 'Upstream API key: ' SENSEHOME_API_KEY
export SENSEHOME_API_KEY
echo
```

适配器不记录请求内容、Authorization 头或上游错误正文，配置对象的密钥字段不会出现在 `repr`。不要把完整进程环境、真实 `.env`、密钥或带凭证 URL 放进截图、录屏和提交包。若启用可选 `SENSEHOME_WRAPPER_TOKEN`，本包 `eval_model.py` 支持该令牌；当前 Dashboard 的默认接法是回环 SSH 隧道，因此保持 wrapper token 未设置。

## 6. 人工标注验收与评测

`eval_commands.reviewed.jsonl` 是 20 条**已由参赛者逐条人工核对并冻结的正式参考标签**，包含明确控制、查询、模糊意图、多意图、澄清、越权和注入；原始 `eval_commands.jsonl` 仅作为草案历史保留。评测脚本默认使用 reviewed 副本并拒绝把草案当作正式标签。

模糊语义中的 60%/30% 是本应用预先声明的交互约定，不能按模型输出改答案。当前已冻结 `backends/eval_commands.reviewed.jsonl`，全部 20 行标记 `human_reviewed=true`，审核人、日期和 SHA256 见 `docs/deployment/20条动作标签冻结记录_20261103.json`。正式报告使用冻结副本；后续仍应增加不同表达的隐藏测试集，避免只记住这 20 句。

```powershell
python eval_model.py --url http://127.0.0.1:18092 --cases backends/eval_commands.reviewed.jsonl --out _work/real_small/eval.json --repeat 3
python eval_model.py --url http://127.0.0.1:18091 --cases backends/eval_commands.reviewed.jsonl --out _work/real_large/eval.json --repeat 3
```

仅做管线预检时可显式添加 `--allow-draft-labels`；输出会保留 `draft_labels_not_human_verified`，这类结果不能用作正式准确率。

评测核对的是实际动作效果：例如开灯和亮度设为 100% 可视为等效，额外动作、错误房间、错误数值和非白名单动作均不能得分。模型自报 `semantic_complete=true` 不参与判分。输出保存每条期望动作、实际动作、HTTP 成功、动作正确性、墙钟耗时、模型信息及标签文件 SHA256。

拒绝类 `expected_actions=[]` 只验证不执行越权动作；当前评测不检查回答文本是否足够清楚，需人工复核澄清和拒绝措辞。

## 7. 接入 Dashboard 并验收最终闭环

两模型都在线且隧道保持时，在本机启动 Dashboard 前设置：

```powershell
$env:SENSEHOME_CLOUD_URL='http://127.0.0.1:18091'
$env:SENSEHOME_LOCAL_URL='http://127.0.0.1:18092'
python openvela_smart_home/dashboard/bridge_server.py
```

这条命令从项目根目录运行。Dashboard 的后端适配器将健康探测和推理分开计时：默认 `SENSEHOME_HEALTH_TIMEOUT_S=0.5` 秒、`SENSEHOME_INFER_TIMEOUT_S=2` 秒；两者都可按实测 GPU 生成时间配置。适配器自身的 `SENSEHOME_UPSTREAM_TIMEOUT_S` 必须小于 Dashboard 的推理预算，否则 Dashboard 会先回退。首次加载、长输出或慢卡超过推理预算会触发规则回退，应先预热并测量，再确定预算，不能用桩耗时替代。

例如允许适配器最多 60 秒时，启动 Dashboard 前显式设置 `$env:SENSEHOME_INFER_TIMEOUT_S='65'`，并保留 `$env:SENSEHOME_HEALTH_TIMEOUT_S='0.5'`；如果现场要求 2 秒交互，则应把适配器上游超时和模型生成长度一起降到该预算以内。

逐项保存证据：

1. 模型身份：两个模型的实际 revision、GPU/驱动、vLLM/Python 版本、启动参数与源码 SHA256。
2. 动作正确性：冻结标签后的逐条结果；至少分别报告模糊、多意图、越权类，避免混合平均掩盖问题。
3. 真实执行：在 Dashboard 输入明确房间的复杂指令，确认返回动作确实改变设备模拟状态，事件 `source=real-model` 且保留模型 ID；此项仍是模拟设备执行。
4. 故障：停止大模型、制造适配器超时、返回坏 JSON 和越权动作，核对日志记录与本地降级；恢复后核对重新可选。
5. 口径：同时保存用户总时延、探测/策略/后端分量和模型适配器墙钟；注明网络是否经隧道、两模型是否共 GPU。

建议验收目标是所有越权动作零执行、明确结构化控制无错误动作、普通指令不因模型故障失去本地规则响应。小、大模型的复杂意图准确率与 p95 先实测再定量，本文不预填分数，也不以此保证获奖。

本包交付状态：代码、本地假上游测试和 AutoDL 小模型草案预检已完成；人工标签复核、正式真实模型评测和现场录屏仍需在提交前完成。
