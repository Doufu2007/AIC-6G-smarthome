# SenseHome 智算家居协同引擎

面向家居智能体集成的可审计执行层。模型生成设备操作提案，本地运行时检查目录、能力与状态，用户确认后才派发动作，并通过平台状态读取和持久回执记录结果。

## 当前功能

- 中文可视化控制台，支持可溯源动态检查单（拟执行 / 自动跳过 / 规则保留 / 只读观测）、多智能体轨迹追踪、人工显式确认、独立状态读回、确认前状态漂移演练、定时灯光、照护应急协同与 MCP 规则审计。
- 标准最小权限 MCP 服务（`mcp_server.py`，同时支持 Streamable HTTP `POST /mcp` 与 `stdio`），仅暴露只读查询与非执行提案校验工具（0 执行工具）。
- Home Assistant 灯、窗帘、开关和适用空调的能力校验与确认执行，门磁、温湿度和摄像头在线状态可查询。
- 华为云 AgentArts 多智能体客户端、知识库语料包（`agentarts_assets/`）、50 条冻结评测基准与 CSI、WM、RSI 离线试验。
- SQLite 持久化执行记录、重复请求幂等处理、含跳过设备的确认前状态漂移拦截、超时后状态核验和未知结果恢复。
- 可选的华为云 MaaS OpenAI 兼容模型提案配置。模型输出仍经过本地护栏，不直接控制设备。
- openvela 家居 Agent、命令白名单、QEMU 运行材料及原始许可证。

## 本地运行 v11

需要 Python 3.10+。v11 核心链路使用 Python 标准库。

```powershell
cd src/SenseHome-6G/v11
python -X utf8 -B web_demo.py
```

从终端读取本地操作员令牌，进入 [SenseHome 控制台](http://127.0.0.1:8765/)。详细的界面、网关和工作流联调步骤见 [实施手册](docs/AgentArts与全屋照护实施手册.md)。

命令行也可以从仓库根目录独立试用。

```powershell
cd src/SenseHome-6G/v11
python -X utf8 -B cli.py --home demo init
python -X utf8 -B cli.py --home demo propose "打开书房阅读灯"
```

配好 MaaS API Key 后，可参考 `src/SenseHome-6G/v11/examples/huawei-maas.routes.example.json` 与 `docs/MaaS接入记录.md` 使用华为云模型生成提案。API Key 通过环境变量 `MAAS_API_KEY` 提供。

读完提案后，再用响应中的 `proposal_id` 和 `digest` 显式确认：

```powershell
python -X utf8 -B cli.py --home demo confirm --proposal-id '替换为提案ID' --digest '替换为提案摘要' --request-id demo-001 --confirm
```

默认软件状态演示不会操作真实设备。接入个人 Home Assistant 前，按 v11 README 配置自己的实例与凭据。

## 测试

```powershell
cd src/SenseHome-6G/v11
python -X utf8 -B -m unittest discover -s tests -v
```

本次发布通过 v11 全部单元测试及 `agentarts_assets/run_eval.py` 50 条冻结基准评测。AgentArts 工作流、标准 MCP 服务和 MaaS 接入已通过本地接口与端到端冒烟测试；华为云控制台编排与部署指引见 [AgentArts 云边协同部署手册](agentarts_assets/workflow_architecture.md)。[AgentArts 实施手册](docs/AgentArts与全屋照护实施手册.md) · [MaaS 接入记录](docs/MaaS接入记录.md) · [CSI、WM 和 RSI 离线试验](src/SenseHome-6G/v11/SENSING_TRIAL.md)

## 目录

```text
src/SenseHome-6G/       当前 Python Runtime、v11 边缘护栏、动态检查单与 MCP 服务
agentarts_assets/       华为云 AgentArts 知识库语料、多 Agent 提示词与 50 条冻结评测集
src/openvela_smart_home/ openvela 家居 Agent、设备工具和许可证
docs/                    参赛方案、接入说明和来源版本边界
rules/                   华为 ICT 官方赛制来源链接
参赛项目初版.md           华为 ICT 创新赛方案
```

## 来源与许可证

openvela 组件保留其 `LICENSE`、`NOTICE` 和 `docs/THIRD_PARTY.md`。SenseHome Runtime 中的规则解析、提案确认和持久执行是本仓库当前维护的应用层代码。复用基础设施、上游来源与历史实验的范围见 `docs/来源与版本清单.md`。

华为 ICT 赛制以官方手册和报名页面为准，链接见 `rules/README.md`。AIC 冻结提交包和旧赛事材料作为本地历史资料保存，没有随本仓库当前版本发布。
