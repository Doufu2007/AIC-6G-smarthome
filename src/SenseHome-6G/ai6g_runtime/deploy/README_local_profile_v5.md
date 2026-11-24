# 本机真实双模型运行入口（V5）

此入口复用已启动的两个 llama.cpp HTTP 服务，使用最终家庭动作协议提示词、真实校准、真实健康探针和只读本机资源采集器。它不会启动、重启或停止模型服务。两个逻辑后端均属于本机信任域；`cloud-model` 是历史接口名称，两服务共用本机 GPU 和 RAM，不能表述为真实端云或 6G 实网实验。

## 启动

在项目根目录运行（`python` 需为已装项目依赖的解释器）：

```powershell
python SenseHome-6G/ai6g_runtime/deploy/local_profile_v5.py `
  --endpoints-file _work/local_llm_v5/endpoints_selected_v5b.json `
  --prompt-file SenseHome-6G/ai6g_runtime/deploy/household_protocol_prompt_v5.txt `
  --calibration SenseHome-6G/ai6g_runtime/evidence/local_v5c/calibration.json `
  --state-dir _work/local_runtime_v5_01 `
  --host 127.0.0.1 --port 0 --probe-interval 2 --probe-timeout 5
```

`--port 0` 由系统选空闲端口，便于避开已有演示实例和 Windows 保留端口。也可以显式使用 `--port 8091`，前提是该端口可绑定。启动输出及 `state-dir/profile.json` 给出实际 `url`。当前两个模型服务端口为 62342 和 62343，分别对应 1.5B 和 3B；它们必须已启动。端点 JSON 仅作显式配置，旧 `system_prompt` 字段不会生效。

`--state-dir` 必须是**尚不存在的新目录**，每次运行换一个名称。入口以原子创建目录保证单进程独占该状态与证据；不会覆盖或重用旧试验。只允许 loopback 监听，拒绝外部模型/slots URL、URL 账号密码、query/fragment、重复名称与未知 profile schema。本机实例不使用 API Key，命令不传密码。

支持以下配置 schema：`sensehome.local_endpoints.v5`、`sensehome.local_endpoints.v5b`、`sensehome.deployment_identity.v5`。两条 service 名称必须严格为 `local-small`、`local-large`，依次映射到 `local-model`、`cloud-model`，每条提供 `model`、`endpoint`、`slots_url`。模型别名不代表性能；换模型、换有效提示词、换解码参数或换端点后，需匹配相应的重新校准证据。

校准目录必须同时保留 `calibration.json`、`calibration_capture.jsonl` 和 `calibration_capture.manifest.json`。入口核对捕获文件 SHA、manifest 指纹、每行 capture 指纹，以及模型名、端点、有效提示词 SHA 与解码配置。最终配置是 `temperature=0`、`seed=20261104`、`max_tokens=384`、`response_format=json_object`。提示词沿用冻结采集的 Python 换行规范化与 `strip()`；分别记录原文件字节 SHA、有效文本 SHA 和 manifest 所用 JSON 标量 canonical SHA，避免 Windows CRLF/LF 与字符串序列化混淆。

## 请求与停止

在另一个 PowerShell 窗口读取本次实例 URL，然后调用：

```powershell
$runDir = '_work/local_runtime_v5_01'
$runtimeUrl = (Get-Content -LiteralPath "$runDir/profile.json" -Raw -Encoding UTF8 | ConvertFrom-Json).url
$body = @{text='打开客厅的灯'; request_id='manual-001'} | ConvertTo-Json
Invoke-RestMethod -Uri "$runtimeUrl/v1/tasks" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod -Uri "$runtimeUrl/v1/state"
```

显式单动作会走本地规则。验证真实模型路径可用已有的非规则窗帘指令，例如“请帮我打开客厅窗帘”，以返回的路由、`model_real` 和模型原始响应为准。校准不合格、健康过期或资源不足时会如实拒绝模型派发；不得为了展示成功放宽质量阈值。启动探针是实际模型请求，会占用 GPU、延迟和 token，每轮对两个模型各请求一次；探针直接调用后端，结果不进入设备执行器。

按 `Ctrl+C`，或在另一窗口写入本次目录的 `STOP` 文件：

```powershell
New-Item -ItemType File -Path '_work/local_runtime_v5_01/STOP'
```

入口先等待本次在途探针结束，再关闭自身 HTTP 服务并将 `profile.json` 的状态写为 `stopped`。只关闭本次入口拥有的线程与端口，模型服务继续运行。需要暂停模型计费或释放模型显存，应另行管理模型服务。

Windows 上，其他进程短暂读取 `profile.json` 可能阻止原子替换。入口每隔 50 ms 重试，最多 20 次（总等待不足 1 秒）；旧 JSON 在此期间保持完整。持续权限拒绝会明确报错，保留临时文件，不把状态写入失败伪装为成功停止。

## 审计文件

| 文件 | 内容 |
|---|---|
| `profile.json` | 本次 PID、URL、配置/校准/有效提示词 SHA、服务映射和生命周期 |
| `state.json` | 设备状态及幂等账本（首次发生状态写入时生成） |
| `requests.jsonl` | 业务请求、路由、协议校验、执行与模型原始响应 |
| `model_calls.jsonl` | 每次真实后端调用的完整 API JSON，含 token usage；`scope` 区分业务与健康探针 |
| `probes.jsonl` | 每个探针的结果、耗时、校准来源 SHA 与健康发布结果 |
| `resources.jsonl` | nvidia-smi 和 llama slots 只读快照、原始采集响应、来源与采集开销 |

显存读数属于共享的整张物理 GPU；slots 只表示 `is_processing=true` 的数量。`queue_ms` 始终为未知，不从 busy slots 推测排队时间。资源采集失败保持未知并记录错误，不伪造可用显存或排队数据。以上是本机演示审计，不能与受控九策略回放日志混用。日志可能包含用户指令和模型输出，对外发布前应按实际内容审查。

3B 使用 Qwen Research License，只用于非商业研究/评估。商用演示或交付需要另获许可或替换为许可允许的模型并重新校准。代码包不分发 GGUF 权重和 llama.cpp 可执行文件。

## 无 GPU 的回归验证

```powershell
python -m unittest discover -s SenseHome-6G/ai6g_runtime/tests -p test_local_profile_v5.py -v
python -m unittest discover -s SenseHome-6G/ai6g_runtime/tests -v
```

新测试只访问两个临时 loopback HTTP fixture，并在采集边界提供 nvidia-smi fixture；不会调用真实模型服务或占用 GPU。真实 Smoke 应在新端口、新状态目录中独立执行并正常停止。
