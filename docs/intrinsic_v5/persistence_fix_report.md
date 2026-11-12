# F10 完整应用持久重启修复记录

本工作包已接通 `IntegratedSession(..., data_dir=None)` 和 CLI `--data-dir`。显式传入稳定目录时，完整退出后再次启动会复用实体状态、实体命令、网关回执、网关缓存和规划前请求账本；省略参数时继续使用全新隔离演示。

实现位于 `docs/demo_v4/live_v4.py`，回归位于 `docs/demo_v4/test_persistent_runtime.py`，部署步骤见 `persistent_deployment.md`。`LocalMQTTSession(data_dir=...)` 及 v3 的 `state_dir` 由另外两个工作包实现，本工作包只消费接口，未编辑这两个模块。

持久数据放在 `data_dir/mqtt/`、`data_dir/bridge/`。每次启动仍新建 `_work/demo_v4/<run>/` 与 `_work/demo_v3/<run>/` 保存运行标记、进程日志和事件。`schema.json` 绑定 v1 格式、软件平台和执行模式；未知/损坏格式、无 schema 的非空目录、执行模式混用均拒绝自动接管。操作系统独占锁在启动 broker 之前取得，并在应用及其所拥有进程关闭后释放；第二个应用进程会在使用数据库之前被拒绝。

MQTT 持久模式在返回就绪前取得实体当前快照并更新 bridge 缓存，避免将历史回执或过时缓存当成设备事实。对账失败则停止本轮启动并清理自有进程。启动只读取当前状态，不提交旧命令；unknown 保留，后续仅回查。运行清单的 `persistence` 提供数据目录、是否恢复已有目录、状态版本与对账来源。

失败回归先于实现运行：4 个新增用例均因入口缺少 `data_dir` 而失败，记录在 `_work/intrinsic_fixes/persistence_red.txt`。源文件原字节备份位于 `_work/intrinsic_fixes/persistence_baseline/live_v4.py`，SHA256 为 `330206034290fc295f8396e0e5613a495e897dc18ef79923c9612849f29d8b1a`。

最终执行：

```powershell
python -X utf8 -m unittest discover -s docs/demo_v4 -p test_*.py -v
```

结果：**6/6 通过，17.055 秒**。完整输出 `_work/intrinsic_fixes/persistence_green.txt`。其中 2 个为已有 v4 规则/HTTP/MQTT 验证，4 个新增回归覆盖：

| 用例 | 实际检查 |
|---|---|
| JSON 整体重启 | 首轮将客厅灯设为60%，完整关闭后启动第二轮仍为60%；请求账本在稳定目录 |
| MQTT 整体重启 | 首轮真实 MQTT 应用60%；终止本测试拥有的实体子进程后发20%，形成真实unknown；两轮间故意把 bridge 缓存改为0%；第二轮从实体恢复60%，已执行回执仍为applied，未知回执仍为unknown |
| 恢复不重发 | 第二轮回查及同ID请求后，实体版本不变；实体库只有1条已提交命令；第二轮实体事件不存在submit |
| 目录与schema保护 | 真正启动第二个 CLI 进程验证同目录拒绝；第一轮关闭后同目录可启动；未知schema拒绝且原文件字节不变，失败初始化释放锁 |

首次集成运行曾在测试清理阶段因测试自身未关闭 SQLite 连接而失败；测试连接已改为 `contextlib.closing`，后续两次完整集合均通过，无 ResourceWarning。MQTT 生命周期工作包已交叉审阅持久路径、schema、锁和清理顺序，未发现需修改的实质问题；本工作包也审阅了 MQTT 连接/订阅恢复、等待者释放与关闭竞态。

全部可变验证数据仅位于 `_work/`，未启动模型、未操作 UI、未连接外部设备。原 `openvela_smart_home/device_state.json` SHA256 为 `22a1550ab65c3db310291c778f67e9d0b3a8b19838a1ebc6fd40ca79f33e929d`，原事件日志 SHA256 仍为 `f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54`；成功运行清单均记录 `original_state_and_events_unchanged=true`。未修改历史封版证据，未 commit/push。

整库回归和其他 F01—F11 的集成检查由主控统一记录。本报告的完成结论仅针对本机软件应用持久重启，不证明物理设备执行或物理 exactly-once。当前是首个稳定数据格式，没有旧持久部署格式可自动迁移；未知版本必须经过显式版本迁移后才能启动。
