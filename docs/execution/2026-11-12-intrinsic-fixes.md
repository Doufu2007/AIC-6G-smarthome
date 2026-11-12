# 项目自身缺陷修复执行台账

计划：`docs/superpowers/plans/2026-11-12-intrinsic-fixes.md`。状态：六个工作包完成，最终验证通过。保留已有未提交功能分支，修复前字节备份位于 `_work/intrinsic_fix/before/`，没有重置、提交或推送。

| Tasks sharing interface | Contract | Ruling |
|---|---|---|
| A / root | bridge context and completion | A owns bridge until handoff; root adds lifecycle hooks after. |
| B / C | MQTT data_dir | B owns transport; C consumes optional data_dir. |
| C / root | v3 state_dir | root implements v3 initialization preservation. |
| A / frontend | session_id / command_id | context IDs; server owns trusted slots. |
| Task self-check 1?6 | spec coverage | all F01?F11 assigned; no automatic external actions. |

Task 1: completed — 完整目标合同、会话澄清、统一场景、提交前目标持久化。
Task 2: completed — MQTT重连重订阅、未知只查询、关闭/发布deadline和独立数据目录。
Task 3: completed — 整体持久重启、OS目录锁、schema检查和启动当前实体对账。
Task 4: completed — 有界HTTP、10秒提交期限/首端4秒、预规划请求账本、页面回查与历史归档。
Task 5: completed — 真实适配器计数、分任务在线代价、实际C策略接线；未知GPU/6G仍未知。
Task 6: completed — 集成、独立审查、补修和最终证据。

最终验证：活动源码384项/194子测试通过，历史兼容5项通过，Node交互5项通过。统一日志 `_work/intrinsic_fix/checks_20261112T042109Z/`，机器记录 `docs/intrinsic_v5/repair_report.json`。

真实烟测保留三轮：首轮明确双房间被1.5B误澄清；第二轮备用4B格式非法被拒绝；补模型输入隔离及不同房间/数值多动作示例后第三轮通过。第三轮首端仍空动作，一次备用生成正确两动作，MQTT applied且目标验收完成；复杂HTTP3473.482ms。相同命令/模型/初始设备状态/策略，仅在代码变更后新建一轮，未覆盖失败。原烟测hash清单未覆盖全部传递依赖，不回填历史指纹；新输入由原始messages实证。

曾有一次MQTT启动超时，定向及最终全集通过，保留失败且未放宽timeout。原state/event哈希不变；旧包6个、保护哈希137项、材料引用93项全部匹配。本轮自有进程退出。报告见 `docs/intrinsic_v5/repair_report.md`、`final_review.md`、`real_smoke_report.md`。

结论限于本机模型和MQTT软件实体：安防未接入，没有物理动作/实际6G验证；本轮未证明joint优于简单策略、250毫秒任务完成或总体模型准确率提高。旧封版结果保留为历史证据。
