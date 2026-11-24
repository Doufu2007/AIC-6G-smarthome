# v6 本机生产入口

复用 v5 的 HTTP API、持久化状态和原始采集绑定校验；新入口使用 `BudgetedHealthMonitor`，不启动旧 `HealthMonitor`。

从项目根目录运行，`--state-dir` 必须尚不存在：

```powershell
python -X utf8 SenseHome-6G/ai6g_runtime/deploy/local_profile_v6.py --endpoints-file _work/local_llm_v6/deployment_identity.json --prompt-file SenseHome-6G/ai6g_runtime/deploy/household_protocol_prompt_v5.txt --calibration SenseHome-6G/ai6g_runtime/evidence/local_v6_calibration/calibration_runtime_device_guard.json --state-dir _work/gap_closure_v6/runtime_live_01 --port 8092 --probe-interval 2 --canary-ttl 60 --min-canary-interval 30
```

端点保持 `GET /v1/health`、`GET /v1/state`、`POST /v1/tasks` 等现有 Runtime API。在本次独占状态目录创建空文件 `STOP`，或发送 Ctrl+C，可关闭 HTTP 和本入口拥有的监测线程；不会停止独立 llama.cpp 模型服务。已有状态目录拒绝复用。

v6 的 `POST /v1/health` 返回 `403 / health_read_only`；健康观测只由内部 monitor 直接发布，外部 HTTP 调用不能覆盖绑定校准。此限制不修改 v5 或通用 Handler。

`load_profile(endpoints_file, prompt_file, calibration_file)` 返回原有 `services/prompt/calibration/metadata` 及新增 `latencies`。它校验原始 capture、manifest 指纹、prompt、model、解码参数和完整部署身份；派生 `calibration_runtime.json` 的 `scoring_pipeline.guard_sha256` 必须匹配当前 `grounding_v6.py`，并绑定原始旁车 manifest、离线投影标记、质量样本总体及正数 p95 推理墙钟时延。guard 改变后必须重新生成派生校准，不能继续使用旧文件。

`LocalProfileServer` 保留 v5 构造和生命周期接口，新增 `canary_ttl_s`、`min_canary_interval_s`；构造完成即暴露 `app.monitor`。实验可不启动 HTTP，直接调用 `app.monitor.run_once()`、`app.runtime.handle(...)`，最后 `app.close()`。调用者自行创建的监测线程须由调用者先停止；入口只负责自己在 `start()` 中创建的线程。

模型原始返回先写入 `model_calls.jsonl`，随后业务经过 `GroundedBackend` 和被动计时包装。canary 直接检查原始返回，审计标为 `health_probe`，不经过 guard 修正或设备执行器。HTTP `/health` RTT 只表示可达性；推理时延来自绑定校准 p95 与近期业务墙钟观测。单次采样共享 GPU 读取，未知资源保持未知。

近期业务延迟保留窗口固定为60秒，与canary有效期分开；比较监测策略时必须保持该窗口、模型、prompt、guard、校准与容量一致。当前正式新校准文件为 `calibration_runtime_device_guard.json`；原 `calibration_runtime.json` 保留了早期guard身份，不能用删校验的方式继续启动。

路由容量固定为 1，与记录的模型 `parallel=1` 一致。`QueueBoundRouter` 仅在选路线程的读取视图中，对新鲜且已知的队列采用 `max(推理墙钟时延, queue_ms)` 下界；不直接相加，不改写原始缓存，也不使用过期或未来资源读数。

`--probe-interval 0` 只运行一次观测；持续服务通常保留周期 HTTP/资源采样。若实验要求正常期间只在启动生成 canary，可将 `--canary-ttl` 设为足够大的明确值；故障恢复仍遵循最低间隔与退避预算。

软件测试使用本机 HTTP fixture，不调用 GPU：

```powershell
python -m unittest discover -s SenseHome-6G/ai6g_runtime/tests -p test_local_profile_v6.py -v
```

红绿日志和完整 runtime 回归日志保存在 `_work/gap_closure_v6/`。
