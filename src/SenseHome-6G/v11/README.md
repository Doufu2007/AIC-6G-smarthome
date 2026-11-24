# SenseHome v11：受控提案、资源路由与持久执行

Python 3.10+，无需第三方Python包。请保留同级 `../ai6g_runtime/`，v11直接复用已有TaskRouter；发布ZIP包含这些依赖。v10及历史实验保留，v11使用新数据目录。

## 本机运行

在本目录打开终端：

```powershell
python -X utf8 -B cli.py --home demo_v11 init
python -X utf8 -B cli.py --home demo_v11 propose "打开阅读灯和客厅顶灯"
```

核对当次全部对象和动作，在30秒有效期内明确确认：

```powershell
python -X utf8 -B cli.py --home demo_v11 confirm --proposal-id <当次ID> --digest <当次摘要> --request-id task-001 --confirm
python -X utf8 -B cli.py --home demo_v11 status
python -X utf8 -B cli.py --home demo_v11 receipt task-001
python -X utf8 -B cli.py --home demo_v11 reconcile task-001
```

卡片绑定动作状态和选择依赖。动态选择范围内设备发生变化或变得不可读时，需重新提案；系统不会向旧卡追加动作。同卡同请求重复确认读取原回执，另一张卡使用新请求ID。过期后重新提案。未配置平台时使用软件状态后端。

`software_states.json`是首次创建的种子。v11软件状态保存在旁边的 `software_states.json.sqlite3`，所有实例事务读取、更新；后续用status查看。重新开始使用新home，不要删除正在运行的数据文件。

## 语义与能力

支持即时灯开关、0—100整数百分比亮度、窗帘开关；明确名称/别名、房间与设备类型、并列对象、顺序子句、当前亮着的灯。整个请求必须匹配受支持语法。

- “不要打开阅读灯”“10秒后打开阅读灯”“今晚打开阅读灯”：空动作澄清，当前不提供定时任务。
- “关闭书房当前亮着的灯”：只筛选书房；范围内设备状态不完整则澄清。
- “打开阅读灯和客厅顶灯”：完整保留两个对象。
- 亮度12.5%或-10%：澄清，不截取部分数字。
- 不支持亮度的实体不会收到亮度确认卡片。新适配器提供明确能力；旧自定义SDK客户端未声明能力时保留兼容行为，应补齐声明。

默认严格模式下，模型候选须与独立规则动作完全一致。显式开启模型语义提案后，可处理有限的即时肯定句式，例如“让阅读灯柔和一点”“我想看书，帮我点亮适合阅读的那盏灯”。对象、能力、状态及参数均受校验；用户在确认卡上核对用途、方向和数值。缺少上下文、否定、定时或条件请求会进入澄清。

## 原生 Home Assistant

在私有环境变量设置SENSEHOME_HA_TOKEN，并使用自己的HA地址：

```powershell
python -X utf8 -B cli.py --home native_v11 --ha-url http://127.0.0.1:8123 --ha-native discover
```

discover只调用官方状态GET。检查分区（states未提供时为“未分组”）、名称和别名，将输出catalog数组保存为native_v11/catalog.json。propose/confirm/status均带同样的ha-url和ha-native；无须软件种子文件。

原生light亮度根据supported_color_modes，cover开关根据supported_features，未声明能力保守拒绝。发送官方/api/services/light或cover服务，执行后独立GET最终状态。

虚拟helper另用 `--ha-url ... --ha-map mapping.json`，逻辑light映射input_boolean，只支持空参数开关。ha-native与ha-map互斥。token来自环境变量，HTTP审计脱敏。v11原生适配的localhost HTTP契约结果见回归记录；官方Home Assistant虚拟实体调用归档在历史v9证据中。

## 模型与路由

默认规则优先，无GPU或端点也可运行。复制编辑examples/routes.example.json后：

```powershell
python -X utf8 -B cli.py --home demo_v11 --routes examples/routes.example.json propose "打开阅读灯"
```

默认prefer_rules=true优先规则。显式设置allow_model_proposals=true后，规则之外的受支持即时句式也可送模型提案。严格模式无法独立验证的请求直接澄清。节点位置以部署者配置的端点为准。

路由复用TaskRouter的观测TTL、质量下限、探测时延、可选HTTP队列/可用显存/inflight、隐私许可和容量预留。质量校准文件绑定source SHA-256。示例分数来自历史开发集，低于默认0.8门槛；部署实际后端前应重新校准。

首次或过期探测只发送空目录健康检查，不含用户请求或设备信息。探测、成功和失败推理分别记录耗时、字节以及服务提供的token数。无可行节点或模型异常回退已验证规则；错误语义候选澄清。严格隐私只允许显式trusted_backends内的节点。

路由元数据绑定卡片摘要并能从持久回执恢复。route.deadline_met只覆盖规划（包括模型探测），不是端到端执行按期率；另记录两次状态读取耗时。响应handler_total_ms包含状态读取、建卡和审计，该响应计时在摘要之外。

## 异步与恢复

SQLite在发送前原子保存请求、完整步骤和实体预留。跨进程请求锁、owner和revision防止并发恢复覆盖新结果。默认GET轮询0.25秒、间隔0.02秒，SDK execution_options可设verify_timeout_seconds（0—30）与poll_interval_seconds；单次HTTP还受HAClient.timeout约束。

unknown只查证已派发步骤，不重发。查证成功后，仅原卡授权未过期、剩余设备预条件仍成立时续行从未派发的步骤；过期重新提案。已验证/未派发实体的安全预留可释放，真正未决派发实体保持排他。

平台状态成功不证明物理动作因果归属、恰好一次或多设备原子事务。本地SDK及平台凭据仍属于可信代码边界，没有面向公网的账户鉴权服务。

## 测试

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

覆盖语义变体、能力、并发与跨进程、硬退出恢复、授权过期、异步续行、原生HTTP契约、模型fixture和完整确认链。HTTP fixture监听127.0.0.1，用于检查接口契约。

## v11 修复与使用边界

- Controller和HA底层JSONL写入失败不再遮蔽业务结果，分别返回audit_status/platform_audit_status。持久执行回执可独立查询，JSONL是附属日志。
- 软件后端在SQLite写事务或内存锁内复查状态、版本、能力和授权期限，确认前变化返回未派发。
- 明确写出“不要联网/离线”等要求时，Controller在任何状态读取或模型调用前返回澄清；“不要上传”等要求跳过模型，只尝试本地规则。直接调用ModelPlanner/RoutedPlanner也执行同一拦截。
- HA单次HTTP的连接、响应头与分块正文共享绝对总时限。POST超时先独立读状态，不自动重发。不同实体不再由Controller长锁串行化，同一实体仍由持久预留和请求锁保护。
- 原生与虚拟HA适配器在读回时接收剩余验证预算。自定义客户端应实现get_state_with_timeout(entity_id, timeout)；未实现者依赖自身请求时限。verify_timeout_seconds=0保留立即读一次、不轮询的既有语义。
- 已支持语法按依赖范围读取，最多8个并发读取共享默认2秒预算；模型语义匹配可能需要整个候选目录，迟到读结果丢弃。
- 模型规划路径对连接、响应头和分块响应使用统一绝对期限，不占用确认锁。任意Python扩展或系统DNS任务无法安全强杀，会继续占用有限worker槽；容量满时请求会被拒绝。只读规划隔离器不执行设备动作。
- 模型规划、状态读取和平台执行分别计量期限，本系统不承诺端到端硬实时。

显式单端点语义提案：

```powershell
python -X utf8 -B cli.py --home demo_v11 --model-endpoint http://127.0.0.1:8000/v1/chat/completions --model-name YOUR_MODEL --allow-model-proposals propose "我想看书，帮我点亮适合阅读的那盏灯"
```

单端点模式使用命令行指定的端点；双端点路由需先完成质量校准。配置开关只接受JSON布尔值true/false。intent_review_required=true表示确认前需核对对象、动作和绝对数值，授权由用户完成。

## 华为云 MaaS 接入试做

`examples/huawei-maas.routes.example.json` 配置官方 MaaS OpenAI 兼容接口与 `openpangu-2.1-pro`；实际可用模型以本人的 MaaS 控制台为准。先私下设置当前进程的 `MAAS_API_KEY`，再运行：

```powershell
python -X utf8 -B cli.py --home demo_maas init
python -X utf8 -B cli.py --home demo_maas --routes examples/huawei-maas.routes.example.json propose "打开书房阅读灯"
```

该模板开启 `direct_model=true`：单后端、`prefer_rules=false`、显式模型提案、standard 隐私，且不能同时提供校准文件或资源探测接口；不声称质量已校准。直连预算为后端 `timeout_seconds` 和 `deadline_ms` 中较短者。普通 routes 未开启 `direct_model` 时保持原有校准路由行为；`--routes` 与 `--calibration-file` 不能混用，路由校准路径须写在 JSON 内。

MaaS 模板设置 `json_mode=false`、`seed=null`，省略尚未确认服务支持的可选字段。其他后端默认仍保留旧字段。`require_api_key=true` 会在缺少密钥时本地报错，防止鉴权错误被误看作规则成功。服务调用仍可能超时、拒绝或返回不符合要求的 JSON；检查输出中的 `metadata.route`，出现 `PLANNER_ERROR_RULE_FALLBACK` 时只代表规则回退。只有用户核对提案并明确确认后才会执行设备动作。

说明与验证范围见工作区 `docs/MaaS接入记录.md`。本机 HTTP 测试不是真实 MaaS 模型结果。

未知结果的可信本地人工处置：

```powershell
python -X utf8 -B cli.py --home demo_v11 resolve-unknown REQUEST_ID --operator "操作员姓名" --reason "已独立检查平台日志及设备状态" --disposition verified_settled --platform-settled
```

只有实际核实平台处理结束后才能作上述声明。仍不确定时应继续隔离；确需承担迟到风险接管时，使用accept_late_risk及--acknowledge-late-risk。处置记录持久保存，原请求保持unknown，不自动重发，也不取消远端迟到动作。新操作须重新propose并confirm。活跃执行不能处置；进程硬退出的非活跃中断请求可直接处置，无须先运行可能续行的reconcile。

## 语义覆盖评测

默认例集examples/semantic_cases.jsonl用于工程检查。实际评测应使用独立新任务，expected_actions由人工事先确定，不从被测模型输出生成。

```powershell
python -X utf8 -B evaluate_semantics.py --endpoint http://127.0.0.1:8000/v1/chat/completions --model YOUR_MODEL --source-label "实际模型和版本" --output evaluation.json
```

可同时提供--catalog及--states读取自己的目录和种子。工具比较严格规则、模型原始输出及受控模型提案，记录错误提案、额外正确动作案例、耗时、端点错误及提供的token和字节。全程只生成提案，不确认或调用设备服务。`source-label`填写实际模型及版本；端点名称本身不能验证模型身份。fixture用于检查接口和提案路径，模型比较需运行独立任务集。
