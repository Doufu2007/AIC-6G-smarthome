# 第三轮竞态恢复实验：注册前独立审查

审查日期：2026-11-11。范围为 `docs/demo_v3/race_matrix.py`、`runtime.py`、`planning_recovery.py`、对应测试，与第三轮设计的契约。未运行正式矩阵，未修改实现或旧证据。

## 裁定

**可按既定设计注册并运行一次；当前未发现阻塞性代码问题。** 这是运行前的代码与合成HTTP契约检查，不是正式36行实验结果，也不能替代运行后逐行审计。

## 已执行验证

1. `python -m pytest docs/demo_v3/test_runtime.py docs/demo_v3/test_race_matrix.py docs/demo_v3/test_planning_recovery.py -q`：**33 tests、72 subtests通过**。包括恢复资格、拒绝/非法输出终止、第二失败不递归、launch budget、原网关执行合同、运行时完整性和生命周期检查。
2. 额外启动两个真实回环 `ControlledProxy` 和一个合成OpenAI HTTP服务，使用生产 `DirectModelBackend` 与实际冻结C04命令进行适配验证。该服务明确是fixture，不是模型：健康检查实际GET成功后把第一代理设为POST503，原始 `UPSTREAM_HTTP_503` 被包装为 `HTTP_502`，恢复器正确识别，第二端生成合法计划；共2个POST，最终 `source=cloud-model:real-model`、`actual_backend=cloud-model`。此处source是被测适配器原有字符串，不表示fixture推理是实模型证据。双代理POST503时共2个POST、结果失败且无动作。未启动GPU模型。
3. 独立核对v2注册表中**12份旧源码/二进制SHA256全部不变**。检查时正式输出目录 `docs/experiments/20261111_race_v3` 尚不存在。
4. DLL完整性缺口已向主线报告并在注册前修正：当前配置明确pin同目录**30个DLL**，doctor要求DLL集合完全一致并逐份验SHA256；篡改/额外DLL测试通过。主要实现 `llama-server-impl.dll` 不再仅由9216字节入口EXE的哈希代替。

## 契约逐项审查

| 项目 | 结论与依据 |
|---|---|
| 注册先于推理 | `register()`以`exist_ok=False`建立新目录，保存注册表与相对路径源快照后，`main()`才进入`LocalRuntime`。不存在覆盖旧正式输出的路径。 |
| 健康检查后才失效 | 每配对块先设代理healthy，真实调用两端`health()`，由GET `/v1/models`构造相同观测，然后才设本地或双端POST503。GET继续转发，故障机制明确是库存检查不能保证下次POST成功。 |
| 相同输入与初态 | 两臂使用同command/context、同观测哈希；每臂重置相同JSON设备初态；C进程按臂和条件独立，条件内连续运行；臂先后交替。 |
| 恢复资格 | 限complex、原结果失败、没有动作/语义完成，且明确503/504或列出的传输失败。泛化502、4xx、非法JSON、安全拒绝与澄清不触发恢复。 |
| 嵌套错误兼容 | 生产DirectBackend将上游503包装成502；恢复器只接受明确`UPSTREAM_HTTP_503`等白名单嵌套码。真实HTTP fixture已验证。 |
| 备端即时检查 | 资格满足后重新调用备端health；要求`ready is True`且`reachable is True`；推理前再次检查发起预算与端点绑定。 |
| 不重复执行 | 恢复器仅返回计划；原网关在动作白名单校验后统一调用executor。已有单元集成测试覆盖成功恰好一次、拒绝/双失败零次，以及备端不安全动作被拒。正式矩阵另外记录`executor_calls`。 |
| 来源与失败透明 | 返回source包含实际槽位；response保留actual_backend、原错误和两次状态。独立`recovery_records`在旧网关回退规则覆盖response后仍由运行器收集。不能用`event.backend`把备端执行误称首选执行。 |
| 请求计数 | `count_calls()`只数POST，不把库存GET计入推理；区分代理POST、真正向模型转发的POST、代理直接503；warmup独立保存。 |
| 预算语义 | `launch_budget_ms`仅限制发起备端，不承诺同步调用总截止时间；实现和测试明确区分。10秒预算下，15秒主端超时可能耗尽预算，不能宣称所有超时均能恢复。 |
| 停止与保留 | 行即时追加，finally保存report，runtime管理子进程退出并保存manifest。正式目录存在时不得结果导向重跑或覆盖。 |

## 非阻塞限制与发布前复核项

- **观测为每配对块冻结快照**，第二臂可能较旧；代码明确`frozen_same_observation_per_paired_block_not_live_per_arm`。这是控制输入的一部分，不应描述为每臂都实时测得、或将`observed_age_ms=0`解释为绝对新鲜。
- 只有C01/C04两条既有冻结命令、三次重复；可验证故障处理机制，不能证明语言泛化、未知用户覆盖率或产品级可靠性。两条件之间固定顺序、模型缓存与共享GPU竞争亦限制时延外推。
- `executor_calls=1`含义是**单次网关请求中动作执行器调用一次**。它不证明跨请求幂等、不证明设备实际收到回执，也不保证一次调用内部多动作在物理设备上原子完成。
- 备端恢复选择是新增独立规则；原C只负责首选。不能称“原C策略独自实现完整重试/容灾”。
- `source_sha256_unchanged`当前为结果摘要字段，正式运行后须核对为true；还须逐行复算36行/18配对块、POST与usage、失败与executor次数、实际source、注册/启动/结束时间，验证源快照和manifest完整。
- 主要效果判据是冻结评分函数的非空动作与JSON模拟状态确认，不是用户成功、设备实测、真实WAN/6G或独立端云部署；本轮结果不能与旧600行、v2新增120行混算准确率。
- 此次独立fixture没有调用实际C二进制决策或设备执行器，相关逻辑依据代码和已有集成测试；正式矩阵才提供同机真实模型、实际C与JSON执行器共同运行的证据。

未新增国一获奖概率、评分预测、用户证言或性能数字。
