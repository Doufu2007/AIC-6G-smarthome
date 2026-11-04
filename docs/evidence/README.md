# 佐证材料 PDF 与构建来源

- 成品：`dist/final/SenseHome_佐证材料_20261103.pdf`，8 页 A4。
- 重建：从项目根目录运行 `python docs/evidence/build_evidence_pdf.py`。
- 输入：`source_manifest_20261103.json` 列出读取的原始/派生 JSON、Dashboard 数据、验证日志索引、依赖披露与最新真实模型部署状态的字节数和 SHA256。
- PDF 包含：范围说明、软件闭环/防护、n=20 台架设计、复杂任务两臂结果、160 次 Dashboard 模拟状态证据、源码与数字门、复用与 AutoDL 状态、核验文件哈希。

本附件不将模型桩语义、PC JSON 状态、TCP 回放或假上游测试表述成真实模型、物理执行器或 6G 空口实测。n=20 的历史源码清单不完整问题已保留；后生成 manifest 与 n=1 烟测不回填历史。人工标签复核仍未完成。

验收：`pdftoppm -r 120 -png` 渲染了全部 8 页，逐页目视检查通过，未见裁切、文字遮挡、缺字或表格溢出。渲染图与拼图位于 `_work/evidence_pdf_qa/`；机器检查记录 `docs/evidence/qa_20261103.json`。Poppler 输出了本机全局字体映射警告；实际 PDF 使用嵌入的 SimHei 子集，8 页渲染均正常。

PDF 中的环境准备描述来自 `docs/real_model_attempt_20261103.md`，它不构成 GPU 推理成绩。真实模型结果产生后，应新建证据版本并更新材料。
