# 新版本真实本机模型烟测

共执行三轮，每个版本各一次，均使用 `smoke_real.py --run-real`，通过现有 `IntegratedSession(models=True, mqtt=True, strategy='local-first')`、真实 loopback HTTP 和私有 MQTT 软件实体完成。首轮为 2026-11-12 03:47:10—03:47:42 UTC，原始证据在 `_work/intrinsic_fixes/real_smoke_20261112T034709Z_ea07f57a/`；空计划恢复修复后的第二轮为 04:15:41—04:16:13 UTC，原始证据在 `_work/intrinsic_fixes/real_smoke_20261112T041541Z_63d214ad/`；提示词及模型输入补强后的第三轮为 04:20:41—04:21:10 UTC，原始证据在 `_work/intrinsic_fixes/real_smoke_20261112T042041Z_4ab4a80a/`。后两轮均在主控修复、测试与明确授权后执行，使用同一组命令和新输出目录，没有对同一版本重复运行挑选结果。

前两轮整体结果均为 `failed_or_incomplete`：规则、澄清、离家支持范围达到预期；明确双房间命令首轮被首端误澄清，第二轮进入备用但备用计划结构不合法。第三轮为 `passed`：首端仍误澄清，备用输出合法双动作并经 MQTT 软件实体执行及目标验收，16/16 项检查为 true。下面完整保留前两轮失败与数值，再列第三轮结果；三轮烟测不用于估计总体模型准确率。

| 请求 | HTTP 耗时 | 结果 |
| --- | ---: | --- |
| 把客厅灯光设为60% | 112.369 ms | MQTT applied，task_complete=true |
| 把客厅灯光设为35%，再把卧室灯光设为25% | 563.652 ms | 真实模型空动作，task_complete=false，未执行 |
| 有点暗 | 16.138 ms | 请求房间与亮度，task_complete=false |
| 卧室（同会话） | 28.308 ms | 请求亮度，task_complete=false |
| 调到60%（同会话） | 114.238 ms | 安全续接，MQTT applied，task_complete=true |
| 离家模式 | 143.569 ms | 支持的灯光/窗帘范围完成；安防未接入，task_complete=false |

六次命令均为 HTTP 200。这里的 HTTP 成功仅表示请求得到结构化响应；任务是否完成以上表中的验收字段为准。

## 首轮真实模型证据与复杂失败

本次启动固定配置中的 Qwen2.5-1.5B-Instruct-Q4_K_M 和 Qwen3-4B-Q4_K_M。既有 `LocalRuntime` 核验可执行文件、依赖和 GGUF 权重的固定 SHA256，启动自己拥有的 llama-server 子进程并检查模型 inventory；没有用 fixture 替代模型。

只有 local 槽发生一次真实 POST，cloud 槽只有健康请求。原始 model content 为：

```json
{"answer":"请指定目标亮度百分比。","actions":[]}
```

原命令已明确给出 35% 和 25%，因此这是本次模型理解失败。模型 POST 状态 200，观测 HTTP wall 为 520.632 ms，没有超时或 JSON 解析错误；原始 completion 15 tokens，prompt 731 tokens。适配器得到合法空计划，`recovery.reason=primary_success`，没有调用备用规划器；网关最终保留 task_complete=false，没有提交错误动作。

首轮烟测只调查和报告该失败，没有由烟测脚本调整生产代码、修改提示词或替换输入。随后主控新增了仅针对完整目标合同下空计划的单次备用恢复，经定向测试和独立审查后，明确授权执行一次第二轮；首轮原始证据和数值保持原样。

## 首轮隔离、持久路径与清理

应用使用新的 v4 run `_work/demo_v4/20261112T034710_f5715208`、新的 v3 run `_work/demo_v3/live_20261112T034712_98878fcd`；私有稳定数据目录位于本次输出目录的 `data/`，保留实体/网关/请求账本证据，未复用旧部署或封版状态。最终实体 state_version=3，对应单规则、澄清落地、离家支持范围三个动作批次。

记录已包含六次完整原始 HTTP 文本及结构化响应、请求耗时、初末 HTTP 状态、实体末态、model attempts、上游原始交换、完整 runtime/integrated/gateway manifest、模型日志和健康/事件日志。脚本拒绝已有输出目录，所有结果文件以排他创建方式写入。

所有自有 HTTP 线程、C 策略、MQTT broker/entity 和两个模型进程均已停止。模型 PID 29348、5976 的结束码均为主动终止时的 1；不是模型服务启动失败。清理检查全部为 true。

原文件运行前后 SHA256 相同：

- `openvela_smart_home/device_state.json`：`22a1550ab65c3db310291c778f67e9d0b3a8b19838a1ebc6fd40ca79f33e929d`
- `openvela_smart_home/offload_events.jsonl`：`f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54`

本次源文件指纹在运行期间也未变化。该运行证明真实本机模型服务和 MQTT 软件实体的本次行为，不证明物理设备、实际云/边独立部署、6G 链路或总体模型准确率。旧封版结果不作为本次新版本模型成功的证据。

## 修复后第二轮全部结果

第二轮与首轮的六条命令、模型配置 SHA256、可执行文件哈希、两份模型权重哈希、`local-first` 策略及初始灯光/窗帘/温湿度组完全一致。两轮记录的生产源指纹只有 `docs/demo_v3/planning_recovery.py` 发生变化，对应主控已审查的空计划恢复修复；每轮运行期间源文件均未变化。

| 请求 | HTTP 耗时 | 结果 |
| --- | ---: | --- |
| 把客厅灯光设为60% | 140.339 ms | MQTT applied，task_complete=true |
| 把客厅灯光设为35%，再把卧室灯光设为25% | 4068.765 ms | 首端空计划后调用备用，备用动作结构不合法被拒绝，task_complete=false |
| 有点暗 | 15.352 ms | 请求房间与亮度，task_complete=false |
| 卧室（同会话） | 28.576 ms | 请求亮度，task_complete=false |
| 调到60%（同会话） | 112.212 ms | 安全续接，MQTT applied，task_complete=true |
| 离家模式 | 115.221 ms | 支持范围完成，security_arming 未接入，task_complete=false |

第二轮首端 Qwen2.5-1.5B 仍返回与首轮相同的空动作 JSON，上游 POST wall 为 546.447 ms。恢复包装器将其标记为 `EMPTY_PLAN_FOR_RESOLVED_REQUEST`，查询备用健康后确实调用 Qwen3-4B。备用真实上游 POST 为 HTTP 200、wall 3447.710 ms，原始 content 为：

```json
{"answer":"","actions":[{"name":"light_control","params":{"location":"客厅","action":"set","brightness":35}},"name","light_control","params","location","卧室","action","set","brightness",25]}
```

这是可解析 JSON，但 `actions` 不是全体白名单动作对象，不能视为合法两步计划。适配器以 `INVALID_MODEL_ACTIONS` / HTTP 422 拒绝整个计划，恢复记录为 `secondary_failed_terminal`；规划总 wall 4008.850 ms，未超出 10s 总预算。最终 HTTP 入口仍返回结构化 200、actions=[]、task_complete=false，没有把前半个合法动作提前提交。

本轮共 2 次真实模型 POST，分别来自首端与备用；没有重试、换命令或第三次模型调用。六条 HTTP 命令均得到 200；除复杂任务完成检查为 false 外，其他预期、软件执行范围、清理和保护检查全部为 true。

第二轮 v4 run 为 `_work/demo_v4/20261112T041541_ab5d8576`，v3 run 为 `_work/demo_v3/live_20261112T041543_7b51e483`；私有稳定 `data/` 路径、所有启动 argv 和实际 PID 均保存在该轮 manifest。最终实体 state_version=3，与首轮一样只执行规则、澄清和离家三个批次。两个模型 PID 为 17232、19980，清理结束码均为主动终止时的 1；HTTP 线程、模型、MQTT 与 C 策略进程全部停止。原 state/event 运行前后及两轮之间的 SHA256 均与上文一致。

第二轮只证明空计划后的备用调用已接通、非法备用计划被完整拒绝；不能据此声称复杂模型任务完成，也不能从两个 smoke 样本估计总体准确率。随后主控对动作对象格式、完整目标处理及模型 user 输入做最小补强，经回归测试和独立审查后，明确授权执行一次第三轮。烟测执行者未修改生产。

## 输入补强后第三轮全部结果

本轮仍使用完全相同的六条命令、模型配置、固定 GGUF 权重及 `local-first` 策略。提示词新增每个 `actions` 元素必须是完整动作对象、已分别给出目标时不得重复询问的说明，以及厨房 18%／书房 72% 的双房间示例；示例没有使用本次客厅 35%／卧室 25% 的输入。真实上游请求的 user message 仅为 `{"command":"把客厅灯光设为35%，再把卧室灯光设为25%"}`；外层 model attempts 仍保留请求 ID、会话与计算观测日志。

独立只读比对确认三轮命令、配置 SHA256、llama-server 可执行文件、30 个运行依赖文件、两份模型权重和槽位配置一致；初始 lights、curtains、temperature、security 一致，动态 sensors 不同，因此不声称全量初态完全一致。指纹覆盖存在一项缺口：三轮 summary/integrated/gateway manifest 均未包含 `openai_backend.py`，第 2→3 轮已记录源指纹均相同；这些记录不能证明该文件运行期间字节不变或只修改了该文件。新提示词和仅 command 的模型输入已从原始上游 HTTP messages 直接核实；未补造历史哈希，也未为补齐指纹再次运行模型。

第三轮结束后，按主控要求扩充 `smoke_real.py` 的 `SOURCES` 清单，补入 `openai_backend.py`、`online_metrics.py`、`resource_policy.py`、`backend_monitor.py`、`clarification_sessions.py`、`state_freshness.py` 和 `bounded_http.py`。新清单仅用于未来运行；前三轮原始记录仍按各自原清单解释。补清单后只执行 `--check` 和 `py_compile`，没有第四轮模型烟测，未回填或覆盖前三轮证据。

| 请求 | HTTP 耗时 | 结果 |
| --- | ---: | --- |
| 把客厅灯光设为60% | 96.014 ms | MQTT applied，task_complete=true |
| 把客厅灯光设为35%，再把卧室灯光设为25% | 3473.482 ms | 首端空计划后调用备用，合法双动作 MQTT applied，目标验收通过，task_complete=true |
| 有点暗 | 18.671 ms | 请求房间与亮度，task_complete=false |
| 卧室（同会话） | 18.924 ms | 请求亮度，task_complete=false |
| 调到60%（同会话） | 100.634 ms | 安全续接，MQTT applied，task_complete=true |
| 离家模式 | 96.717 ms | 支持范围完成，security_arming 未接入，task_complete=false |

六条入口请求均为 HTTP 200。本轮首端 Qwen2.5-1.5B 的真实 POST 为 HTTP 200、685.743 ms，原始 content 仍是误澄清：

```json
{
    "answer": "请指定目标亮度百分比。",
    "actions": []
}
```

因此不能声称首端模型已能正确理解该复杂命令。恢复包装器记录 `primary_error=EMPTY_PLAN_FOR_RESOLVED_REQUEST`，只调用一次备用 Qwen3-4B。备用真实 POST 为 HTTP 200、2688.360 ms，原始 content 为：

```json
{"answer":"","actions":[{"name":"light_control","params":{"location":"客厅","action":"set","brightness":35}},{"name":"light_control","params":{"location":"卧室","action":"set","brightness":25}}]}
```

适配器将其作为合法两步计划返回，`recovery.reason=secondary_success`，规划总 wall 3382.285 ms，仍在 10s 规划预算内。随后 MQTT 软件实体回执为 `applied`，两个目标均由当前观测验收，`goal_acceptance.accepted=true`、`observation_covers_receipt_version=true`，入口最终 `task_complete=true`。两次模型 POST 的 prompt 均为 704 tokens，首端 completion 为 24 tokens，备用 completion 为 51 tokens。本轮没有第三次模型调用或自动重试。

本轮 `summary.json` 的 16 项检查全部为 true，包括规则任务、复杂任务、两步澄清后续接、离家支持范围及未接入安防声明、HTTP 状态、软件执行范围、清理和保护。`passed` 表示这些预设验收条件通过；离家任务仍明确 `task_complete=false`，只确认灯光和窗帘支持范围。

第三轮 v4 run 为 `_work/demo_v4/20261112T042041_44739942`，v3 run 为 `_work/demo_v3/live_20261112T042043_49523c98`；持久路径为本轮输出目录下新的 `data/`，启动账本标记 `resumed=false`。最终实体 state_version=4，较前两轮多出复杂双房间动作批次。模型 PID 3536、20648 均已主动终止，结束码均为 1；C 策略 PID 36764 结束码为 0。MQTT `cleanup.json` 记录 entity PID 35572、broker PID 36732 均正常退出、结束码 0、`forced_termination=false`、`all_stopped=true`；HTTP 线程与所有自有子进程清理检查均为 true。原 state/event 的运行前后 SHA256 与前两轮及上文值一致；记录所覆盖的生产源指纹运行期间也未变化。

第三轮证明本次固定本机模型配置能通过备用恢复完成这一条复杂命令，并且没有把首端空计划声称为成功；它没有消除前两轮失败，也不构成模型准确率或时延改进的统计证据。local/cloud 是本机两个槽位名，两端均为 loopback llama-server，不代表已测量远端云 GPU 或 6G 链路。三轮之后没有继续运行烟测。

## 旧封版只读核验

另见 `sealed_preservation.json`，全程未调用会写旧结果的 `audit_sealed_runs.py`，只读取 seal、包文件和冻结源快照计算 SHA256。

- 6 个 v1—v4 包文件均匹配各自 seal 哈希。
- 4 组 fused 封版的 24 项结果文件与 44 项源快照全部匹配；v4 frozen_source、operations source_snapshot 均匹配对应记录。
- 93 项材料 manifest 的来源/输出引用全部匹配。
- 137 项封版保护检查中 136 项匹配。唯一不符是 `docs/experiments_v4/test_comparison.py`：final_manifest 期望 `47dac11bf199d1e1a810fb20b9857b8b7bddb5ebe5ae2270efd2b0db501b0bf9`，检查时当前为 `4c0b53a173cd42d902a0af0074ceea4e736db280e2b1f294598d8d4b75a69dd5`。已报告主控，未自行覆盖旧测试文件。
- 24 项旧 source_sha256 与当前生产源码不同，作为信息项保留；对应冻结源快照仍匹配，因此没有把版本升级误判成旧结果损坏。

这项核验只证明与所存哈希记录一致的字节完整性，不重新核算旧模型结果，也不把旧测试或封版结果升级为新模型证据。

主控随后将 `docs/experiments_v4/test_comparison.py` 恢复为原封版哈希。只读终检重新读取全部已列字节，结果见 `sealed_preservation_final.json`：137/137 保护检查、93/93 材料来源/输出引用匹配，6 个旧包仍全部匹配。初检记录原样保留，未覆盖历史检查结果；24 项当前生产源码差异仍为信息项。
