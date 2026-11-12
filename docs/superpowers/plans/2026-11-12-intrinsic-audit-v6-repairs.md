# Intrinsic Audit V6 Repairs Implementation Plan

> **For agentic workers:** execute task by task with test-first changes; do not commit because the workspace already contains the user's uncommitted deliverables.

**Goal:** 修复V6审计中的软件缺陷，并消除显式命令与规划质量遥测中已复现的可靠性问题。
**Architecture:** 保持现有Python/C/SQLite/HTTP边界；分别强化请求对账、传感器可信度、入站HTTP、状态/线程生命周期、语言合同及质量指标。完整有界语法命中的明确请求走服务器动作计划，不调用模型；其余请求保留模型/澄清路径并执行完整合同校验。
**Tech Stack:** Python 3.13、pytest/unittest、SQLite、原生HTTP server、C策略现有接口。
**Spec:** `docs/superpowers/specs/2026-11-12-intrinsic-audit-v6-repairs-design.md`

## Global Constraints

- 不reset/clean；保留当前工作区其他未提交成果。按任务只暂存相关文件并提交，最终验证后上传用户指定仓库的功能分支，不force push。
- 不改原状态/事件文件、历史封版、冻结测试或其原始证据。
- 所有运行数据放在私有新隔离目录；不启动已有服务或真模型。已有MQTT集成测试可启动其自有临时broker/entity，禁止连接外部或既有broker。
- 不声称物理设备、真实云端或6G已验证；不把夹具结果当模型能力结果。

## Review Focus

- durable回执已存在但UI缓存或状态读取失败时，账本仍保留执行事实并能恢复。
- 两个旧回查交错时，新确认结果不可被旧结果覆盖。
- 传感器字段缺失、过期、类型错误或来源不可信时不得执行自动控制。
- 入站header/body分别超绝对期限、worker释放，但正常执行deadline保持独立。
- 表达归一化不得吞否定、条件、多个相互冲突目标或超范围数值。
- 完整解析请求绕过模型后仍必须核对动作与目标顺序，不能把“模型未调用”当成功证据。

## Task 1: 请求回执和并发对账

**Files:** `SenseHome-6G/net_offload/command_requests.py`、`SenseHome-6G/net_offload/offload_gateway.py`、`openvela_smart_home/dashboard/bridge_server.py`及相关dashboard/net_offload测试。

- [ ] 先为离家模式首次观测失败后可恢复、并发旧回查不可降级、执行后缓存写失败保留receipt写定向测试，并确认红灯。
- [ ] 修复awaiting observation判定、事务版本/CAS reconcile，以及applied receipt之后的异常映射；异常路径不得触发重提交。
- [ ] 运行请求账本、持久回执、网关执行和相关dashboard测试。

## Task 2: 传感器、状态文件、巡检生命周期

**Files:** `openvela_smart_home/dashboard/bridge_server.py`、`SenseHome-6G/net_offload/mqtt_receipts.py`、对应测试。

- [ ] 先新增旧sensor快照不触发关灯、畸形sensor不崩溃、损坏状态不被覆盖、off/on单实例与异常状态恢复测试并确认红灯。
- [ ] 为每字段观测验证真实采样元信息；没有可信元信息则只观察不控制。旧静态软件实体状态不能因为读取时间更新而变成新鲜传感器观测。
- [ ] 区分首次状态文件缺失与已存在损坏；隔离坏文件、报告错误，禁止默认值覆盖。
- [ ] 将巡检线程切换为具备单实例停止事件和finally清理的生命周期；运行相关测试。

## Task 3: 正式HTTP入口输入和请求读取期限

**Files:** `openvela_smart_home/dashboard/bounded_http.py`、`openvela_smart_home/dashboard/bridge_server.py`、`docs/trial_v2/run_isolated_gateway.py`及相关测试。

- [ ] 先新增巡检畸形context返回400与slow-header/slow-body在总期限内释放worker的回归，并确认红灯。
- [ ] 将命令/巡检统一校验为JSON对象context；畸形输入在账本和操作前拒绝。
- [ ] 为header和body加入绝对、单调时钟驱动的读取期限，限定体积并正确关闭socket释放worker；不把入站读取deadline混入已开始的规划/执行deadline。
- [ ] 运行bounded HTTP、正式wrapper和请求输入测试。

## Task 4: 显式中文合同与调度质量遥测

**Files:** `SenseHome-6G/net_offload/target_contract.py`、`SenseHome-6G/net_offload/offload_gateway.py`、`SenseHome-6G/net_offload/backends/openai_backend.py`、`SenseHome-6G/net_offload/online_metrics.py`、`docs/deployment/fused_eval.py`及相关测试。

- [ ] 先新增礼貌前缀、等价显式命令、中文百分数解析以及否定/条件/冲突/超范围拒绝测试，并确认红灯。
- [ ] 对明确等价语法规范化为同一有序server-owned目标合同；对完整解析请求直接编译并执行合同动作，不额外调用模型；保持执行层严格全量匹配。
- [ ] 先新增empty-plan不可计作成功规划样本、尝试数/有效计划数/率分别暴露的在线指标测试并确认红灯。
- [ ] 修复planner计量传入；把成本量的范围明示为规划wall，低样本不提供质量保证。
- [ ] 运行目标合同、模型适配器、资源策略和在线指标测试。

## Task 5: 集成、回归和交付

**Files:** `docs/intrinsic_v5/README.md`、新报告 `docs/intrinsic_v6/repair_report.md` 与机器可读摘要；仅在与新验证一致时更新活动操作文档。

- [ ] 运行全部活动源码测试与必要的历史兼容子集，日志留在唯一 `_work/intrinsic_v6_fix/` 目录。
- [ ] 独立复核所有十项缺陷/边界的触发断言及原protected文件哈希。
- [ ] 编写修复矩阵、命令/结果、剩余限制和真实模型/硬件未重测说明。
- [ ] 最终审查变更范围，确认未覆盖现有未提交成果和历史实验。
