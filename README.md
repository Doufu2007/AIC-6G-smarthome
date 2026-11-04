# SenseHome AI+6G Smart Home Gateway

SenseHome 是面向智能家居控制场景的网络—算力感知 AI 网关原型。系统根据任务类型、WAN/LAN 状态和模型可用性，在本地规则、本地模型与云端模型之间选择处理路径，并通过动作白名单和安全降级保护设备执行。

## 项目边界

当前仓库包含：

- C99 链路感知卸载策略与策略 CLI；
- Python 网关、OpenAI-compatible 模型适配器和动作白名单；
- PC JSON 隔离设备状态与 Dashboard 测试闭环；
- AutoDL Qwen2.5-1.5B 真实模型预检及逐条评测证据；
- AI+6G 场景设计方案、答辩材料、演示视频和实验记录。

仓库中的 6G 相关结果是网络轨迹/软件台架和设计承载验证，不代表真实 6G 空口、物理设备、家庭试点或大模型部署已经完成。正式模型报告明确区分了真实推理、冻结后重新标注、模拟设备和设计目标。

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

## 公开仓库说明

本仓库不包含 API 密钥、SSH 凭据、模型权重、私有 token 或本地临时构建缓存。第三方组件和许可证见各目录下的 `LICENSE`、`NOTICE` 与 `THIRD_PARTY.md`。
