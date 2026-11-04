# AutoDL 小模型第三轮运行与正式标注

本目录来自同一 AutoDL RTX 4080 SUPER 实例，使用 Qwen2.5-1.5B-Instruct 固定 revision、vLLM 0.33.0 和当前 `openai_backend.py`。本轮在提示词 few-shot 与透明动作归一化加入后重新运行 20 条标签，每条一次。原始逐条结果保留在 `eval.draft.json`，冻结标签后的正式标注报告见 `eval.formal.json`。

运行结果：HTTP 成功 17/20；在冻结标签下重新标注后，参考动作一致 17/20（85.0%）；请求中位数 290.679 ms，p95 522.643 ms。A01/A02 因模型产生了未提及房间的动作而由保护层返回 422，A04 因阳台动作不在白名单而返回 422。三条属于 fail-closed 安全拒绝，需单独报告 `refusal_correct`，不应被改写成普通模型语义错误或成功响应。

`eval.draft.json` 是运行时原始报告，使用当时的草案标签字段；`eval.formal.json` 已核验 20 条命令和期望动作与冻结副本完全一致，标记为 `human_reviewed`，并明确这是“冻结后对已捕获真实模型响应的重新标注”，没有伪称重新推理。17/20 是动作一致数；3/3 预期拒答另行记录。该结果仍不证明物理设备执行或家庭网络时延。

归一化响应保留 `raw_model_actions` 和 `action_normalization`，便于审计模型原始计划与规则修正的边界。模型权重、SSH 凭据和外部服务密钥未随本目录分发。
