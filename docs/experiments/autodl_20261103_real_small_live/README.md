# AutoDL 真实小模型预检（2026-11-03）

这是一次可复核的真实 GPU 模型预检，标签仍为 `human_reviewed=false` 草案，因此不得把 `60.0%` 写成正式准确率。

- 模型：Qwen2.5-1.5B-Instruct，served name `sensehome-small`
- GPU：NVIDIA GeForce RTX 4080 SUPER
- 推理链路：vLLM `0.33.0` → `openai_backend.py` → `/infer`
- 数据集：20 条固定草案标签，SHA256 见 `provenance.json`
- 结果：HTTP 成功 19/20；草案动作一致 12/20；请求中位数 264.0745 ms，p95 620.525 ms
- 安全：模型输出经过结构化 JSON、设备动作白名单和目标房间显式性校验；本次评测只做规划，不执行物理设备动作。

原始输出、适配器日志和 vLLM 日志均保留在本目录。正式评测前，先人工逐条冻结标签并记录审核人、日期和新数据集 SHA256，再用同一命令重新运行。
