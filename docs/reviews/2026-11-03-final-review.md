# 2026-11-03 最终独立审查

审查结论：当前代码可形成可审阅的 PC 软件闭环，未发现需要阻塞本地审阅快照的未修代码错误。该结论不表示达到国赛一等奖水平，也不将假上游、模型桩、PC JSON 设备或 TCP 软件回放当作真实模型、物理设备或 6G 空口证据。

本次核查覆盖 C 策略与 CLI、Python 网关、Dashboard 动作执行、HTTP 模型适配器、未配置后端和超时回退、证据数字复算、构建与证据入口、依赖披露。未租用 GPU，未联网调用真实模型，未修改作品方案 MD 或实验结果 JSON。

## 阻塞项

没有本次复核仍可复现的未修阻塞代码问题。以下原有问题已处理并有回归证据：

| 问题 | 最终行为与证据 |
|---|---|
| HTTP 健康结果覆盖传输可达性，或健康与推理共用短超时 | `HttpJsonBackend` 将传输字段设为权威结果，独立配置健康／推理超时。`test_backend_health.py` 4 项通过，包含真实本地 HTTP 超时测试。 |
| 返回远端动作但未实际执行，或执行失败仍称成功 | 网关调用 Dashboard 权威白名单执行器；执行与持久化失败进入显式回退。`test_gateway_action_execution.py` 4 项及 Dashboard `test_remote_execution.py` 相关闭环通过。 |
| 真实适配器的来源被网关覆盖 | `source=real-model`、模型 ID、模型 HTTP 墙钟及环境声明来源保留到 Dashboard；假上游测试仅验证协议，不形成真实模型成绩。 |
| wrapper 只有部分健康字段，无法驱动 C 算力策略 | C `compute_partial` 使用 `-1` 表示未知；Python 使用 `null` 并记录 known／unknown 字段及来源。ready=false 和已观测 inflight 超阈可独立降级，未知显存不冒充 0 或健康值。wrapper 的无排队不被当作上游模型队列。真实 C、Python、Dashboard 回归通过。 |
| 复合、条件或否定指令落到单动作关键词规则，局部执行却称完成 | `needs_plan()` 统一保守分流；本地回退对这些指令不执行部分动作，返回未完成。Dashboard 复合指令、安全拒绝及真实 C＋HTTP 执行回归通过。它仍是路由保护规则，不是完整自然语言理解器。 |
| 数字门只检查恢复计数类型，无法发现被改成另一个非负整数 | 本审查先加入篡改回归，修前 4 个计数和 recovered 标志共 5 处预期失败；再让 `_work/verify_numbers.py` 从 `transition_trace` 调用 `recovery_stats()` 复算并比较完整对象。修后 3 项测试及权威结果复算通过。 |

## 重要项与交付约束

1. **n=20 结果与当前源码快照须分清。** `_work/evidence_final/` 的原始完整运行未保存当次完整 build manifest；派生 JSON 保存了原件 SHA256 和探测聚合修正来源。修正元数据中的 benchmark source hash 是修正时记录，不能追认为完整跑数发生时全部源码版本。`_work/runner_smoke_final/build_manifest.json` 只关联后来的 n=1、跳过恢复过程的入口 smoke。后生成当前源码 snapshot 可证明交付版本，不能回填历史；打包说明与 provenance 应保留这一区别。

2. **执行成功与命令语义正确性分开。** 网关 `semantic_complete` 对模型计划可表示合法动作已执行，不能独立识别模型选错房间等语义错误。真实模型准确率使用 `eval_model.py` 的冻结、人工复核参考动作 `action_correct` 及逐条记录；该脚本不信模型自报完成状态。20 条标签已冻结在 `eval_commands.reviewed.jsonl`，冻结记录包含审核人、日期和 SHA256；当前正式报告是对已捕获真实模型响应的重新标注，明确记录没有冻结后重新推理。

3. **旧差距审计需要按日期理解。** 根目录《提交材料差距审计.md》在本次读取时仍有“check_draft 硬编码旧工作区”“失败字段尚未修”的历史描述，已与当前实现不一致。它说缺少最终答辩 PPT/PDF，不应与现有作品方案 DOCX/PDF 混淆。交付时应更新或标记历史状态，避免把已关闭代码问题继续当作当前事实。

4. **依赖表已按当前范围补充。** 本审查新增 `compliance/CURRENT_DEPENDENCIES.md`，记录实测 CPython 3.13.7、GCC 15.2.0、标准库运行和未执行的 AutoDL 路径；README 明确旧五文件是基线快照，撤去“完整凭据”“本轮只改两处”和未验证的补丁可应用保证。原 LICENSE、NOTICE、THIRD_PARTY 和两份补丁 SHA256 全部保持不变，未知上游 commit／模型许可未填确定值。

## 可后续处理的边界

- `save_state()` 已保证原子替换，远端执行器也有状态锁；巡检线程、旧本地规则和 `/api/light` 的全部读改写尚未共用同一事务锁。并发写可能丢更新，当前单机模拟动作证据不能扩大为多写者事务或真实设备并发保证。
- 旧“离家模式”会修改模拟安防布防状态；本轮只读安防查询的 80 次证据不覆盖这套旧联动的授权、安全或准确性。真实门禁、燃气和安防设备仍需单独接口、授权和联锁验收。
- HTTP 客户端 timeout 是底层 socket 等待预算，尚未实现对恶意慢速分块响应的严格总墙钟截止；受控的回环 wrapper 路径不因此等同于开放公网服务保障。
- Dashboard 默认通过回环 SSH 隧道连接 wrapper，文档明确 wrapper token 保持未设置；`eval_model.py` 支持可选 token，`HttpJsonBackend` 暂无 token 配置。这是已说明的接入范围，当前默认路径不因此失败。
- wrapper 可提供模型 inventory 就绪与自身 inflight，未提供真实 GPU 空闲显存或上游排队遥测。未知字段保留未知；后续补遥测须注明来源。
- GPU 模型部署、人工标签复核、真实小／大模型比较、最终 3—5 分钟视频、答辩文件与官方编号／网盘上传仍应按真实完成情况验收，不由本轮代码测试替代。

## 本审查实际执行的验证

| 检查 | 结果 |
|---|---|
| `test_backend_health.py` | 4 项通过 |
| `test_openai_backend.py` | 5 项通过；全部使用本地假上游 |
| `test_eval_model.py` | 3 项通过 |
| `test_gateway_action_execution.py` | 4 项通过 |
| `test_policy_cli_compute.py` | 6 项通过；使用真实 C CLI，包含日志与实际算力决策一致性 |
| `test_gateway_resilience.py` | 13 项通过 |
| Dashboard `test_gateway_bridge.py` | 5 项通过 |
| Dashboard `test_remote_execution.py` | 5 项通过；包含真实 C＋HTTP＋PC 模拟状态闭环 |
| `_work/test_verify_numbers.py` | 修前按预期失败，修后 3 项通过 |
| `_work/verify_numbers.py` | schema、raw summary、作品方案 8 行复杂任务表、实际探测中位数及恢复轨迹重算通过 |
| `_work/check_draft.py` | 作品简介 468 字符、场景摘要 441 字符、标题 18 字符；脱敏串、符号及奖项一致性门通过 |
| 合规五基线文件 | 字节数与 README 所列 SHA256 逐项一致 |

当前恢复轨迹复算为 72 次探测、首次离云索引 21、首次离云到回云跨度 26、末次断链后到回云跨度 15。探测聚合只使用各档实际探测的 20 条记录，P1—P4 分别为 62.7、425.5、22.0、29.5 ms；未把 40 条未探测本地快路径的零值纳入中位数。实验原件和派生结果均保留，本次没有重新包装这些数字为真实 GPU 或设备性能。
