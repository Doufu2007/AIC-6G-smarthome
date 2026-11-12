# v4 独立审查

结论：当前已审范围无未修复的 P1/P2。三项 P1 和两项 P2 已修复或明确更正，并完成对应复核。最终提交包仍在生成；封版核对记录见下文，未完成部分不标通过。

审查依据为 `docs/superpowers/plans/2026-11-11-competition-v4.md`。本审查未修改实现、未提交 Git、未启动模型或 MQTT 服务、未覆盖历史证据；仅运行离线复算及不会启动这些服务的定向测试。实际取消清理与完整模型冒烟由主任务执行，本审查读取其测试源码和保存结果，未自行重复运行。

## 发现与修复复核

| 编号 | 原问题 | 可复现触发与影响 | 当前状态 |
|---|---|---|---|
| V4-R01 / P1 | 需求前测界面全部为字面问号 | `docs/trial_v4/index.html` 原第 23—32 行标题、五个问题、跳过与按钮均为 `????`；正常开始后进入必经前测，无法理解需求问题。其余中文正常，故不是终端显示问题。 | 已修复。现为可读中文、中性问题，保留“没有困扰 / 不需要改变 / 跳过”；页面与协议测试通过。 |
| V4-R02 / P1 | 取消只删除反馈文件，仍保留原始命令日志 | 原页面承诺最终保存前不落盘，而 `TrialStore.execute` 已调正式网关：事件 JSONL 和持久回执数据库立即保存原始命令。原 `cancel` 仅删除 `data/<kind>/<sid>.json`，不删除两个私有运行目录。执行后取消即可残留命令。 | 已修复。页面提前说明即时暂存；每次启动只允许一个会话；取消、撤回或未保存关闭时先停止本次服务，再删除 v3/v4 私有目录及反馈。清理路径限定在两个私有根的子目录。已审阅真实取消测试：先确认事件与 SQLite 文件存在，取消后进程退出 0，`all_absent=true`，两目录及反馈均不存在。普通删除不宣称磁盘覆盖。 |
| V4-R03 / P2 | README 把规则负载证据写成模型启动证据 | 原 `docs/trial_v4/README.md` 将“模型真实启动与 HTTP 连接证据”指向 operations，但该运行为 rules-only。 | 已修复。README 分别说明真实取消测试和 `full_model_smoke.json`；operations 继续明确规则路径，未声称模型浸泡。 |
| V4-R04 / P1 | 集成 runner 重启时污染原项目事件文件 | `normal` 场景重启调用 `bridge._init_gateway` 时，`production_bridge` 已恢复原 `SENSEHOME_EVENT_LOG`，后续 integration-1 事件写入原 `openvela_smart_home/offload_events.jsonl`。并行 demo 关闭保护正确报 failed，不能据旧 run 宣称原项目事件不变。 | 已修复。runner 的场景环境持续固定私有事件路径，并保存原始文件 before/after。新 run `20261111T070837Z_7cfbfcf3` 离线 audit 通过，六个源码 SHA 匹配，原 state/events 的 before、after 及当前 SHA 三方一致。旧 failed manifest 保留；当前原 event + 保存的 8 行 / 32,461 B 追加 suffix 严格等于完整污染备份，原 event 恢复 SHA 为 `f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54`。 |
| V4-R05 / P2 | 最终演示 manifest 的继承字段与实际策略不符 | `final_runtime_manifest.json` 的继承 `policy_arm` 为 joint，而 `strategy` 为 local-first，四条实际事件的 `policy.arm` 均为 static-local。引用旧快照会错报演示策略。 | 已通过 `docs/video/v4/metadata_correction.json` 显式更正：本次实际为 local-first/static-local。原 manifest、运行记录及捕获源码不改写。后续版本再同步启动快照字段，本轮不改已封存源码。 |

## 独立复算

1. **三臂实验：PASS。** 直接导入 `verify_run.py` 调用 `verify()`，未重写其原 `verification.json`。96 行顺序、32 块、26 个注册文件哈希、初态、动作/状态完成条件、执行次数、POST 及 token 均一致。主分析为四条既有人工命令 × 六条件 × 三臂 = 72 行，另外 24 行空计划结构检查；不是 96 个独立任务。
2. **调用与时延：PASS。** 评测 102 次代理 POST = 66 次实际模型 POST + 36 次代理 503；12 条预热另存。评测 38,898 输入、4,173 输出、43,071 总 token。各臂主请求完成 20/24，全部主请求墙钟中位数分别为 955.506、1,265.0945、976.297 ms。预注册规则暂选 local-first；与 joint 差 20.791 ms，无显著性结论。另重算 `post_analysis.json` 的 162 项分段统计和 recommendation 的四项证据哈希，均一致。成功 60 条中 ≤250 ms 为 0/60，最短 341.576 ms；`c_internal` 全部为 null。
3. **正式回执：PASS。** 审阅 `durable_receipts.py`、`mqtt_receipts.py`、`offload_gateway.py` 和 `bridge_server.py`，确认先持久预留 unknown 再发一次；重试/重启查询已有命令；异内容冲突、partial 和未知结果不进入规则重发；回执绑定命令、请求摘要、动作及平台身份；历史回执与当前平台 snapshot 分离。对修复后的 `20261111T070837Z_7cfbfcf3` 运行独立离线 audit，通过四场景的 HTTP、MQTT 原始日志、网关/实体数据库和自有进程清理复核，其六个源码哈希与审查时源码一致。另独立核对 `protected_files.json` 的 before/after 及当前两个原项目文件 SHA 全部一致。
4. **新目录负载：PASS。** 独立重算 registration、rows、results、资源采样和 MQTT 数据库：120 行 = 116 可执行 + 4 无效，120 条均符合登记预期；含首任务共 121 条网关事件；117 个唯一 MQTT submit、117 条实体命令、状态版本 117；观察 120.000587 秒，最大派发计划偏差 19.995 ms。复制目录 `_work/operations_v4/20261111T064349` 内 57 个注册文件哈希全部匹配。230 个资源样本的 RSS 求和峰值为 147,460,096 B。人工工时仍 null、独立人工部署 false、外部反馈 0、总成本 null，未把规则负载写成模型浸泡。
5. **试用：PASS（限定范围）。** 需求在任务前必填或明确跳过，任务后冻结；完整模型是默认模式，rules-only 需显式参数；开发和自动化不计外部反馈；匿名引用默认为 false。独立运行 trial 单元及 HTTP 测试 11 项通过。另审阅 `full_model_smoke.json`：automation_test、复杂预设任务完成 true、MQTT applied、真实模型 usage 816 token、physical false、外部反馈 0。此记录不作为真人采集结果。
6. **材料事实：已审至生成源。** `build_submission_v4.py`、`content_model.json` 和主张索引中的 20/24、72/24、0/60 与旧 0/18、66+36 与预热、固定 contract probe、软件实体、120 秒规则负载、成本 null 和反馈 0 口径与对应证据相符。最终材料应明确指向修复后的 MQTT run `070837Z_7cfbfcf3`，不用旧 run 宣称原文件隔离；最终页码与视频仍由封版检查确认。

## 已运行检查

```text
verify_run.verify(): PASS（只读，不改原 verification.json）
verify_integration.py docs/integration_v4/runs/20261111T070837Z_7cfbfcf3: PASS
新run六项源码SHA及原state/events before/after/当前SHA: PASS
污染备份 == 恢复原始文件 + 保留追加suffix；旧failed manifest保留: PASS
独立 operations 原始行/文件哈希/事件/采样/SQLite 重计: PASS
独立 post_analysis 与 recommendation 哈希复算: PASS
pytest receipt 边界、bridge 回执、comparison 定向测试: 18 passed, 12 subtests passed
pytest trial_v4/test_trial.py trial_v4/test_http.py: 11 passed
```

主任务保存的 `docs/trial_v4/verification.txt` 为含真实 MQTT 取消测试的 12 passed；本审查只读取此结果，不将它表述为独立重跑。

## 限制

未运行根目录全量 pytest，避免旧 `_work` 收集冲突；未启动模型/MQTT，未作实物、独立主机、WAN 或 6G 验证。未独立重新招募参与者。四命令一次回归不支持一般自然语言泛化或统计优势。视频、最终 PDF/PPT 页面、永久链接及最终 ZIP 尚不在本次已完成审查范围内。

## 封版核对补充（进行中）

- 最终运行源：`final_runtime_records.json` 四条记录均为 automation_test。开灯与复杂二动作均有 applied 回执，软件版本 0→1→2；模糊和越权命令均不执行，版本保持 2。最终模型采集冒烟另提交一次，版本到 3、真实 usage 815 token，外部反馈仍 0。三次唯一 MQTT submit 与实体 SQLite 三条命令逐一绑定。12 个捕获源码文件的当前/快照/manifest SHA 完全一致。
- 退出边界：最终模型 runtime 标为 stopped，两个模型子进程退出码原样为 1；MQTT 子进程退出 0。仅称所有自有进程已停止，不将模型退出码描述为正常 0。演示数据来自真实 API 操作，后续视频是编辑回放，不是连续实时 UI 录屏。
- 新增 `verify_operations.py` 已读源码并仅运行不带写参数的离线核验，PASS；其 57 份封存复制源已独立重核。
- 当前 PDF 方案 14 页、答辩 13 页、佐证 14 页，全页缩略渲染布局已视查，无溢出或重叠；十二项主张索引的 36 处页码均落在对应主题页面，原始路径均存在。最终文件哈希、完整视频和两种 ZIP 等待成品稳定后补记。
