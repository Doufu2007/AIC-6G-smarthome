# 需求价值线工作记录（2026-11-11）

任务：执行 `docs/superpowers/plans/2026-11-11-evidence-completion.md` 的 Task 2；依据对应设计文档，补齐可独立完成的需求背景、对标、AI+6G 映射、部署、试用与成本工具。

- 已知约束：用户没有可提供的真实经历；尚无访谈、问卷、家庭试点或客户确认。不联系第三方。用户最新决定只用本机，不再使用 AutoDL。
- 裁定：本线仅更新 `docs/value/` 与 `.gitignore` 的 `.firecrawl/` 忽略规则，不改主材料；父代理负责集成与最后审查，不做 Git 提交。
- 裁定：使用官方公开资料证明现有能力和技术背景，不能据此证明用户痛点频率、购买意愿或本作品优势。试用表保持未采集。
- 使用执行计划、firecrawl 和 TDD 技能；成本 CLI 的测试先于实现。文稿不增加机械性文本测试。
- 外部资料：firecrawl CLI 1.26.3，未登录，keyless scrape 成功；原始抓取在被忽略的 `.firecrawl/`，交付内保留短摘录和哈希。
- 成本接口：父代理稍后提供本机资源测量；内存/CPU 时间不转换成电量。没有墙上功耗与电价就不输出完整实测总成本。
- 测试记录：首轮 7 个测试在无实现时按预期失败；实现后发现测试手算夹具的局部活跃时长写错（1,500×3秒=1.25小时），已将夹具改为3.6秒，与独立手算1.5小时一致，不改正确公式。
- 资源事实已接收并复算：`docs/experiments/20261111_local_resources/baseline.json`，30样本，摘要与原件SHA256在`host_resources_summary.json`。不使用WMI截断显存值，不由GPU板卡功率推整机电费。
- 官方材料核验9项，访问日期2026-11-11，保留官方URL、必要短摘录、哈希和支持范围。Home Assistant已有本地意图优先/LLM路径，已纠正差异化边界。
- 成本CLI 7项测试通过；4份情景配置成功输出。当前配置总成本未知；3份USD演示均标假设+公开报价，不称实测。
- 用户数据状态：未招募、不联系第三方，`trial_sessions.csv`只有列头，样本为0；真实需求、试用、整机电量/电价、账单与物理部署仍为外部依赖。
- 最终验证：`python -m unittest discover -s docs/value -p test_cost_calculator.py -v` → 7/7；`python -m py_compile docs/value/cost_calculator.py docs/value/test_cost_calculator.py` → 通过。4份保存的JSON输出与调用calculate重新计算完全相同。
- 最终来源核验：9项官方短摘录均存在于对应原始抓取，SHA256一致；资源摘要与baseline原件哈希一致；试用CSV数据行=0。`git diff --check -- .gitignore docs/value` 无空白错误（Git仅提示将来LF→CRLF转换）。
- 本线交付20个文件（不含被忽略的pycache）及`.gitignore`一条忽略规则。没有Git提交、没有第三方联系、没有覆盖旧实验或修改根README/方案/PPT。完整项目回归和跨材料审查由父代理集成验收。
