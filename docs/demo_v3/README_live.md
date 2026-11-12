# 本机隔离演示入口

本入口服务修复后的 `openvela_smart_home/dashboard/web_panel.html`，复用原生产C策略、网关与动作执行器。所有设备状态和事件写入 `_work/demo_v3/` 下每次新建的唯一目录，原项目的 `device_state.json` 和 `offload_events.jsonl` 不会被用作运行存储。

## 一条命令启动

在项目根目录执行：

```powershell
python -X utf8 docs/demo_v3/run_live.py
```

打开 `http://127.0.0.1:8879/`。默认 `rules-only` 不加载任何模型，不占用模型端口，不需要外部接口。启动首行JSON包含实际端口、目录、模式、来源与仅用于此次停止的 `shutdown_token`。

可直接验证：

- “打开客厅的灯”：原规则与执行器真实更新私有JSON状态；页面显示网关报告完成。
- “把客厅灯光设为60%，再把卧室灯光设为30%”：默认模式没有模型规划能力，返回未完成且无动作。
- “有点暗”：必要目标不完整，保留澄清或未完成，不自行补出房间、传感器和成功。
- 关闭服务后在保留的页面发送“太暗了”：页面只显示执行未确认，当前状态恢复未知。

也可显式指定端口或新目录：

```powershell
python -X utf8 docs/demo_v3/run_live.py --port 8879 --run-dir _work/demo_v3/my_new_demo
```

指定目录必须尚不存在，且位于 `_work/demo_v3/` 内。已存在目录、工作根本身和根外路径会被拒绝，避免覆盖旧证据。

## 可选的真实本地模型

```powershell
python -X utf8 docs/demo_v3/run_live.py --models
```

它调用 `runtime.LocalRuntime` 校验既有运行时、依赖与模型文件的固定哈希，再启动并只拥有本次的两个模型子进程。端口、模型ID和路径动态读取 `docs/demo_v3/local_models.json`；不会连接或终止他人已运行的模型。如果配置端口被占用，启动失败并清理已经创建的本次资源。

此模式复用 `fused_eval.DirectModelBackend` 的原提示词与输出校验。原C策略以 `joint` 模式初选候选；配置读取既有冻结校准文件的 `local_infer_ms=1116`、`cloud_infer_ms=1361`，不根据此次演示结果重新校准。只有符合条件的复杂任务发生暂时传输失败时，独立 `PlanningRecoveryBackend` 才允许一次替代规划，再由原网关执行器确认并执行动作。替代规划不冒称由旧C策略作出。

恢复启动预算为10000ms，只控制是否再发起一次规划；每个现有HTTP调用仍有独立15s超时，因此它不是整个请求的10s截止保证。

两个模型候选运行在同一台电脑、同一GPU上，是逻辑槽位，不是独立云边部署。页面设备和传感器均为PC仿真，不是实物或真实6G链路测量。模型模式启动期间请勿与正式评测争用同一端口或GPU。

## 健康、隔离与停止

原 `BackendMonitor` 启动时立即实际采样，之后约每2秒采样一次。`bridge.gateway_backends`、`gateway.backends` 和monitor使用相同候选实例。未测量的上游队列、可用显存、带宽、抖动及丢包保持未知；HTTP端点RTT不改称真实WAN或6G测量。

既有Direct适配器的原始交换列表按顺序切片记录。为防止后台健康GET混入推理POST记录，本入口为每个候选单独串行化health与infer；候选之间独立。健康采样可能等待该候选当次规划结束，缓存年龄会真实递增，原C陈旧缓存规则仍生效。

服务仅绑定 `127.0.0.1`，保留原隔离入口的Host与Origin检查。新增GET `/`、`/web_panel.html`；原允许路由为GET `/api/state`、`/api/health`、POST `/api/command` 和带令牌POST `/__shutdown`。巡检、直接灯控与其余路由均禁用，页面巡检状态因此为未确认，不生成模拟巡检记录。

前台运行时按Ctrl+C停止。程序导入场景调用 `session.close()`。如需由自动化脚本停止CLI，使用启动首行给出的令牌调用POST `/__shutdown`，请求头 `X-Isolated-Shutdown` 设置为该值。停止会关闭本次HTTP服务、monitor、C策略进程及自己启动的模型，保存最终清单。不会按端口批量终止其他进程。直接强杀操作系统进程可能跳过清理，应优先使用这些正常停止方式。

## Python导入接口

```python
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path('docs/demo_v3').resolve()))
from run_live import make_live_server

session = make_live_server(port=8879)  # 默认不启动模型；models=True显式启用
thread = threading.Thread(target=session.server.serve_forever, daemon=True)
thread.start()
try:
    print(session.manifest['gateway_url'])
    # 在这里完成浏览器验收或HTTP交互。
finally:
    session.close()
    thread.join(timeout=3)
```

`make_live_server` 返回已绑定但尚未serve的 `LiveSession`，提供 `.server`、`.bridge`、`.run_dir`、`.manifest`、`.shutdown_token` 和幂等 `.close()`；也支持上下文管理。

## 本次保存的可追溯文件

- `run_manifest.json`：模式、范围、配置、校准来源、源码哈希、原状态及事件前后哈希、正常停止或失败原因、C退出码和monitor停止状态。
- `device_state.json`、`offload_events.jsonl`：本次仿真设备状态与原网关事件。
- `health_snapshots.jsonl`：带时间与范围标记的实际健康采样。
- 模型模式另有 `runtime_manifest.json`、模型服务日志、`model_backend_records.json` 与 `planning_recovery_records.json`，保留启动参数、文件哈希、实际HTTP与恢复尝试。

这些记录属于开发者自测或自动化证据，不构成人工标注、外部用户试用或真实家庭部署证明。

## 回归验证

```powershell
python -X utf8 -m pytest docs/demo_v3/test_run_live.py -q
node --test docs/demo_v3/dashboard/test_dashboard.cjs
```

启动/HTTP测试覆盖真实子进程CLI、真实C规则成功与复杂任务失败、页面路由、原状态和事件保护、Host/Origin边界、巡检禁用、令牌停止、未serve关闭、重复目录/根外路径拒绝、端口冲突清理，以及模型启动失败清理。

模型模式集成测试使用随机loopback端口的受控HTTP夹具代替模型进程，只模拟上游OpenAI兼容响应；实际运行原Direct适配器、原C选择、恢复适配器和JSON执行器，检查一次503后一次替代请求、一条网关事件以及健康/推理原始记录互不串入。它验证接线和异常边界，不证明真实模型准确率。真实模型性能引用另行冻结的评测结果。

RED输出保存在 `verification/live_red.txt`，并发记录缺陷的独立RED输出在 `verification/live_recording_race_red.txt`；修复后的实际回归输出在 `verification/live_green.txt`。

2026-11-11实际验证：本入口7项测试全部通过；与runtime、规划恢复、竞态注册及原隔离入口合并回归为43项测试通过，另72个subtests通过。页面后续补充来源呈现测试，目前12项JavaScript行为测试全部通过。完成块与规划器执行前接口答复分开展示，路由同时显示C初选、实际规划来源与恢复首端错误。浏览器视觉及断连验收由主代理独立执行，不能以这些自动测试替代。
