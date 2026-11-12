# F01 / F02 / F03 / F09 实施记录

本区使用 `systematic-debugging` 与 `test-driven-development`：先复现合法错计划、规则死路、无会话续接及场景断路，再修改合同与执行边界。初始 `test_language_contract.py` 的10个测试产生11个断言失败及1个缺失接口错误；后续又分别先复现旧快照验收、远端过期计划错误被备用覆盖、直接启动/离线打包缺模块、空计划仍声称完成的问题，再修复。

## 最终接口

- `SenseHome-6G/net_offload/target_contract.py`：`request_goals(command)` 返回完整有序目标，无法证明完整理解时返回 `None`；`validate_goal_plan` 对全部动作的设备、房间、有效数值和顺序进行精确比对；`accept_goals` 用执行器提供的可信当前状态验收全部最终目标。
- `OffloadGateway` 在远端提交前独立重复合同校验，合同拒绝为终止错误，不能经过本地备用绕过。规则返回 `UNRESOLVED_NO_NLU` 可再次按 complex 选择模型。模型响应不具有执行或目标验收权限。
- `_gateway_action_executor(actions, context)` 必须获得服务器设置的 `_receipt_command`；显式设备API及服务器场景可提供私有 `_server_goals`。整个动作批次先通过原设备白名单，再通过请求合同，随后才提交。
- `process_command(command, context=None, internal=None)` 丢弃外部所有以下划线开头的 context 字段；仅从 `internal` 注入 `_deadline_monotonic`。执行锁内、动作提交前检查截止时间；过期返回 `DEADLINE_EXCEEDED`，不再触发备用提交。
- 执行结果区分 `plan_valid`、`execution_complete`、`task_complete`。网关最终 `semantic_complete` 保守等于 `task_complete`。`device_actions.semantic_complete` 为兼容既有软件回执协议，仍只表示动作批次已应用；新增 `execution_complete=True`、`task_complete=False`，不能单独用于用户目标验收。
- MQTT任务完成需要已应用、已绑定的动作回执，以及独立当前状态查询。查询版本必须不小于回执的 `state_version_after`；无快照、旧快照、部分回执、目标状态已改变均不能完成任务。软件实体确认不代表物理执行确认。

## 澄清与场景

`ClarificationSessions` 根据 `context.session_id` 隔离待补房间/亮度槽位，默认300秒过期，最多1024个会话；不提供ID时只进行单轮澄清，不保存隐含目标。`有点暗 → 卧室 → 调到60%` 由服务器合成为明确请求后执行。不同会话互不继承；取消、过期或新独立指令清除旧意图；新相对调节不能继承上个相对意图的旧数值。相对亮暗不再自行猜60%/30%。适配器只读温湿度的确定性补计划仍保留并记录来源。

`离家模式` 通过统一白名单/命令ID/回执提交五个室内房间的关灯及关帘，共10个动作。结果提供 `supported_scope_complete`、`partial_complete` 和 `unsupported_capabilities=['security_arming']`，不宣称安防已布防。显式安防控制仍整请求拒绝。

`run_environment_patrol(context=None)` 至少提交五个房间的 `read_temperature`，经统一命令ID和回执返回温湿度检查结果。只有当前可信光照/人体观测满足条件时才产生控制动作；MQTT实体没有提供的传感器列入 `unavailable_sensors`，不通过刷新随机仪表数据伪造观测。报告区分 complete/partial/failed，原始结构化动作在 `execution_actions`。

## 验证结果

全部测试通过 `docs/intrinsic_v5/run_private_tests.py` 或测试模块自己的导入前隔离设置执行，状态、事件及SQLite均使用私有临时目录。未启动UI、未调用真实模型、未修改历史封版结果。

- 最新新增语言回归：`python docs/intrinsic_v5/run_private_tests.py docs/intrinsic_v5/test_language_contract.py -q --tb=short`：19 passed，3 subtests passed。最后一项先复现“验证期间截止时间到期仍提交”的失败，再在真正提交前补第二次deadline检查。
- 适配器/合同/执行合跑：`python docs/intrinsic_v5/run_private_tests.py docs/intrinsic_v5/test_language_contract.py SenseHome-6G/net_offload/tests/test_openai_backend.py SenseHome-6G/net_offload/tests/test_model_command_contract.py SenseHome-6G/net_offload/tests/test_gateway_action_execution.py SenseHome-6G/net_offload/tests/test_receipt_gateway_boundary.py -q --tb=short`：48 passed，21 subtests passed（当时语言回归18项）。
- dashboard全组与 gateway/offload/receipt定向合跑：66 passed，33 subtests passed。随后加强历史回执测试，确认真实经过receipt调用且任务验收拒绝旧目标：该组4 passed。
- 恢复打包与语言合同合跑：38 passed，3 subtests passed（当时语言回归17项）；离线zip提取后的适配器 `--help` 与源码直接启动均成功，未启动模型。新增模块已加入 `prepare_autodl_recovery.py` 打包映射。
- 扩展 `net_offload/tests + dashboard/tests + language_contract` 合跑曾为225 passed、1 failed、78 subtests passed；唯一失败是固定合同测量stub缺少新完成字段。随后为该stub补上有明确 `isolated-fixed-contract-stub` 来源的验收字段，定向复验 `test_experiment_concurrency.py + language_contract` 为17 passed，3 subtests passed。最终全项目结果由主控集成记录覆盖；本报告不将该次旧失败合跑改写为全绿。

## 保留边界

目标抽取是有界、完整匹配的语法，不是通用自然语言理解。语法以外的计划不能靠模型自证可信；会升级模型或请求明确目标。澄清状态是短期内存状态，重启不会恢复未完成的含糊意图。此区MQTT正负验收新增测试使用真实SQLite软件实体及本进程传输接口，实际broker断连/重连及HTTP集成由其他工作区验证。未对真实模型错误率或真实物理设备完成率作结论。

## 独立复核 P2：提交前持久保存服务端解析与目标合同

独立复核发现：三轮澄清的最后原文是“调到60%”，服务端实际执行“把卧室灯光设为60%”。若动作提交成功而请求结果尚未写入时进程中断，旧请求账本仅保存原文，无法恢复用户目标。服务器离家/巡检计划也存在同类恢复缺口。

修复在 `command_requests.py` 新增兼容的 `request_resolutions` 表，以原始请求哈希绑定同一个命令ID。`RequestJournal.bind_resolution()` 在规划前不可变地绑定服务端 `resolved_command`，在执行边界不可变地绑定已通过完整校验的目标列表、`supported_scope` 及 `unsupported_capabilities`。重复绑定相同内容允许，不同房间、数值、文本或能力范围均拒绝覆盖。新增表不修改既有请求表和历史请求内容。

`RequestJournal.execute()` 使用 `ContextVar` 关联当前服务器执行与账本；桥接器通过 `bind_active_resolution()` 保存目标。该关联不进入公开context、模型请求或事件JSON，公开 `resolved_command` / 私有伪造字段无法授予目标权限。执行退出或抛出模拟进程终止异常后均清理关联，并发请求的关联分别隔离。目标持久绑定完成后，再检查提交截止时间。

崩溃回查只使用可信持久合同；没有合同的旧记录仅可解析账本中的原始明确命令，不能从模型结果或回执动作反推用户目标。P1原有“当前观测版本必须覆盖回执版本”的门保持不变。离家恢复仍保留 `security_arming` 不支持，只能报告支持范围完成和整体任务未完成；巡检读目标与明确三轮续接可以恢复验收。

验证先新增5项失败回归，确认原4项通过、新5项失败，再实现持久绑定；另补并发关联隔离检查。`test_request_reconciliation.py` 最终共10项：三轮续接提交后崩溃、离家能力范围恢复、巡检读目标恢复、不可变绑定（含能力范围）、公开context不可授权、并发隔离，以及原有4项查询/观测版本回归。

最终命令：`python docs/intrinsic_v5/run_private_tests.py docs/intrinsic_v5/test_request_reconciliation.py docs/intrinsic_v5/test_language_contract.py docs/intrinsic_v5/test_service_lifecycle.py openvela_smart_home/dashboard/tests/test_durable_receipt_bridge.py --import-mode=importlib -q --tb=short`，结果 **39 passed，3 subtests passed**。崩溃测试使用私有SQLite软件实体，真正提交完成后抛出 `BaseException`，重新打开账本并清空会话状态，仅回查而不重发。此补修未调用真实模型或真实物理设备。

生产实现及相关测试已冻结，交由主控统一验证；本段为最后一次报告更新。
