# AutoDL 真实模型验证状态（2026-11-03）

## 已完成

- 通过用户提供的 AutoDL SSH 入口登录实例并记录环境：Ubuntu 22.04.5、Python 3.12.3、RTX 4080 SUPER（32760 MiB）、驱动 595.71.05、Torch 2.14.0+cu130、vLLM 0.33.0。
- 安装记录保存在远端 `/root/autodl-tmp/sensehome-runtime.lock.txt` 和 `/root/autodl-tmp/sensehome-nvidia-smi.txt`。
- 适配器、设备动作白名单、评测脚本和原始标签草案已上传到 `/root/autodl-tmp/sensehome-adapter/`，上传时逐文件 SHA256 已对齐。
- 新 SSH 入口恢复后，已启动 Qwen2.5-1.5B-Instruct 并完成首轮 20 条真实模型草案预检；结果与失败日志已回收本地。

本次依赖安装发生在实例预置 Conda 的 Python 环境，未创建文档示例中的独立虚拟环境；正式复跑应以本次导出的运行时锁文件为准。

## 首轮真实推理结果与当前状态

- 原入口中断后，用户提供了新的 SSH 入口，连接已经恢复。没有在本项目文件中保存密码。
- 已把当前 Torch 安装 RECORD 之外的 `flex_attention.py`、`flex_decoding.py`、`mm_scaled_grouped.py` 备份隔离，解决重复模板注册；Torch inductor 和 vLLM 导入通过。首次 vLLM 启动因非登录 shell 的 PATH 缺少 Conda 中的 `ninja` 而失败；补充 `/root/miniconda3/bin` 后完成启动，两次日志均保留。
- 小模型固定 revision 为 `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`，权重为 3,087,467,144 B，完整 SHA256 为 `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee`。API、固定版本响应头、配置 Git blob 与完整权重哈希均已核对。先前恢复脚本误录为 65 位哈希，校验阻止了继续执行；已修正并增加格式回归。
- 首轮预检为 20 条、每条一次：HTTP 成功 19/20，与草案动作一致 12/20；20 次请求的中位数 271.207 ms、p95 527.368 ms。这里的时延是同一 AutoDL 实例回环 HTTP 请求墙钟，包含错误响应；不含首次启动和预热，不代表家庭网络、纯 GPU 内核时延或最终方案端到端时延。**标签未经人工复核，这些是开发预检结果，不能写成正式准确率。**
- 使用动作目标保护层重打包后，第二轮 20 条请求均来自相同模型 revision；HTTP 成功 18/20，草案动作一致 12/20，回环请求中位数 263.763 ms、p95 523.654 ms。两条 422 分别保留了安全拒绝（模型猜测未提及房间、阳台动作不在白名单）证据；它们不是上游不可达，也不能计作正确动作。该轮仍使用草案标签，不能作为正式准确率。
- 已观察到漏动作、猜测房间、亮度错误、额外窗帘位置等问题；不支持的阳台动作被白名单以 HTTP 422 拒绝。首轮预检按当时的严格条件标记为失败，不能用环境安装或接口可达代替验收通过。
- 原始证据位于 `docs/experiments/autodl_20261103_baseline/`，其中 `small_preflight_20261103T042705_821718Z/eval.draft.json` 保留全部逐条结果；同目录保存失败启动、运行参数、接口、版本、权重和代码哈希。原始下载快照保留于 `_work/autodl-evidence-snapshot-v1.zip`，SHA256 为 `937ceea49b3218b0ce1861a04faa82885570d69c25fc1b049fd23f1fcdd9e12a`。
- 第二轮证据位于 `docs/experiments/autodl_20261103_guard/`，原始远端快照 `_work/autodl-evidence-snapshot-v2.zip` 的 SHA256 为 `6adc40a5017158959afc4255332a0c4c54633fffec8b37d8b028200196ca1f80`。两轮目录均保留逐条 JSON、运行参数、模型身份和失败状态。

## 当前提示词与归一化版本

- 之后加入了 7 个中文 few-shot 约束，并增加透明的高置信度动作归一化：明确房间的“有点亮/有点暗”和温湿度只读请求可补出动作；模糊窗帘“调整一下”会移除模型猜测并要求澄清。归一化保留 `raw_model_actions` 与 `action_normalization`，不把修正伪装成模型原始输出。
- 第三轮证据位于 `docs/experiments/autodl_20261103_normalized_v2/`，代码哈希以其中 `source-sha256.json` 为准。20 条草案请求来自同一固定 revision：HTTP 成功 17/20，动作一致 17/20（85.0%），请求中位数 290.679 ms、p95 522.643 ms。三条仍为保护层 422：A01/A02 的房间未指明，A04 的阳台不在白名单；这三条应报告为安全拒绝，不能把 HTTP 失败直接写成语义错误。
- 原始轮次的 `eval.draft.json` 保留运行时草案字段；20 条标签随后已冻结，`eval.formal.json` 核验冻结副本与已捕获请求的命令、期望动作完全一致，并将报告标记为 `human_reviewed`、`formal_accuracy_evidence=true`。因此 17/20 可作为冻结标签下的正式重新标注结果；该报告明确 `inference_rerun_after_freeze=false`，不把重新标注写成新的推理运行。A01/A02/A04 的原始响应、A03 的归一化结果及模型身份均保存在逐条记录中。
- 大模型 Qwen2.5-7B-Instruct 已冻结 main revision `a09a35458c702b33eeacc393d103063234e8bc28`，immutable API 与 commit header 通过；因镜像 Range 两个块长时间无进展，为控制租用成本中断下载。已回收 `docs/experiments/autodl_20261103_large_weights/` 中的 API、块操作、断点和中断状态；没有完整大模型权重或推理结果。
- 原始 `eval_commands.jsonl` 作为草案历史保留；正式冻结副本为 `SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl`，冻结元数据为 `docs/deployment/20条动作标签冻结记录_20261103.json`。20 条标签由参赛者本人在本对话确认，期望动作无修改。
- 执行过程中曾错误地把自动复制的标签标为 `human_reviewed=true` 并上传为远端 `backends/eval_commands.reviewed.jsonl`，没有据此运行评测。本地错误副本已撤销，远端错误副本现已隔离并保存哈希；公开证据保留隔离记录，不把错误副本当作 reviewed 标签分发。
- 恢复包已修正 `backends/` 目录布局，设备白名单与适配器同级，上传包及各文件 SHA256 已核对。

## 继续执行

1. 动作房间必须在原始指令中显式出现、否定/条件变更保守保护、归一化审计和远端 4xx 拒答硬门已经落地并通过 82 项网关测试、20 项 Dashboard 测试；仍需在独立保留集上验证泛化，不能把这层保护称完整语义理解。
2. 顺序验证大模型，与小模型使用相同草案、解码参数和测量口径；大模型恢复包已开始下载，当前尚无大模型推理结果。两者均租用 GPU 时，不能宣称真实家庭网关与云端已同时部署。
3. 把真实推理接入现有 C 策略、Dashboard 白名单和隔离 PC JSON 状态，保存完整前后状态及路由事件。注入的网络输入必须明确标注，不能称真实断网。
4. 20 条标签已完成人工核对并冻结；后续针对已见样本的改进仍需使用独立保留集验证，不能直接据此宣称泛化能力。

