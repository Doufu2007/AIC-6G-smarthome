# SenseHome 交付检查点 · 2026-11-03

## 15:10 更新：AutoDL 真实小模型预检已回收

通过已恢复的 AutoDL 实例完成一轮真实 GPU 小模型预检，原始文件见 [`docs/experiments/autodl_20261103_real_small_live/`](../experiments/autodl_20261103_real_small_live/)。本轮使用 Qwen2.5-1.5B-Instruct（served name `sensehome-small`，revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`）、vLLM 0.33.0 和 RTX 4080 SUPER，20 条固定草案标签的 HTTP 成功为 19/20，草案动作一致为 12/20，请求中位数 264.0745 ms、p95 620.525 ms。

该结果已证明“真实 GPU 模型 → OpenAI-compatible wrapper → 结构化动作白名单”链路能够运行；本轮也没有执行物理设备动作，不能据此宣称家庭试点或 6G 空口验证。20 条标签已由参赛者确认并冻结，正式冻结记录见 `docs/deployment/20条动作标签冻结记录_20261103.json`。

本文件是当前交付状态的公开索引，替代旧执行检查点中“视频/PPT未制作、尚无实例入口”等过期状态。当前为本地审阅材料，未上传网盘、未提交报名系统。比赛奖项取决于评审和同届作品，工程完成度不能保证国一。

## 交付文件

| 文件 | 验证情况 | 字节数 | SHA256 |
|---|---|---:|---|
| `dist/final/SenseHome_6G_场景设计答辩.pptx` | 13页；已导出、逐页检查；末页联系方式和作品入口待真实信息 | 56,845 | `265e390df54a5909db9a71c51c0ea878b1c184f9713b46b3a9d576f8ffc8b71c` |
| `dist/final/SenseHome_6G_场景设计答辩.pdf` | 13页；PowerPoint无窗口导出，全部页面渲染检查通过 | 764,568 | `5055b3af98c409c007d3ffe9a9755769413ba48fa8f7ece5b0a3d81b7bda4997` |
| `dist/final/SenseHome_佐证材料_20261103.pdf` | 8页；全部页面渲染检查通过 | 102,324 | `fe9a7a89fbb2bf4334925bbccc554d40fd46cd2c33df93e4cb04b53658599a4b` |
| `dist/SenseHome_AI6G_演示视频_20261103_v2.mp4` | 245.047秒；1080p/30fps；H.264+AAC；全片解码与抽帧核验通过 | 6,594,060 | `cfcf37ab4f5630abf4306a37c16ebad18ecaf1392c99c1964d8efa3f4bddd782` |

作品方案仍为 `SenseHome-6G/作品方案_场景设计_v1.md` 及同名 DOCX/PDF。当前方案 PDF 为23页、843,782 B；目录、字体、脱敏和字数检查已通过。答辩逐页验证记录为 `_work/ppt_render/validation.json`；佐证 PDF 来源与 QA 见 `docs/evidence/`；视频媒体参数、原始软件事件和 QA 见 `docs/video/v2/`。

视频 v2 的0:25—1:00包含当前软件实际执行的开灯、只读查询、复合指令拒绝局部执行、关灯四次事件。画面标明“PC模拟设备/非现场录屏”，播放节奏经过编辑。事件与状态前后保存于 `docs/video/v2/software_replay.json` 和 `events.jsonl`；单次墙钟只代表本次样本，不当作性能分位数。原视频保留，v2为当前优先审阅版本。

## 已完成的本地技术验证

- Dashboard、Python网关、唯一C策略进程、动作白名单、PC JSON状态确认及结构化事件已经串联。
- `ready/inflight` 部分健康可驱动策略；未观测到的显存和队列保持未知。模型动作保护新增“目标房间必须在原指令中显式出现”、否定/条件变更整批澄清、透明动作归一化、模型4xx拒答硬门和执行确认文案；82项net_offload测试与20项Dashboard测试通过。
- 复合、条件和否定指令不会在本地回退中被局部关键词执行；危险操作与未授权安全控制拒绝执行。
- 当前验证通过70条策略断言、17条算力断言、82项net_offload Python测试、20项Dashboard Python测试及3项数字回归；新一轮n=1流水线烟测位于 `_work/final_checks_20261103b/smoke/`。n=1只验证流水线，不替代真实模型逐条证据。
- 数字复算、方案字数/脱敏以及PDF字体和目录检查通过。独立审查与执行记录分别位于 `docs/reviews/` 和 `docs/execution/`。

网络台架的权威n=20原始结果是 `_work/evidence_final/results.json`，共480条桩模型网络请求，SHA256为 `df0d74d19842797a8a6fb1d5f4db37fa804542640904085e9b558a73aeb433f0`。同源修正聚合 `results.corrected.json` 和 `provenance.json` 保留原始行及修正范围。历史n=20未保存完整当时源码清单，后生成manifest不能追认其全部源码版本。

另有160条Dashboard请求验证PC JSON模拟状态；开关灯与只读查询p95最大分别为4.434 ms和1.711 ms。这些数据不构成物理执行器、家庭试点、真实模型质量或6G空口的证据。TCP实测中的模型语义与耗时仍是桩合同。

## AutoDL与标签的当前边界

新的SSH入口已恢复。主执行流程已把Torch当前安装RECORD之外的三个旧文件 `flex_attention.py`、`flex_decoding.py`、`mm_scaled_grouped.py` 备份隔离，Torch inductor和vLLM导入验证通过。

Qwen2.5-1.5B-Instruct完整权重已恢复并校验：3,087,467,144 B，SHA256为 `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee`，revision为 `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`。最新提示词与归一化版本证据见 `docs/experiments/autodl_20261103_normalized_v2/`：20条请求均来自固定模型/revision，HTTP成功17/20、动作一致17/20（85.0%），回环请求中位数290.679 ms、p95 522.643 ms；A01/A02/A04 的422是房间缺失或白名单拒绝，属于fail-closed安全保护。`eval.formal.json` 是冻结后对已捕获真实模型响应的正式重新标注，3/3 预期拒答另行记录。

第三轮仍保留逐条原始动作、`raw_model_actions` 与 `action_normalization`，归一化仅处理明确房间的“有点亮/有点暗”、温湿度只读和模糊窗帘澄清，未绕过动作白名单。旧轮次保留在 `docs/experiments/autodl_20261103_baseline/`、`autodl_20261103_guard/` 和 `autodl_20261103_normalized/`。Qwen2.5-7B-Instruct 已从 main 冻结 revision `a09a35458c702b33eeacc393d103063234e8bc28` 并通过 immutable API/commit 复核。分片恢复因镜像 Range 在两个块上长时间无进展而为控制租用成本中断；已保留已完成块、`.download` 断点和下载证据，未发布大模型权重，也没有大模型推理结果。证据索引为 `docs/experiments/autodl_20261103_large_weights/`。

本文件不记录连接凭据。环境和后续真实执行状态以 `docs/real_model_attempt_20261103.md` 及其原始证据索引为准；产生新结果时，必须更新本节和受影响材料。

原始 `eval_commands.jsonl` 仍保留为草案历史；正式评测使用 `eval_commands.reviewed.jsonl`，20 行均为 `human_reviewed=true`，审核人、日期和 SHA256 见 `docs/deployment/20条动作标签冻结记录_20261103.json`。`eval.formal.json` 明确记录没有在冻结后重新推理，而是验证命令与期望动作完全一致后对已捕获响应重新标注。

## 剩余工作与责任边界

1. 小模型真实响应已按冻结标签完成正式重新标注，需继续分开报告质量、模型墙钟和端到端时延。大模型恢复包已准备并开始顺序下载，不能以单模型结果声明双模型切换通过。
2. 复杂任务原K1/K2时延目标尚未达到。观测缓存、静态本地/静态云端/自适应和消融需要新的设计与原始运行，不能沿用历史倍率或改写目标当作完成。
3. 真实业务需求、可接受指标、部署主机、成本与用户成效仍需要参赛者取得可核验外部证据。行业资料与需求画像不能代替已开展的访谈或试点。
4. 参赛者确认最终报名成员、队长、指导教师、正式团队名称及编号；本轮投入按1名学生安排，历史原型名单不能自动当作本赛名单。作品材料按规则保持匿名。
5. 参赛者提供可以公开展示的联系方式和作品入口，补齐答辩末页；按“团队编号-赛题名称-作品名称-材料名称”组织提交文件。
6. 生成永久有效网盘链接并核验提取码、外部可访问性和报名字段；目前没有网盘上传或正式提交证据。截止为2026-11-13 20:00。
7. 新真实模型证据产生后，按证据更新方案、答辩、佐证和视频；完成人工复核，再生成新的本地审阅快照并逐文件验证归档哈希。

真实5G/6G空口、切片、确定性承载、真实设备联锁与家庭试点均未验证。已按用户Esc停止桌面和浏览器UI操作；此后工作使用文件、CLI与SSH。

## 审阅包状态与复核入口

原快照 `dist/SenseHome_本地审阅包_20261103.zip` 保留。当前对外审阅包为 `dist/SenseHome_本地审阅包_20261103_v18.zip`（构建后以旁车 `.sha256.json` 为准），旁车文件列出归档文件数和逐文件哈希；最近一次构建通过 ZIP CRC 与身份信息扫描，包内证据仍明确大模型尚无推理结果。内部历史《提交前检查单.md》含身份信息，不原样纳入对外审阅包；以本文件作为当前公开检查点。

从工程根目录运行：

```powershell
./SenseHome-6G/net_offload/build.ps1
python _work/verify_numbers.py
python _work/check_draft.py
python _work/verify_pdf.py
```

完整检查入口为 `python _work/run_final_checks.py`，会产生新的验证输出；归档旧原始结果后再运行会写固定目录的脚本。恢复脚本和用法位于 `docs/deployment/`，具体远端解释器与环境以真实执行记录为准。
