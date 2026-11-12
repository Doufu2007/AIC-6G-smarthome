# V6 早期独立代码审查

审查对象：Task 1（提交 `608654622a000468d28244458808272fc8853eb2` 对应工作树）及 HTTP 截止时间模块（`818c16c` 对应实现）。比较基线为 `_work/intrinsic_v6_fix/prechange/manifest.json` 指向的备份，不把任务前已有 dirty 改动归咎于本次。已读 V6 修复设计及 Task 1 brief。未修改生产源码、测试源码、Git 索引或 HEAD。

## 裁定

**Task 1 无 Critical / Important，指定修复可接受。存在 1 项非阻断 Minor；主控已确认将修。HTTP 模块定向审查通过，最终 BridgeHandler 接线与普通 ThreadingHTTPServer 兼容属于已知在途集成，本轮不列缺陷。**

### Critical

无。

### Important

无。

### Minor — 并发首次升级旧 SQLite schema 可能抛出重复列错误

位置：`SenseHome-6G/net_offload/command_requests.py`，`RequestJournal.__init__` 中 `PRAGMA table_info(requests)` 后的条件 `ALTER TABLE`（约第 46–47 行）。检查列存在性与添加列没有放在同一写事务中。两个实例/进程同时连接同一个旧 7 列数据库时，可以都读到不存在 `result_revision`，随后一个升级成功，另一个抛出 `OperationalError: duplicate column name: result_revision`。新 schema 和普通单实例启动不受影响；bridge 的 `_request_store_lock` 已覆盖普通进程内请求初始化，因此裁为 Minor。

确定性复现（只有私有临时 SQLite，无生产服务）：

```powershell
python _work/intrinsic_v6_fix/review-migration-probe.py
```

观察结果：

```text
['ok', 'OperationalError: duplicate column name: result_revision']
```

探针复用真实 `_connect()`，仅在两个连接各自读完 `PRAGMA table_info` 后设 barrier，迫使一个合法的并发时序发生；不改源文件。建议在 schema 检查前取得 `BEGIN IMMEDIATE`，让后一个初始化者在取得锁后重新检查 schema。修复后探针 barrier 必须移到锁之前，或改为断言第二个初始化者等待并最终成功，避免测试自身锁住第一个初始化者。

## 已核验行为与证据

- `python -m unittest discover -s openvela_smart_home/dashboard/tests -p test_request_reconciliation_v6.py -v`：8 项通过。覆盖离家灯帘恢复而安防仍 unsupported、旧并发回查不能回退成功、成功历史与更新设备状态分开、快照/UI-cache 异常保留 applied 回执、同 ID 恢复不再次提交、旧 schema 迁移及重启 CAS、SQLite 与 `_guard` 释放顺序。
- `python -m unittest discover -s openvela_smart_home/dashboard/tests -p test_http_read_deadlines_v6.py -v`：5 项通过。请求行、header、body 持续滴字节均不能重置绝对预算；完整 GET/POST 输入后的处理可以超过输入预算；超时释放唯一 worker。
- 补充受控 loopback 探针：16 个 header 持续滴字节连接，header/body 预算 0.22 秒，16/16 关闭，约 0.316 秒后健康请求返回 200，服务线程退出正常。未发现持续占用 worker 或清理泄漏。
- 代码确认 `_DeadlineSocketReader.readinto` 在每次真实 recv 前重新计算同一绝对截止时间；body 读满后解除输入预算，不以输入读取预算定时取消后续 handler 计算。
- 对 `command_requests.py` 与 `bridge_server.py` 的实际增量审查确认：回查不规划、不 submit；revision 比较在 SQLite 写事务内；完成历史保留原回执/actions，当前验收保存独立字段；local-rule 与 model-executor 均经过同一回执后观察异常映射。

## 未查及置于范围外

1. 本轮未独立重跑整个 NET / Dashboard 活动测试集；实现者报告中的全量结果仅作背景，以上 13 项和 16 连接探针是本审查独立实跑证据。
2. BridgeHandler 的 mixin 接线、普通 ThreadingHTTPServer 的默认预算兼容、巡检非法 context 属主控正在整合内容；未完成最终整合验收。
3. Task 2 的传感器可信度/巡检生命周期/损坏状态文件，以及 Task 4 的语言合同/调度指标，尚未审查，不据本报告裁定。
4. 未接入任何既有/外部 MQTT broker、生产设备服务、真实模型或实体硬件；软件 SQLite 与模型响应夹具不能证明实体执行或真实模型性能。
5. 未审查历史封版、部署资料、前端是否充分展示新增 current_observation 字段，也未做跨主机或多进程执行中断的系统级验证。

所有新增可变状态位于 `_work/intrinsic_v6_fix/` 私有临时目录并已清理；唯一保留的审查产物是本报告与迁移复现脚本。
