# 当前运行依赖与复用范围

记录日期：2026-11-03。范围是本轮 `net_offload` 软件台架、C 策略进程、PC Dashboard 及已交付但尚未部署到 GPU 的模型适配器。运行版本取自 `net_offload/build_manifest.json`；manifest 另保存源码、二进制 SHA256 与 Git 工作区状态。生成提交文档、图像和 PDF 的编辑工具不属于模型后端或设备运行时。

## 本轮已运行

| 组件 | 实测版本／来源 | 本轮用途与范围 |
|---|---|---|
| CPython | 3.13.7 | 台架、网关、HTTP 后端、Dashboard、证据校验及 `unittest`。这些运行模块使用 Python 标准库；本轮没有为其引入 PyPI 运行依赖。Python 许可随 CPython 发行版提供。 |
| GCC / MinGW-W64 | `gcc.exe (MinGW-W64 x86_64-msvcrt-posix-seh, built by Brecht Sanders, r6) 15.2.0` | 在 Windows 编译 C99 策略、CLI 与测试。编译器是构建工具；本项目不将 GCC 源码或安装包作为自有代码分发。GCC、运行时及异常条款以实际工具链发行版附带许可为准。 |
| Windows / PowerShell | manifest 记录 `Microsoft Windows NT 10.0.26200.0`；未冻结 PowerShell 精确版本 | 执行 `build.ps1`、`run_evidence.ps1`。未把本机操作系统或 shell 作为作品自有代码。 |
| Python 标准库 | 随 CPython 3.13.7 | 包括 HTTP/JSON、线程、子进程、socket、hashlib、文件与测试工具。网络台架为 TCP 软件回放，设备状态为 PC JSON 模拟。 |

C CLI 和 Dashboard 在 PC 上运行不能作为 openvela/NuttX 烧录、QEMU 整系统启动、物理传感器或真实 6G 空口验证的证据。模型桩 `backends/stub_backend.py` 的注入耗时属于台架假设，不能作为模型权重、GPU、准确率或商用服务性能记录。

## 复用工程与本轮新增代码

| 资产 | 来源／版本现状 | 当前关系 |
|---|---|---|
| `openvela_smart_home` 及其 Tool／Skill、Dashboard 基础 | 本地已有工程；原 `NOTICE` 指向 `https://github.com/YashTech/vela_agent`；该上游精确 commit 未在本轮确认 | 在既有项目上新增网关适配、动作白名单、健康监控及测试。原工程署名、LICENSE、NOTICE、历史依赖表和两份补丁保持原文。 |
| `SenseHome-6G/net_offload` C/Python 实现与测试 | 本轮工作区代码；具体文件 hash 见构建 manifest | 新增 C 路由策略、逐请求网络回放、后端接口、证据与模型验收工具。它们不把 Python 依赖表中的外部工程源码当作新增原创模块。 |
| 当前合规基线五文件 | `compliance/README.md` 中有原文 hash | 仅证明已保留这些基线文件；不以此声称所有上游版本和权属均已完成独立验证。 |

现有自有代码许可声明使用 Apache-2.0，文本见 `LICENSE`。第三方组件继续适用其各自许可；构建器或服务的许可不能由本项目 Apache-2.0 声明覆盖。

## 可选真实模型路径：本轮已运行小模型，7B 仅冻结版本并中断恢复

| 组件／资产 | 当前状态 | 真正使用时需记录 |
|---|---|---|
| AutoDL GPU 实例 | 已运行过 RTX 4080 SUPER 实例；驱动 595.71.05，显存 32760 MiB，Python 3.12.3，Torch 2.14.0+cu130 | 最新证据见 `docs/experiments/autodl_20261103_normalized_v2/`；凭据不入库、不随包分发。 |
| vLLM | 0.33.0，已在该实例导入并启动 OpenAI-compatible 服务 | 最新启动参数、日志和版本见 `docs/experiments/autodl_20261103_normalized_v2/`；仅记录实际运行版本。 |
| Qwen2.5-1.5B-Instruct | revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`，权重 3,087,467,144 B，SHA256 `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee`；已完成三轮真实模型预检 | 最新一轮 HTTP 成功 17/20、动作一致 17/20，回环请求中位数 290.679 ms、p95 522.643 ms。20 条标签已冻结，正式报告为冻结后对已捕获响应的重新标注；3/3 预期拒答另行记录。 |
| Qwen2.5-7B-Instruct | main revision `a09a35458c702b33eeacc393d103063234e8bc28` 已冻结；分片恢复因 Range 长时间无进展而中断 | 无完整权重、无推理结果；恢复断点和 manifest 见 `docs/experiments/autodl_20261103_large_weights/`。 |
| OpenAI-compatible 上游 HTTP 服务 | `backends/openai_backend.py` 已通过本地假上游测试 | 实际服务、配置模型与返回模型 ID、服务条款；上游需密钥时只用环境变量。 |
| `openai_backend.py` 适配器 | 已交付标准库实现；已用 AutoDL 1.5B 真实模型完成三轮预检，并加入透明动作归一化 | `source=real-model` 结果只证明该次固定环境调用；正式报告在冻结标签下对已捕获响应重新标注为 17/20（85.0%），且明确没有冻结后重新推理。 |

模型权重和外部服务密钥不随本包分发。`AutoDL_真实模型验证.md` 是部署与验收路径，未执行的命令不计为完成；未来新增模型或服务后应按实际资产补披露，保留本页所列历史状态。
