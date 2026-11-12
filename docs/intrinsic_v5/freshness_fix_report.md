# F07 状态新鲜度与 F04 非阻塞状态查询修复

## 最终行为

- `GET /api/state` 调用 `cached_state_view()`，立即读取原子 JSON 缓存和内存元数据；不获取 `device_action_lock`，不同步等待 MQTT。每个 bridge 实例最多有一个后台快照刷新线程，持续轮询不会叠加在途查询。
- 旧灯光、窗帘和温湿度数值保留为最后观测；查询失败、断连、连接代际变化、年龄超过 5 秒、适配器禁用或启动尚未对账时，响应明确给出 `stale=true`、`unknown=true`，且不再声明当前观测。
- `_sync_mqtt_state()` 保留同步验收用途：动作执行和持久启动仍等待真实查询结果。平台绑定、schema、证据种类、非负整数版本、房间集合、字段集合、布尔/数值类型、范围、一致性和版本倒退检查保留；新增真实 `sampled_at` 检查和采样时间倒退保护。无效、过期或断连快照不会被返回给目标验收。
- GET 已读取旧值、后台线程刚写入新快照时，响应不会把新版本的“当前”元数据贴到旧值上。
- 关闭时先停止刷新任务接纳并设置取消标记，关闭 transport 唤醒 MQTT 等待，再限时等待自有线程退出；取消后的结果不写入状态。重新初始化清空运行时新鲜度确认，持久缓存须经实时查询重新对账。

## 接口

`bridge_server.py` 新增：

- `cached_state_view(refresh=True)`：返回状态视图；`refresh=False` 仅读取。无 MQTT 历史的 JSON 模拟模式保持原状态格式。
- `close_state_refresh(timeout_s=3.0) -> bool`：禁止新刷新、取消在途结果并等待线程；`timeout_s=0` 只标记取消。返回 `False` 表示线程仍存活，关闭流程继续负责处理。
- `reset_state_refresh()`：旧线程已退出后重新开启刷新，并撤销持久缓存的运行时可信确认。

主控已将以上关闭/重置接口接入 `close_gateway()` / `_init_gateway()`。辅助实现位于 `openvela_smart_home/dashboard/state_freshness.py`。

状态中的 `_mqtt_observation` 字段：

| 字段 | 含义 |
|---|---|
| `platform_id` / `state_version` | 最后合法观测的平台和版本 |
| `sampled_at` | 实体生成快照时的墙钟秒；未有合法采样为 null |
| `age_ms` | 接收时源采样年龄，加上 monotonic 推进；后续系统墙钟跳变不会改变该观测的年龄轨迹 |
| `stale` / `unknown` | 缓存是否过期，以及当前设备状态是否无法确认 |
| `connection` | transport 非阻塞连接缓存，附 `status`：connected / disconnected / connecting / unknown / disabled |
| `connection_generation` | 采样所属连接代际，重连后要求重新采样 |
| `last_refresh_error` | 最近刷新错误；正常年龄过期时可为 null |
| `refresh_in_progress` | 当前是否有自有刷新线程 |
| `scope` | current-software-platform-observation / last-known-software-platform-observation / unknown-software-platform-observation |
| `physical_execution_confirmed` | 始终 false，本次仍为 MQTT 软件实体 |

首次接收时仍需用源墙钟估计网络传输前已过去的时间；后续年龄只使用 monotonic。当前实体和 bridge 在同一台 PC，没有把该时间解释为真实 6G 链路测量。

## 回归与验证

新增 `docs/intrinsic_v5/test_state_freshness.py`（12 项）和 `test_request_reconciliation.py`（2 项）。测试使用 `_work/intrinsic_v5/` 下的临时状态、事件日志及请求库，bridge 导入前即设置私有环境变量和不存在的策略 CLI，避免导入副作用访问原状态。

先运行失败回归，再实现：初始 9 项按预期失败；真实 HTTP 状态查询随受控慢快照等待约 1.01 秒，且缺少 `sampled_at` 的快照原先可被接受。随后分别先复现并修复旧值配新元数据、重新打开网关沿用旧确认、禁用适配器后缓存仍声明当前的边界。

最终合并执行：

```text
python -m pytest 
  docs/intrinsic_v5/test_state_freshness.py
  docs/intrinsic_v5/test_request_reconciliation.py
  docs/intrinsic_v5/test_language_contract.py
  docs/demo_v4/test_live_v4.py
  docs/demo_v4/test_persistent_runtime.py
  SenseHome-6G/net_offload/tests/test_mqtt_receipts.py
  SenseHome-6G/net_offload/tests/test_mqtt_lifecycle.py
  SenseHome-6G/net_offload/tests/test_receipt_gateway_boundary.py
  SenseHome-6G/net_offload/tests/test_durable_receipts.py -q

55 passed, 15 subtests passed in 47.85s
```

新增恢复回归验证：请求 unknown → 回执 applied 但快照不可用 → observation_pending → 有效快照确认全部目标 → task_complete。测试在 `__call__` 与 `submit` 上设立即失败守卫，确认恢复只回查，未再次提交动作。`RequestJournal` 和请求回查实现由主控维护，本工作包仅增加回归。

完整日志与安全校验：

- `_work/intrinsic_v5/freshness_related_tests.txt`
- `_work/intrinsic_v5/freshness_tests_safety.json`：退出码 0，原状态与原事件日志前后哈希一致。
- 根目录仅运行 `python -m pytest --collect-only -q`，395 项收集、45 项错误，全部为历史 `source_snapshot` / `frozen_source` 与正式目录同名测试引起的 `import file mismatch`。未执行历史快照测试；详细错误逐项保存在 `_work/intrinsic_v5/freshness_pytest_collect.txt`。
- `_work/intrinsic_v5/freshness_collect_safety.json`：收集退出码 2，原文件前后哈希一致。原 `device_state.json` SHA256 为 `22a1550ab65c3db310291c778f67e9d0b3a8b19838a1ebc6fd40ca79f33e929d`；原 `offload_events.jsonl` SHA256 为 `f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54`。

本结果覆盖受控慢查询、断连缓存、结构/版本保护、关闭、重开、墙钟漂移、HTTP 查询可用性与现有私有 MQTT/持久应用路径；不声明真实物理设备或真实模型的性能改善。
