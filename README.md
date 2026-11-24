# SenseHome 智算家居协同引擎

面向家居智能体集成的可审计执行层。模型生成设备操作提案，本地运行时检查目录、能力与状态，用户确认后才派发动作，并通过平台状态读取和持久回执记录结果。

## 当前功能

- Home Assistant `light`/`cover` 发现、能力校验、提案与明确确认。
- SQLite 持久化执行记录、重复请求处理、超时后状态核验和未知结果恢复。
- 可选的华为云 MaaS OpenAI 兼容模型提案配置。模型输出仍经过本地护栏，不直接控制设备。
- openvela 家居 Agent、命令白名单、QEMU 运行材料及原始许可证。

## 本地运行 v11

需要 Python 3.10+。v11 核心链路使用 Python 标准库。

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

2026-11-24 MaaS 接入后，v11 测试套件第二轮 211 项通过。本地 HTTP 契约测试不代表华为 MaaS 真实模型调用，当前真实调用仍待在运行环境配置有效 API Key 后验证。相关范围与失败记录见 `docs/MaaS接入记录.md`。

## 目录

```text
src/SenseHome-6G/       当前 Python Runtime、v11 和校准/模型接口
src/openvela_smart_home/ openvela 家居 Agent、设备工具和许可证
docs/                    MaaS 接入说明与版本记录
rules/                   华为 ICT 官方赛制来源链接
参赛项目初版.md           华为 ICT 创新赛方案
```

## 来源与许可证

openvela 组件保留其 `LICENSE`、`NOTICE` 和 `docs/THIRD_PARTY.md`。SenseHome Runtime 中的规则解析、提案确认和持久执行是本仓库当前维护的应用层代码。复用基础设施、上游来源与历史实验的范围见 `docs/来源与版本清单.md`。

华为 ICT 赛制以官方手册和报名页面为准，链接见 `rules/README.md`。AIC 冻结提交包和旧赛事材料作为本地历史资料保存，没有随本仓库当前版本发布。
