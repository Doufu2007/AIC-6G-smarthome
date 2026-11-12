# 本轮成本与部署补证

本目录仅新增离线复核与采证工具，没有修改旧源码、冻结标签或600条封存实验记录。工具仅依赖Python标准库。

## 已取得的用量证据

`usage_audit.py`对照封存索引核对四份report.json的SHA256，仅读取原始`backend_attempts[].upstream_exchanges[]`中的POST响应。不会把adapter返回的副本、GET模型清单或本地规则请求重复计数。HTTP失败和动作被保护拒绝的请求仍保留原始服务端用量。缺失/非法token保持null，不按0补齐。

| 独立矩阵 | 实际模型POST | 输入token | 输出token |
|---|---:|---:|---:|
| 人工冻结20条回归 | 84 | 49,572 | 4,860 |
| 程序派生12条开发回归 | 144 | 85,644 | 10,086 |
| 受控观测与迟滞 | 55 | 32,386 | 3,483 |
| 预注册程序组合留出 | 132 | 78,900 | 10,848 |
| 执行量合计 | 415 | 246,502 | 29,277 |

全部415次响应均有合法服务端usage；这里的token不是人工计量或提供商账单。合计仅描述执行量，不能跨矩阵汇总准确率、时延或收益；校准6次、历史AutoDL、其他会话及本轮后续新实验均未包含。各矩阵×模型分组的上游HTTP墙钟中位/p95见`results/usage_report.md`，它不是完整网关时延，也不是纯生成时延。

`results/usage_calls.jsonl`逐调用保存原始报告路径与完整SHA256、row_index、attempt_index、exchange_index、case/pair ID。可按这些位置回到原始响应核对；没有复制用户命令、Authorization或密钥。

## 当前实际成本保持未知

`ledger_current_unknown.json`只为上述四组实验建立有范围的成本账本。所有记录目的地均为已知本机llama.cpp服务，因此该范围内直接收费模型API分项记0，凭据类型明确为执行日志推导，**不是提供商账单**。历史AutoDL租机、下载、其他会话、电脑采购及运行开销不在这个零分项中。

硬件分摊、整机电量/电价、运维和增量网络费用没有实际凭据，均保留unknown；`results/ledger_current_result.json`的`actual_total`与`scenario_total`都为null。没有用GPU板卡功耗推算整机电费，没有把现有电脑的采购与维护自动记0。

`cost_ledger.py`要求实际条目同时填写币种、期间、来源、分摊比例、凭据类型、工作区内文件路径与SHA256。所有货币输入必须处于同一币种；不做隐含汇率转换。报价和假设可以生成单列的情景总额，不能生成实际总额。任一必要项未知，总额保持null。凭据哈希只证明文件未变化，不能自动证明发票真实、可报销或分摊公平，仍需本人核对。

真正补齐成本时，依次加入采购/原有电脑分摊凭据、包含待机/加载/失败的整机电量积分与实际电价、实际维护工时和增量网络费。若未来接收费模型，按实际账单计失败尝试、缓存和价格时段。填写后运行同一校验器，不能把报价或假设改名为实测。

## 部署检查的实际结果与范围

`deployment_current_local.json`保留旧封存实验的两个端口62352/42354。后续只读GET检查时，两端均已拒绝连接，原失败结果保存在`results/deployment_current_check.json`；没有重启或停止旧服务。主线随后启动的专用63452/63454属于本轮新实验，配置示例见`deployment_dedicated_local.json`，本目录没有再次发送推理或探测，不覆盖旧结果。

`deployment_check.py`默认只检查URL与逻辑槽位。`--probe-loopback`只允许字面量回环IP（如`127.0.0.1`或`::1`，不解析`localhost`等名称），发GET模型清单，不发推理请求；不允许把密钥塞在URL用户信息或query。探测使用`http.client`直连，不读取环境/系统HTTP代理，拒绝所有HTTP重定向；结果记录请求URL、HTTP状态及已连接对端。两回环端口只能说明两个逻辑服务，不会通过两主机部署验收。远端模板使用RFC5737文档地址，静态检查不会访问它们。

独立审查曾发现旧`urllib`实现会被环境代理截走且自动重定向。I1已修复；本机HTTP夹具复验见`results/deployment_probe_fix_verification.json`及`verification/deployment_probe_red_green.md`。原`results/deployment_current_check.json`保留旧实现的历史拒绝连接记录，未覆盖为新版本探测；它不单独证明请求确实抵达声明端点。修复测试只访问临时回环夹具，没有访问模型或远端主机。

从单机迁移到两台有授权的电脑时：

1. 在两台主机分别安装声明版本的运行时和权重，核对`docs/experiments/20261111_local_resources/provenance/`中的许可证及模型SHA256；端口可达不替代权重身份核验。
2. 将模型服务绑定到实际可达的受控地址，配置认证、防火墙及允许的调用方；密钥通过环境变量注入，不写进文件或截图。不能把仅本机监听的地址直接当作远端可用地址。
3. 将`deployment_two_host_template.json`的文档IP替换成真实端点，并记录两台主机匿名资产编号、运行时/权重哈希、部署角色和采证时间。静态检查得到不同hostname仅为配置事实，不能自动证明独立硬件。
4. 从实际网关主机验收模型清单、健康/队列接口和同一动作合同，再以同一命令集保存请求、状态与失败。队列、网络、设备回执分别测；不要把HTTP墙钟写成无线空口RTT。
5. 使用新的输出目录保存源快照与结果。只有实际部署、采证和业务验证完成后，才把部署状态改为已验收；本目录模板当前仍是`template_not_deployed`。

## 复算命令

```powershell
python -m pytest -q docs/value_v2/test_evidence_tools.py docs/value/test_cost_calculator.py
python docs/value_v2/usage_audit.py
python docs/value_v2/cost_ledger.py --ledger docs/value_v2/ledger_current_unknown.json --out docs/value_v2/results/ledger_current_result.json
python docs/value_v2/deployment_check.py --config docs/value_v2/deployment_dedicated_local.json --out docs/value_v2/results/deployment_dedicated_static_check.json
python docs/value_v2/deployment_check.py --config docs/value_v2/deployment_two_host_template.json --out docs/value_v2/results/deployment_template_check.json
```

已完成9项新增测试的RED→GREEN：真实POST去重、缺失/非法usage、外部目标分离、未知成本传播、实际凭据哈希和分摊、报价/币种隔离、越界凭据路径、同机槽位识别、URL凭据拒绝。测试使用临时文件和虚构响应，不计真实模型或真实账单证据。

I1修复另含环境代理和302重定向两项真实HTTP夹具测试的RED→GREEN，以及字面量回环IP限制测试。当前完整命令为19项、12个子测试通过；原9项初版RED记录口径不变。
