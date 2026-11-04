# 开源合规材料（Open-Source Compliance Bundle）

本目录保留复用工程的五份合规基线快照，并补充本轮 PC 台架、网关和真实模型适配器的实际依赖说明。它是提交材料的组成部分；不能仅凭旧依赖表推断当前系统已完成全部许可核查或硬件部署。

当前版本先阅读 [`CURRENT_DEPENDENCIES.md`](CURRENT_DEPENDENCIES.md)。其中区分已运行的本地软件、复用工程、已完成的 AutoDL 1.5B 草案预检和仅冻结版本但未完成恢复的 7B 模型；未知的上游 commit、模型 revision 和许可不填确定值。

以下五份文件取自基线工程 `开源大赛/大一/openvela_smart_home`，本轮保留原文，不将历史内容改写为当前实现。

---

## 一、清单与校验值

| 文件 | 字节 | SHA-256 |
|---|---|---|
| `LICENSE` | 11,366 | `762abd64330976eb9b56371bbf0eeaa4d2bccf1f37a037e596a44ee8bf7b4df0` |
| `NOTICE` | 398 | `7ce00985f5eb9731ae24407e30d74e56accc94acb5469b54b80e6e8f9ae4b072` |
| `docs/THIRD_PARTY.md` | 2,244 | `6e9bb01db314e4d7238369b07fdeeabbff716414859045ddea6bff82297befc6` |
| `patches/agent_init.patch` | 2,721 | `1c473c297c307e341bd403fada33dad8d30abf301a1fb12167583b7abe5d1e6b` |
| `patches/tool_registration.patch` | 2,413 | `7f95dfc8b138ed105abcddd03f84e099939519754a3ae115fe82ce41acd44cf4` |

复算：

```bash
sha256sum LICENSE NOTICE docs/THIRD_PARTY.md patches/*.patch
```

---

## 二、基线许可声明

基线工程的自有代码（Skill、Tool、构建与测试脚本）声明使用 Apache-2.0，文本见 `LICENSE`。第三方组件继续适用各自许可；不能仅凭本项目的许可声明推断全部依赖的兼容性已得到验证。

`NOTICE` 逐字保留了对上游 openvela 工程的署名与出处：
本作品基于 `https://github.com/YashTech/vela_agent` 开发。

`docs/THIRD_PARTY.md` 是原工程当时的第三方依赖表，覆盖核心框架（openvela / NuttX）、构建工具链、可选 LLM 后端、系统工具与测试框架。其版本范围和可选服务记录保留历史意义，不能代替本轮 Windows PC 构建环境记录，也不能证明这些组件均在本轮运行过。

⚠️ 其中**LLM 后端三项（DeepSeek API / 通义千问 API / Ollama）为可选后端**，
使用者须自行申请密钥并遵守各平台服务条款；本作品**不分发**其任何源码或二进制。

---

## 三、基线对上游的两份补丁

以下两份补丁描述基线工程当时的 Agent 入口与 Tool 注册改动，保留原文用于追溯：

| 补丁 | 触及的上游文件 | 增/删行 | 作用 |
|---|---|---|---|
| `patches/agent_init.patch` | `packages_ai_agent/src/agent_main.c` | +32 / −5 | Agent 入口：引入智能家居 Tool 头文件、改写启动横幅 |
| `patches/tool_registration.patch` | `packages_ai_agent/src/tool_manager.c` | +39 / −0 | Tool 注册表：登记 `light_control` 与 `read_temperature` 两个 Tool |

这张表不能解读为本轮整个作品只改了两个文件。当前新增 C 策略、Python 台架、网关、Dashboard 白名单、模型适配器与测试，应结合源码和构建 manifest 审阅；也不能凭这两份补丁证明当前整个目录与某个未知上游 commit 的差异范围。

补丁头部带中文标注，作为历史快照保留。是否可应用须对实际目标上游版本执行 `git apply --check`；本轮没有把旧补丁应用结果计为验证证据。

---

## 四、这两个目录该放在哪

| 用途 | 放法 |
|---|---|
| **佐证材料 PDF**（附件1 二.6，合并为一个 PDF） | 本目录五份文件的**文本内容**按上表顺序串成 PDF 的一节 |
| **代码包（百度网盘，附件1 二.7）** | 这五份文件在**代码包根目录按原相对路径**放置：`LICENSE`、`NOTICE`、`docs/THIRD_PARTY.md`、`patches/*.patch` —— 与作品方案「原创性与合规声明」一节里写的路径**逐字一致** |

当前版本同时保留 `CURRENT_DEPENDENCIES.md`，在佐证材料中明确“基线快照”与“当前运行依赖”的范围。后续真正使用模型权重、GPU 或外部服务时，按实际版本、来源、许可和运行记录补充披露，未运行资产不写成已验证部署。
