# 服务并发、请求账本与 deadline 独立审查

审查范围：`bounded_http.py`、`command_requests.py`、bridge 的 `process_http_command`/`lookup_http_request`、`PlanningRecoveryBackend` 的 total-budget 分支，以及 `fused_eval.py` 的 `DirectModelBackend`/`exchange`。仅使用私有 TCP、临时 SQLite 和记录型边界探针，不使用云模型、桌面界面或原设备服务。

发现共 4 项；均已实时反馈主控。原审查为只读，发现 3 经主控明确授权后由审查者修复 `durable_receipts.py` 与 `mqtt_receipts.py`，其余由主控修复。本报告保留修复前的可触发证据和复验结果。

## 1. P1：慢响应头可突破总 HTTP deadline，耗尽备用窗口

位置：`docs/deployment/fused_eval.py:68`，原 `getresponse()` 调用；其消费者为 `DirectModelBackend.infer` 与正式 10s/4s 规划恢复路径。

修复前，`remaining()` 只在 `getresponse()` 前设置一次 socket timeout；`http.client` 读状态行及响应头时可被小字节持续喂入，每次 recv 都在相对超时内，导致整个头部阶段超过绝对 deadline。私有 TCP 服务先写状态行，再每 0.04s 发一字节，共 14 次；传入预算 0.12s，实测 `exchange` 用时 0.580s 才返回 `TimeoutError`。后置超时检查会丢弃迟到动作，但首端仍能占用备用窗口和 HTTP worker。

主控已添加整个 exchange 生命周期的 socket shutdown watchdog。审查复验 `test_http_absolute_deadline_covers_slow_dripping_headers` 通过；该用例与服务/回执 deadline 集合共 8/8 通过。旧 launch-only 兼容路径未作为缺陷；正式启动已显式使用 total 10s、primary 4s。

## 2. P1：GET 巡检绕过统一请求账本与 deadline

位置：`openvela_smart_home/dashboard/bridge_server.py:1300`，以及 isolated 入口 GET 允许列表。

审查时 GET `/api/patrol/trigger` 直接调用 `run_environment_patrol()`；巡检内部生成新命令 UUID 并可能执行灯光/窗帘动作。该路径未经过 `process_http_command` 的请求 ID 预留、4 槽准入或 10s deadline，且外层固定返回 `status=ok`。重复 GET 会形成新的提交。

主控在审查期间改为 GET 405，仅保留 POST 统一请求流程，并报告私有入口已观察到 405；审查者回看当前代码确认该分支只返回 `USE_POST_WITH_STABLE_COMMAND_ID`，不再执行巡检。

## 3. P1：执行入口检查之后，数据库/发布锁等待仍可造成迟到提交

位置：`SenseHome-6G/net_offload/durable_receipts.py:156` 起的首次发送路径、`mqtt_receipts.py:214` 的发布准入路径。

bridge 在 `_gateway_action_executor` 入口检查 deadline 后，`SQLiteReceiptExecutor` 仍可能等待 SQLite 写锁。修复前探针持有真实 `BEGIN IMMEDIATE` 锁 0.23s，请求 deadline 仅为当前时间后 0.06s；释放后仍出现一次 `transport.submit`，提交时间在 deadline 后 0.237s。第二个失败用例锁住真实 MQTT transport 的发布锁直至预算过期；修复前实体仍返回 applied。

经主控授权，审查者实施以下修复：持久 reservation 完成后复检可信 deadline；MQTT 新增可选 `submit_before(payload, deadline_monotonic)`，在发布锁准入后及实际 publish 前复检；旧一参 stub 的 `submit(payload)` 保持兼容。过期保留持久 unknown 预留，错误为 `DEADLINE_EXCEEDED_BEFORE_SUBMISSION`，同 ID 后续不会变成重发。deadline 不进入既有 payload/哈希合同。

新增 `test_receipt_deadlines.py` 的 2 项测试先均失败，修复后通过。SQLite 等锁与真实 MQTT 发布锁过期均为 0 次发布；及时的新命令仍正常 applied，实体版本只增加一次。重新打开数据库及给同 ID 新预算也不能发送原过期命令。

复验结果：原与新 `*receipt*.py` 12/12，通过 pytest importlib 的新增测试 2/2；MQTT 生命周期 7/7。此前网关回执边界旧用例也在本次 12 项集合中通过。

## 4. P2：临时无法验收当前状态会把请求永久封存为未确认完成

位置：`SenseHome-6G/net_offload/command_requests.py:79`、`openvela_smart_home/dashboard/bridge_server.py:1242`。

修复前 `RequestJournal.execute` 只把 execution unknown/partial 视为待恢复；lookup 则在获得 applied 回执后直接标记 request finished，即使 `_sync_mqtt_state()` 为 None、全部目标尚未验收。后续 lookup 直接返回缓存 finished，无法在连接恢复后重新读取当前状态。

使用临时 journal 的控制流探针：原请求 unknown，第一次回查得到 applied 但 snapshot=None，第二次已提供灯光 60% 的正确快照；修复前两次均为 finished/task_complete=False，receipt lookup 仅 1 次，第二个快照未被消费。

主控已新增 `observation_pending`，保留只查询的验收续接。相同探针复验：第一次为 observation_pending/False；第二次为 finished/True，查询共 2 次，无规划或 submit。正常终态和不支持能力的部分结果仍可明确结束。

## 已核查且未发现新增实质问题的路径

- 请求账本在调用 operation 之前提交 ID、原始命令和公开 context 的绑定；重复 ID 及重启中断路径不会再次调用规划器。`_active` 在进程内与预留事务共用 guard，重复请求可得到 processing。
- 公开 HTTP context 的私有字段被移除，内部 deadline 由服务端重新注入；设备目标仍由原始/服务端合并命令的目标合同校验，不以模型自报完成作为验收。
- 有界 HTTP worker、4 槽命令准入、C 策略锁和 DirectModelBackend 的单槽模型准入各自承担不同资源边界；健康与缓存状态查询无需等待推理槽。
- total-budget 包装器在首端、备用健康检查、备用规划之后检查绝对 deadline，并清除迟到结果的 actions；实际发送边界现由发现 3 的复检补齐。回执等待可以在已提交之后继续，不能由此推导发生迟到新提交。
- DirectModelBackend 的每次调用使用独立 RecordingService，记录列表及观测分别加锁；健康采样使用独立 service，不与推理交换日志串包。

本审查没有验证真实模型性能、远程网络、恶意本机流量或多进程共享未加部署锁的独立 RequestJournal；这些不在本轮成功结论内。项目级测试与保护文件哈希由主控统一核查，本报告不替代最终集合结果。
