# 需求、差异化与部署价值证据包

版本：2026-11-11。身份：面向实际需求的场景设计；当前新实验使用本机。没有开展用户访谈、家庭试点、客户确认或商业成交。

| 文件 | 用途 | 证据属性 |
|---|---|---|
| `demand_and_benchmark.md` | 需求逻辑、利益相关者、Home Assistant/固定后端/模型路由对标 | 官方背景 + 可检验的场景假设 |
| `public_sources.json` | 9 项官方来源、短摘录、访问日期、快照 SHA256 与支持范围 | 2026-11-11 公开网页快照 |
| `ai6g_mapping.md` | ITU IMT-2030 / ETSI MEC 与当前字段逐项对应 | 设计映射；没有 6G 空口实测 |
| `deployment_and_replication.md` | 无硬件本机复现、BOM、单点到集成商的复制步骤 | 现有运行入口 + 待验证推广路径 |
| `five_minute_trial.md`、`trial_sessions.csv` | 可直接执行的 5 分钟试用脚本和空白采集表 | 待采集；有效样本数为 0 |
| `cost_model.md`、`cost_calculator.py` | 公式、字段边界、未知成本传播、每千请求分项 | 标准库情景计算器；没有实测完整总成本 |
| `cost_current_unknown.json` | 本轮本机运行的未知项清单及可计算边界 | 当前无云账单；功耗/电价/采购价未知 |
| `cost_scenario_*.json`、`cost_results/` | 同负载、同网关摊销下的三个可修改演示情景 | 假设 + 公开云报价；不是用户需求或账单 |
| `host_resources_summary.json` | 对本机 30 个样本的只读统计和原件哈希 | 实际短时资源采样；不是整机能耗 |
| `test_cost_calculator.py`、`work_log.md` | 公式/边界验证与工作记录 | 执行证据 |

可用于主材料的主张：已有开源智能家居能够本地理解、连接本地/云模型、限定实体并优先处理本地意图。本作品的验证重点是家庭两段链路与两端推理资源共同变化时，如何让路由、动作校验、回退及能力损失可以解释和复核。尚无与 Home Assistant 的同条件优劣实验。

当前不能由材料补齐的事实：真实住户/集成商需求、试用反馈、支付意愿、实际故障频率、整机电量与电价、采购/维护账单、真实物理设备和运营商接口。它们均保留为外部依赖，不用情景计算替代。

```powershell
python -m unittest discover -s docs/value -p test_cost_calculator.py -v
python docs/value/cost_calculator.py --config docs/value/cost_current_unknown.json --json
python docs/value/cost_calculator.py --config docs/value/cost_scenario_hybrid.json --json
```

成本工具运行不联网、不调用云 API、不操作设备。公开来源通过 firecrawl keyless scrape 获取；完整抓取缓存在被忽略的 `.firecrawl/`，交付保留必要短摘录及其适用边界，未重新分发第三方整站内容。
