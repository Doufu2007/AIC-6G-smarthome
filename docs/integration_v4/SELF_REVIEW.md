# Task 1 收尾自审

已实现并验证正式网关可选 `receipt_executor`、Dashboard 的 MQTT 配置接线、原动作白名单预校验、SQLite 先记未知再发请求、命令/平台/原文/动作哈希绑定、未知只查、部分终止，以及 GET 回执入口。

本次自审按实际新增差异逐项检查；没有还原或提交工作区已有改动：

- 事务和重启：网关 reservation 在网络调用之前 commit；同 ID 并发不能插入第二行；重启后 unknown 只调用 lookup。实体状态与回执同一事务提交。7 个 durable tests 覆盖重复/三种冲突/丢 ACK/中断/部分/9 种矛盾/缺失 ID。
- 真协议：paho 的命令采用 QoS 0，关闭自动重连，应用层没有 submit retry；AMQTT 仅 loopback。实体独立子进程，从 SQLite 重启后仍返回原 receipt。实际集成离线读取 entity wire logs，逐 ID 确认 submit 只有一次。
- 原网关：发送前仍执行 `_actions_safe` 和完整 `execute_device_actions`；MQTT 实体重复白名单校验。远端执行未知或异常为终止状态，不进入规则回退。原规则错误通过 BackendResult 保留，避免顶层 error 被 None 覆盖。
- 主代理指出的历史回执镜像问题已修复：当前界面只读取独立 snapshot，不重放 receipt.actions。已测较新的关灯之后再取旧开灯 receipt，当前镜像与实体保持关灯、version=2。
- 旧巡检/离家场景曾直接写模拟状态，开启 durable MQTT 模式时明确停用；关闭该模式时旧行为兼容。新回归确认不绕过白名单、不修改状态。
- 进程与证据：context 只 wait/terminate 自己的 Popen handle；正常结果全部 exit=0，无强制终止。首次失败与中间运行不覆盖，最终入口 `LATEST.json`。所有最终生产源码 SHA256 与运行 manifest 相符；`git diff --check` 和新增 Python 编译检查通过。

验证：网关 unittest 155 passed；Dashboard unittest 49 passed。最终四组实际软件集成覆盖六类场景，离线原始消息与双数据库复核通过。裸工作区 `pytest -q` 实际 exit=2，36 项收集错误，原因是历史解压副本重复模块名和 docs 测试导入冲突；完整日志保留，不能称全工作区 pytest 通过。

适用边界：无物理设备、WAN/6G、生产认证、TLS 或断电实验；默认匿名 broker 仅本机 loopback。当前软件平台快照属于独立软件观测，不能用它把未知命令标为成功。部分执行场景用固定双动作协议探针，不能称真实模型推理。依赖版本与新目录/离线安装方法见 README 与 requirements-lock.txt。

后续隔离复核发现：此前 integration runner 在重启 bridge 时未保持 `SENSEHOME_EVENT_LOG`，重启后的少量 `integration-1` 事件追加到了原项目事件日志。正式设备状态未变。主代理保留污染行与失败会话，并按预先记录的 SHA256 恢复唯一匹配前缀，记录位于 `docs/operations_v4/event_isolation_repair/`。本代理已把场景全生命周期的事件路径显式固定到隔离目录，并新增原状态/事件文件前后 SHA256 检查。修后新例程 `20261111T070837Z_7cfbfcf3` 六类通过，`protected_files.json` 确认两原文件完全不变。此前运行不得作为“原事件文件未变”的证据。
