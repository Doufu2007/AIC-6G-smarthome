# SenseHome AI+6G Smart Home Gateway

当前源码修复（2026-11-12 V6）：回执恢复、巡检观测可信度、HTTP 输入期限与显式命令路径的修复说明及回归入口见 [docs/intrinsic_v6/README.md](docs/intrinsic_v6/README.md)。下列 v4/v3 数字与材料代表其冻结版本；不因当前代码变化而重算。两份大型历史原始日志以无损 `.jsonl.gz` 公开，运行旧证据校验前按 V6 说明还原。

当前入口（2026-11-11 v4）：`dist/final/20261111_v4/` 为统一方案、答辩、佐证和当前软件证据回放。运行 `python -X utf8 docs/demo_v4/live_v4.py` 默认接入既有真实模型、正式持久回执与 MQTT 软件实体；真人采集入口为 `python -X utf8 docs/trial_v4/run_trial.py --record-kind participant`，每人单独重启。操作见 `docs/demo_v4/README.md`、`docs/trial_v4/README.md`，六项完成边界见 `docs/execution/2026-11-11-evidence-v4.md`。

v4 三臂共用恢复器，4条既有人审复杂命令×6条件×3臂共72主行，另24空计划结构检查；每臂完成20/24，按预登记规则暂荐本地优先，不宣称联合策略有显著优势。实际 MQTT 软件平台六类故障合同通过，新目录120秒规则负载120条符合预期。当前视频250.376秒，为真实运行记录的编辑回放、非连续现场录屏。真人反馈0、人工独立部署未完成、实际成本null、永久链接待补，成功模型请求250ms内0/60。两模型同机共享GPU，无物理设备、真实WAN或6G空口验证；v1—v3和旧失败分别保留。

交付分为匿名材料审阅包和内部复现审阅包；后者保留原始证据中的本机路径，不可直接公开。模型权重、推理DLL和真人私有记录不入包。

历史 v3：`dist/final/20261111_v3/` 为差距与整改报告、代码和证据；`docs/competition_v3/整改与冲刺路线.md`逐项列出官方六维、获奖案例和仍需真实外部事实的验收。

v3移除工作台断连伪成功、补一次备用规划/启动完整清单/独立设备回执合同。新增36行预注册机制回归与旧600/120行分别统计：本地POST503时原网关0/6、新恢复6/6；成功请求在250ms内0/18。所有设备仍为PC软件仿真，外部反馈0、实际总成本未知。v1/v2材料与旧失败保留，未因新增功能改写历史视频和效果。

SenseHome 是面向智能家居控制场景的网络—算力感知 AI 网关原型。融合后的 C 策略按任务类型、WAN/LAN 观测、本地与云端模型健康度及已知资源成本选择规则、本地模型或云端模型；动作白名单和安全降级约束设备执行。

## 2026-11-11 本机补证

本轮本地审阅件位于 `dist/final/20261111/`。四个最终矩阵分别保存人工冻结回归240行、程序开发回归144行、受控观测72行、预注册程序组合留出144行，共600条网关记录、415次实际模型推理调用；不同矩阵不合并计算准确率或时延。留出非空任务联合21/24、第二同机4B模型24/24，失败保留，未据此再调参。完整状态见 `docs/execution/2026-11-11-evidence-completion.md`，最终文件哈希与离线核验分别见交付目录清单和 `docs/experiments/20261111_fused_real/offline_sealed_audit.json`。

按参赛者要求，本轮复用本机 Qwen2.5-1.5B 与 Qwen3-4B 量化模型，两个服务共享同一 RTX 4050 Laptop GPU；策略中的 cloud 仅表示第二逻辑槽位，不是异地云部署。最新20条人工冻结命令 × 四策略 × 三次重复共240条记录位于 `docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/`，保存逐请求模型文本、保护前动作、执行前后模拟状态和源码快照。正常健康条件下未证明联合策略优于固定后端，不能将 r3 合成增益移植为本机实测收益。

本轮真实联测发现并修复了缺少房间默认控制、窗帘语序/百分比漏解析、旧关键词对否定/教程文本误执行，以及模型对泛称设备和不支持设备复合指令的部分执行。动作规划、保护拒绝、空结构、模拟执行和失败分别统计；没有把接口200或初始状态相符当作任务完成。程序派生挑战集经问题修复后按开发回归集使用，不称人工独立保留集。

公开需求背景、竞品对标、ITU/ETSI接口映射与参数化成本见 `docs/value/`；实际资源采样见 `docs/experiments/20261111_local_resources/`。真实用户反馈有效样本仍为0，采购/墙上电量/电价/维护成本尚无凭据。新版正式格式文件位于 `dist/final/20261111/`，总体交付与未完成外部依赖见 `docs/execution/2026-11-11-evidence-completion.md`。

## 项目边界

当前仓库包含：

- C99 双端网络—算力策略与策略 CLI（已完成全量构建、测试和 r3 合成实验审计）；
- Python 网关、OpenAI-compatible 模型适配器和动作白名单；
- PC JSON 隔离设备状态与 Dashboard 测试闭环；
- AutoDL Qwen2.5-1.5B 真实模型预检及逐条评测证据；
- AI+6G 场景设计方案、答辩材料、演示视频和实验记录。

`SenseHome-6G/net_offload/results/results.json` 中的 n=20 性能数据仍是旧网络策略历史基线。融合版结果、构建日志和 SHA256 清单位于 `SenseHome-6G/evidence/network_compute/20261111_fused/`；其 replay 使用合成轨迹与固定规划合同，HTTP/并发实验使用本机桩服务，不能解释为模型准确率或真实 GPU、6G 空口、物理设备及家庭试点效果。AutoDL 模型预检是另一条独立证据，不能替代上述策略实验，也不能与桩合同混为一谈。

## 目录

```text
SenseHome-6G/          AI+6G 场景方案、C 策略、网关、评测与证据
openvela_smart_home/   智能家居动作白名单、Dashboard 和 PC 模拟设备
docs/                  部署、实验、视频、审阅和交付记录
dist/final/             最终答辩 PDF/PPT 与佐证 PDF
```

## 本地验证

```powershell
python -m pytest -q SenseHome-6G/net_offload/tests
python -m pytest -q openvela_smart_home/dashboard/tests
python -m pytest -q docs/deployment
```

C 策略可在 `SenseHome-6G/net_offload/offload_policy.c` 和 `offload_policy.h` 中查看。评测默认使用已经人工核对并冻结的 `backends/eval_commands.reviewed.jsonl`；草案标签只能显式用于预检。

新增双端策略、当前验证结果和复现实验流程见 `SenseHome-6G/net_offload/README.md`。归档的 r3 运行使用当前 C 源码构建并通过完整测试与审计；旧二进制和 n=20 结果仍只代表历史网络策略基线。

## 公开仓库说明

本仓库不包含 API 密钥、SSH 凭据、模型权重、私有 token 或本地临时构建缓存。第三方组件和许可证见各目录下的 `LICENSE`、`NOTICE` 与 `THIRD_PARTY.md`。
