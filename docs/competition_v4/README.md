# SenseHome v4 正式材料

主线为家庭复杂指令在后端异常时的恢复与执行核验。目标读者是场景设计评审；按业务问题、处理流程、同条件比较、动作回执、复制部署和价值验收组织内容。旧材料不逐轮堆叠到正文。

生成器：`docs/submission/build_submission_v4.py`。运行 `python -X utf8 docs/submission/build_submission_v4.py`，仅写入 `dist/final/20261111_v4/`、本目录及 `_work/submission_20261111_v4/`，不覆盖 v1—v3。

`results_inputs.json` 明确锁定新增原始 JSON 路径。生成器不按最新日期自动选择运行；未锁定或不存在的结果按待验收处理，不能计为已实现。最终材料与 `claims_index.json`、`主张指标证据页码索引.md` 共享一个内容模型。

分层口径：当前真实模型仍在同一台电脑的两个逻辑槽位，共享 GPU；受控 HTTP 故障与等待是软件代理实验；JSON 状态与 MQTT 软件实体均不证明实物执行；未验收真实 6G、外部家庭试点或独立主机。外部反馈在真实记录保存前为 0，成本必要分项未知时总额为 null。历史 600 行、120 行、36 行分别保留，历史 250ms 达标 0/18 不删除。

技能与环境：已阅读 documents、pdf、presentations 技能；当前工具目录中无 `load_workspace_dependencies`，也未提供其指定的 artifact-tool 运行时。采用项目已有 python-docx、python-pptx、reportlab 与本机 LibreOffice 的替代链；生成前已运行各技能 authoring marker。DOCX 使用技能 `render_docx.py` 渲染；所有 PDF/PPT 页面另行渲染并目视检查。替代链是环境记录，不作为业务功能或评审卖点。

正式上传仍需参赛者真实报名信息、永久分享链接及提取码、最终人工核对与上传。源码、索引和内部渲染记录不含参赛者姓名、学校、联系方式、密码或本机绝对私人路径。

最终材料检查记录见`material_qa.md`与`material_qa.json`。视频为当前软件证据编辑回放，清单固定为`docs/video/v4/manifest.json`；MQTT固定为`20261111T070837Z_7cfbfcf3`。生成器会重置视觉审核状态，重建后须重新检查受影响页。
