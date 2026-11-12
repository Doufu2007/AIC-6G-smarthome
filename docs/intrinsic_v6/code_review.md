# V6 修复独立审查结论

**裁定：当前审查范围内可接受；无未关闭的 Critical / Important / Minor。** 审查过程中发现的 1 项 Important（原始空/错误读取计划经规则修正后虚增模型规划质量）已由 `93953a9` 修复并独立复核；早期发现的 SQLite 并发迁移 Minor 也已在主控当前工作树修复并复核。最终全量运行、提交与上传由主控执行。

## 范围与比较基线

- Task 1：`6086546` 的回执和请求恢复，及主控追加的迁移事务修正。
- Task 2：`9a9f02a` 的可信传感器采样、损坏状态保留、巡检线程生命周期。
- Task 4A：`37f09a7` 的有界中文目标及规划成本/质量计数，连同 `93953a9` 的原始计划质量修正。
- 主控当前工作树的 HTTP mixin 接线、command/patrol 共用 context 校验、完整显式计划本地编译。

已读修复 design、Task 1/2/4 brief 与各实现报告。源码增量对照 `_work/intrinsic_v6_fix/prechange/manifest.json` 的备份；初始 HEAD 之外原有 dirty 内容不算本轮新增缺陷。审查者未修改生产源码、测试源码、Git 索引或 HEAD；只在 `_work/intrinsic_v6_fix/` 写审查报告及复现脚本。

## 缺陷裁定

### Critical

无。

### Important

当前无未关闭项。已关闭的问题如下：

**原始空/错误读取计划被服务器修正后获得有效模型规划信用。** 原实现 `fused_eval.DirectModelBackend.infer` 仅以规范化后的 `result.actions` 判断 `valid_plan`。对于“查看客厅温度”，模型 `actions=[]` 或错误的卧室读取均被既有 `high_confidence_command_semantics` 修成客厅读取；原始空计划的单次指标得到 `valid_plans=1 / valid_plan_rate=1.0`。这违背空模型计划不得计为有效规划的验收。

已由 `93953a9` 修复：存在 `raw_model_actions` 时以其计算模型质量，并经过动作 schema 和完整目标合同校验；服务器修正后的可执行计划不再给原始空/错计划质量信用。独立脚本：

```powershell
python -X utf8 _work/intrinsic_v6_fix/review-planner-raw-probe.py
```

修复后输出保持两次规范化后的客厅读取，且：

```json
{"planner_attempts":2,"planner_completed_attempts":2,"planner_response_ok":2,"valid_plans":0,"valid_plan_rate":0.0,"samples":2}
```

实现者新增回归还验证之后的原始正确读取计划让 `valid_plans=1`、分母为3、有效率为1/3。已独立运行通过。

### Minor

当前无未关闭项。早期 `RequestJournal.__init__` 的 schema 检查与 ALTER 未持有共同写事务，两个实例同时升级旧 7 列数据库可触发 `duplicate column name: result_revision`。当前工作树已在 schema 检查前使用 `BEGIN IMMEDIATE`。独立复核以私有旧 schema、两个连接在进入构造器事务前同步放行，结果 `['ok', 'ok']`，最终仅存在一列 `result_revision`。原 RED 证据和探针在 `review-early.md` / `review-migration-probe.py`；该旧探针的 barrier 在 schema 读取之后，仅用于复现原实现，不能直接用来测试新增迁移写锁，否则会人为锁住测试。

## 行为审查结论

**完整目标语法与保护：** 三条审计同义表达得到相同60%客厅目标；规范中文整数百分数边界、完整有序多目标与重复设备的明确时序被保留。无序异值冲突、条件、否定、部分解析、未知房间与超界值不获得服务端动作授权。显式编译再次运行完整语法，随后仍经过动作白名单、目标合同、deadline、持久回执和当前状态验收。两房间百分比回归证明0次模型调用、1次提交及同ID重放不再提交。没有发现模型能借部分解析扩大目标的本轮缺陷。

**遥测：** admitted、completed、响应ok、有效原始计划、成本样本分别计数；有效率分母为完成的已准入规划调用。失败与空计划仍产生规划调用耗时样本。字段明确 `planner_call_only`、不含设备执行、描述性质量计数；`usable`是成本新鲜度，不是任务可靠性证据。未从小样本硬编码模型偏好。

**传感器：** `snapshot.sampled_at` 明确为数据库读取时间、`sensor_samples_refreshed=False`；自动控制只读取每个房间、每个信号独立的 Unix `sampled_at/source/status/trusted`。30秒上限和类型/数值校验不会把旧 `update_time` 或新 snapshot 时间当成新测量。旧初始化 motion 不会撤销显式卧室开灯；缺失、畸形、陈旧及未来时间逐字段降级，可信新鲜软件夹具仍可触发对应动作。当前 source 是软件协议契约，不等于硬件来源认证。

**状态保护：** 缺失文件允许首次默认值；已有坏JSON、坏核心schema或不可读文件明确报错。保存前验证现有文件并保留坏字节，避免下一次动作以默认值静默覆盖。传感器本身畸形不重置有效设备组。

**巡检生命周期：** 每轮独立 Event，控制操作串行；停止唤醒等待，旧轮仍在执行时拒绝新线程；异常在 `finally` 清除归属和运行状态。停止时 join 超预算会返回false，并继续拒绝重启直到旧线程退出，不会同时启动两个循环。没有忙等待或通过新 start 清除旧 stop Event 的问题。

**回执/HTTP集成：** 重新运行8项回执回归通过；历史完成与当前观察独立、旧revision不能回写、已应用回执后的缓存异常仍可同ID查证。HTTP hook现已接入；普通 ThreadingHTTPServer 使用默认读取预算。非法 context 在准入前返回结构化400。输入读取绝对预算不因滴字节重置，读取完整后的处理不受输入预算误杀。

## 独立运行的验证

| 验证 | 最终结果 |
|---|---|
| `test_request_reconciliation_v6.py` | 8项通过（Task2/主控集成后重新运行） |
| `test_sensor_patrol_v6.py` | 10项通过 |
| `test_language_metrics_v6.py` + `test_model_command_contract.py`（`pytest --import-mode=importlib`） | 14 passed，28 subtests passed（93953a9对应修正后） |
| `test_explicit_compilation_v6.py` | 4项通过 |
| `test_patrol_context_v6.py` | 2项通过 |
| `test_http_read_deadlines_v6.py` | 5项通过（getattr兼容修正后再次运行） |
| 16个持续滴header字节的私有loopback连接 | 16/16按预算关闭，随后健康请求200，服务线程正常退出；见early报告 |
| 原始计划质量独立探针 | 两次服务器修正规划，0个有效原始模型计划 |
| 两实例并发首次旧schema迁移 | 两者成功，schema无重复列 |

审查中一度恰逢实现者新增遥测 RED 测试并使用错误测试属性，随后实现者已修正；上表是修复后独立实跑结果，不将中间测试笔误作为生产缺陷。

原状态及事件 SHA-256 仍与 prechange 一致：

- `device_state.json`：`22a1550ab65c3db310291c778f67e9d0b3a8b19838a1ebc6fd40ca79f33e929d`
- `offload_events.jsonl`：`f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54`

## 未查或范围外

1. 未独立重跑全量活动NET/Dashboard/部署测试；只执行上列定向回归与私有探针。全量集成验收仍由主控负责。
2. 未调用真实模型、GPU、既有/外部MQTT broker、既有设备服务或实体硬件；这些测试不证明真实模型质量改善、实体感知/控制、安防能力或6G链路表现。
3. 未做磁盘损坏与文件外部篡改同时发生的跨进程故障注入，未对多进程运行中的完整请求执行作系统级恢复证明；迁移并发只验证首次schema升级。
4. 未重审历史封版、论文/部署资料或前端如何展示新增当前观察字段；未改历史审计探针、封版或历史数字。
5. 最终部署、后台常驻行为、仓库上传与远端CI不属此只读代码审查范围。

## 主控最终小增量复核

按主控要求只读核对，未重跑已通过套件。**未发现为绿化测试放宽安全断言，未发现新增 Critical / Important / Minor。**

- `OffloadGateway.handle` 在覆盖有效 `reason=SERVER_EXPLICIT_GOALS` 之前捕获 `selected_backend` 与 `decision_reason`；显式计划的 `task_class=simple`、`decision_reason=TASK_IS_SIMPLE` 与 C 分支一致。既有 `replay_captured_policy` 以 task_class / captured observation 重放，再比较 selected_backend / decision_reason；服务端有效路由文案不再伪装成 C reason。此处为源码与重放合同静态核对，没有新跑 C 重放。
- bridge 只在完整 `request_goals` 可解析时传 `prefer_explicit_rules=True`，同时转交已构造的服务端 observation。gateway 对 explicit simple 忽略该 observation；故意去掉 prefer flag 的下层测试仍能取得原 observation，不会意外切换到另一条资源决策路径。公开 context 无法开启私有 flag或替代服务器采样。
- HTTP command/patrol 共用对象 context 校验；显式 JSON null、数组、字符串、布尔或数字均在请求准入前拒绝。BridgeHandler 的 mixin 在 BaseHTTPRequestHandler 前，输入deadline接线保留。
- 错模型计划合同测试改为直接调用真实 `gateway.handle`，仍严格断言状态不变、task失败、无fallback和 `REQUEST_GOAL_PLAN_MISMATCH`。恢复/阻塞HTTP测试的局部包装仅移除服务端 prefer flag，保留模型、动作合同、回执、请求去重与状态验收；仍断言实际POST次数、恢复来源、最终目标值和资源释放。
- 默认资源路由测试改用未给明确数值的请求，继续检查C选择的后端、reason、server observation与客户端参数不能覆盖决策；新增共同断言要求模型猜出的百分比被整体拒绝、动作空、状态不变。完整显式请求另测本地成功且0次模型调用。rules-only正式入口同时保留完整显式成功和unresolved失败，未把任意模型/规则猜测改判为成功。

模型合同、下层恢复路径与正式显式入口分别有对应断言；测试调整反映新入口行为并保留原安全边界。最终全量结果由主控在当前运行完成后登记。
