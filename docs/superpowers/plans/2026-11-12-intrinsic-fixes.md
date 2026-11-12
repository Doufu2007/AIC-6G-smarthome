# Intrinsic Project Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or executing-plans. 任务的失败回归、实现、验证均在本轮完成。

**Goal:** 修复审计F01—F11并证明新版本运行行为。
**Architecture:** 保留现有Python/C网关，增加可信目标合同、会话、请求账本和在线观测；连接、持久化、截止时间采用统一生命周期。
**Tech Stack:** Python 3.13、unittest/pytest、SQLite、paho/AMQTT、C策略、原生JS。
**Spec:** `docs/superpowers/specs/2026-11-12-intrinsic-fixes-design.md`

## Global Constraints

- 当前功能分支原地修复，备份旧源文件；不reset/clean/commit/push。
- 旧封版证据、原state/event不变；所有验证使用私有目录与自有进程。
- 不连接AutoDL、不进行桌面操作，不重新要求20条标签确认。
- 资源字段保留真实来源；不将软件提交或平台状态称为物理执行。

## Review Focus

- 错计划、漏目标与部分执行不能报告任务完成。
- 会话切换、取消、过期、重复ID和响应丢失不能产生跨会话或重复动作。
- 慢推理、健康轮询、断线恢复并发时不阻塞查询或破坏共享C管道。
- 整体重启只恢复持久记录，不自动重发unknown命令，不覆盖设备状态。
- 在线代价和队列缺失时不得补零或声称真实GPU/6G观测。

### Task 1: 目标验收、语言续接与场景统一（A）

Files: bridge_server.py / device_actions.py / offload_gateway.py / backends/openai_backend.py、新目标/会话模块及回归测试。
Interfaces: context.session_id、context.command_id；返回plan_valid/execution_complete/task_complete及保守semantic_complete。
- [x] 复现F01—03、F09失败回归。
- [x] 实现目标抽取、提交前匹配与执行后验收，未解析规则升级、服务器澄清状态、统一场景巡检。
- [x] 跑相关网关/模型合同/dashboard测试，自查并写工作报告。

### Task 2: MQTT连接恢复与数据路径（B）

Files: mqtt_receipts.py、新测试。
Interfaces: LocalMQTTSession(..., data_dir=None)；现有submit/lookup/snapshot保持兼容。
- [x] 测试原连接断开后恢复仍能查询、unknown不重发、双查询不串包。
- [x] 实现连接/订阅恢复与关闭竞态保护，分离运行目录和持久数据目录。
- [x] 跑MQTT与回执测试并报告。

### Task 3: 应用整体持久重启（C）

Files: docs/demo_v4/live_v4.py、test_live_v4.py与持久部署说明。
Interfaces: IntegratedSession(..., data_dir=None)；调用B的MQTT data_dir与v3.make_live_server(state_dir=...)。
- [x] 整体关闭再启动使用同目录，状态/命令保留；第二进程拒绝占用目录。
- [x] 加持久模式CLI，记录目录与恢复情况，运行日志独立。
- [x] 跑启动与恢复测试并报告。

### Task 4: 并发、截止时间、请求ID（主控）

Files: run_isolated_gateway.py、run_live.py、planning_recovery.py、web_panel.html、新请求账本模块及测试；A完成后接线bridge。
Interfaces: persistent request ID bound before planner; deadline monotonic internal only; request status query。
- [x] 先写并运行F04/F05/F08回归。
- [x] 有界HTTP并发、预算传递/拒绝迟到计划；请求ID持久绑定与重试/回查前端；v3稳定state_dir支持。
- [x] 新鲜度标记与非阻塞缓存状态查询接线，巡检页面正确处理失败。
- [x] 检查共享资源锁顺序并跑定向测试。

### Task 5: 在线观测与代价更新（主控）

Files: fused_eval.py、run_live.py、新telemetry模块及测试；A完成后接线bridge/策略观测。
Interfaces: 来源标明adapter admission queue/inflight、per-task cost；上游GPU未知仍unknown。
- [x] 受控慢请求产生真实适配器队列，在线分任务代价随样本更新，过期观测失效。
- [x] 实现在线代价输入策略，验证受控竞争下选择变化，保留静态默认与未知字段。
- [x] 报告实测适用范围与基线结果。

### Task 6: 集成与独立审查

- [x] 相关回归集合与根pytest执行结果记录。
- [x] 隔离HTTP+真实MQTT验证目标验收、断连、重启、回查、巡检和并发。
- [x] 独立审查新代码，处理有实质影响的发现并复验。
- [x] 完成F01—F11修复矩阵、保护文件哈希及运行说明。
