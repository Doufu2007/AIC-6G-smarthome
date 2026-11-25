# SenseHome 实施手册

`web_demo.py` 已提供中文网页、设备工具、确认接口、定时灯光、照护模拟和感知回放。AgentArts 客户端已接线，需在团队控制台创建并部署对应工作流。默认使用软件模拟设备。

## 1. 先跑通 AgentArts

先在 v11 目录验证现有软件底座。使用单独的演示数据目录，避免之前的操作状态影响验收。

```powershell
Set-Location -LiteralPath 'src/SenseHome-6G/v11'
python -X utf8 -B web_demo.py
```

访问 `http://127.0.0.1:8765/`，输入终端显示的本地操作员令牌。提交「把阅读灯调到40%，拉开书房窗帘」，在 30 秒内确认，查看两条读回。配置了 `SENSEHOME_OPERATOR_KEY` 时使用配置的令牌。

### 网关和网页

HTTP 服务复用 `controller.py` 的业务方法和 SQLite 回执。首次云端联调使用独立的软件演示实例，在受控 HTTPS 代理后运行。设置不同的 `SENSEHOME_OPERATOR_KEY` 和 `SENSEHOME_TOOL_KEY`，代理只向 AgentArts 提供所需工具路径。真实 Home Assistant 使用独立本机实例，配置 `--ha-url` 和 `SENSEHOME_HA_TOKEN`。在该实例启动环境中清除 `AGENTARTS_WORKFLOW_URL` 和 `AGENTARTS_RUNTIME_KEY`，并保持端口仅供本机访问。

| 建议路由 | 调用方 | 处理 |
| --- | --- | --- |
| `GET /healthz` | 部署探测 | 返回服务存活 |
| `GET /api/v1/devices?room=书房` | 网页、AgentArts | 返回该房间已授权设备的 ID、别名、能力和在线状态 |
| `GET /api/v1/devices/{id}/state` | 网页、AgentArts | 调 `Controller.snapshot([id])`，返回该实体状态和时间 |
| `POST /api/v1/proposals` | 网页、AgentArts | 网页传 `text`。工具传 `text`，正常网页云端链路还传 `context`，按需加 `candidate` |
| `POST /api/v1/confirm` | 已登录的网页用户 | 校验用户与提案归属后调用 `Controller.confirm(body)` |
| `GET /api/v1/receipts/{id}` | 已登录的网页用户 | 校验归属后用 `Controller.receipt(id)` 查询回执 |

AgentArts 的工具 Key 只允许查询与生成提案。网页由持有共享操作员密钥的人员使用，浏览器生成会话标识 `X-SenseHome-Session`，服务端以此绑定提案和回执。确认请求使用 `proposal_id`、`digest`、`request_id`、`confirmed: true`，由网页为一次确认生成一个请求 ID，重试使用同一 ID。v11 默认给提案 30 秒有效期，网页读取 `expires_at` 并显示倒计时。展示卡片和动作回执时，只渲染网关返回的结构化字段。

### AgentArts 控制台

1. 开通 AgentArts 后进入对应区域的控制台，在「开发中心 → 智能体管理 → 工作流」创建任务型工作流。首轮用「开始 → HTTP 请求 → 结束」。开始节点创建字符串 `query` 和 `context`。HTTP 节点配置网关的固定 HTTPS 地址和 `POST /api/v1/proposals`，在 `Authorization` 请求头配置 `Bearer <SENSEHOME_TOOL_KEY>`。将 `text` 和 `context` 分别绑定开始节点的 `query` 和 `context`，请求体为 `{ "text": {{text}}, "context": {{context}} }`。结束节点将输出 `proposal` 绑定到 HTTP `body`。控制台试运行填原句和空字符串 `context`，这类试运行卡只能用于验证工具返回。
2. 加入意图识别或大模型节点和分支。状态查询走设备列表和状态工具。控制分支先定位房间和设备，再输出候选并调用提案接口。照护分支通过只读工具查询指定事件和处置流程。家庭知识库只放设备说明、可用场景和照护流程，实时状态走工具。先用一个工作流验证分支，工具被多个工作流复用时可以再封装 MCP。
3. 模型输出包含 `result_kind`、`actions` 和 `reason`。动作字段为 `entity_id`、`service`、`data`。在创建提案的 HTTP 节点将模型输出作为 JSON 对象加入 `candidate`。`Controller.propose_candidate` 以原句校验候选，复用依赖推导、设备能力检查、状态快照和持久化确认。书房阅读场景由本地已知动作生成候选，缺少房间时返回追问。默认提示词可要求只使用工具返回的实体，且保留否定、条件和时间约束。
4. 提交版本并部署至实例。设置 `AGENTARTS_WORKFLOW_URL`、`AGENTARTS_RUNTIME_KEY` 后重启网页进程。如果运行时响应在 `outputs.proposal` 内，设置 `AGENTARTS_RESULT_PATH=outputs.proposal`。默认取顶层 `proposal`。正式 API 需配置可用模型，例如 MaaS 付费模型或自接兼容模型。网页后端发起调用并生成短期 `context`，工作流原样回传，服务端据此绑定同一网页会话和同一次请求。确认接口使用操作员凭据。

```powershell
# 配置环境变量 AGENTARTS_WORKFLOW_URL 与 AGENTARTS_RUNTIME_KEY 后执行。
$headers = @{
    Authorization = "Bearer $env:AGENTARTS_RUNTIME_KEY"
    'x-hw-agentarts-session-id' = [guid]::NewGuid().ToString()
}
$body = @{ inputs = @{ query = '打开书房阅读灯'; context = '' } } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri $env:AGENTARTS_WORKFLOW_URL -Method Post -Headers $headers `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

验收时在演示环境从关闭的灯开始。输入「把阅读灯调到40%，拉开书房窗帘」，确认前两项状态应保持原样，确认后两步 `verified`，再次提交同一请求 ID 返回原回执。再跑「我想看书，帮我点亮适合阅读的那盏灯」做语义对照，以及否定、设备离线、提案过期三类用例。首次联调保留 AgentArts 工作流、工具 Trace 和 v11 读回回执。公开 Demo 的云端入口只使用该独立演示环境。

家庭实物保持在本地网关。云端工作流使用独立软件设备演示实例。离线和禁止上云的请求由本地入口判定。网关的工具路由独立调用 v11，避免重复进入云端规划。

[创建工作流](https://support.huaweicloud.com/lowcode-agentarts/agentarts_05_0047.html) · [HTTP 请求节点](https://support.huaweicloud.com/lowcode-agentarts/http_node.html) · [结束节点](https://support.huaweicloud.com/lowcode-agentarts/end_node.html) · [发布并部署](https://support.huaweicloud.com/lowcode-agentarts/agentarts_05_0053.html) · [API 调用](https://support.huaweicloud.com/bestpractice-agentarts/agentarts_06_0022.html)

## 2. 扩展到全屋

1. 安装 Home Assistant，在设置里创建书房、卧室、阳台、门口等区域。按具体设备型号加原生、Matter、Zigbee、MQTT 或 ONVIF 集成，再给设备分配区域和好理解的名称。先在 HA 界面独立验证设备在线、状态变化和支持的动作。普通家具可登记位置，接了传感器或执行器后再补状态或控制能力。
2. v11 可发现灯、窗帘、开关和空调。用 `--ha-url` 启动网页服务时自动发现，也可把审阅后的 `catalog` 数组以 UTF-8 保存到运行目录的 `catalog.json`，使用指定设备。可在运行目录放 `rooms.json`，例如 `{"light.study":"书房","camera.entry":{"room":"门口","room_aliases":["玄关"]}}`，补充房间和房间别名。`SENSEHOME_HA_TOKEN` 由启动服务的环境提供。

```powershell
# 将示例地址换成实际的 Home Assistant 局域网地址。
$senseHomeHaUrl = 'http://192.168.1.10:8123'
python -X utf8 -B cli.py --home ha_test --ha-url $senseHomeHaUrl --ha-native discover
python -X utf8 -B web_demo.py --home ha_test --ha-url $senseHomeHaUrl --port 8766
```

3. 网页按房间查询设备目录。门磁、温湿度、人体存在和独立门铃事件实体可以读取，摄像头仅返回在线状态。当前使用 REST 读取状态。高频事件接入可在后续通过 HA `state_changed` 订阅实现。摄像头按型号接 ONVIF 或厂商集成，视频由授权用户在 HA 侧查看。
4. 阳台插座支持开关，客厅空调支持在制冷或制热模式下设定绝对摄氏温度。适配器检查设定范围和步长，执行后核验设定值及模式。可试用「打开阳台插座」和「把客厅空调调到24.5摄氏度」。扩展新设备类型时需同时定义能力、提案约束和读回判据。
5. 网页已支持「10 秒后打开阅读灯」一类持久化一次性任务。用户确认后才开始计时，可以查询和取消。到期时验证设备配置、能力与状态，错过执行窗口时任务过期。由 HA 事件触发的通用条件自动化仍需开发。

网页区分 `needs_clarification`、`device_not_found`、`device_offline`、`unsupported_action` 和 `model_unavailable` 等结果。执行状态不确定时显示原回执中的 `unknown`。控制结果以网关读回为准。

[HA 集成目录](https://www.home-assistant.io/integrations/) · [HA WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) · [HA 自动化触发器](https://www.home-assistant.io/docs/automation/trigger/)

## 3. 独立实现养老求助模拟

可以在无粤安芯 API 的情况下实现自己的求助系统。命名为「SenseHome 照护模拟」，以公开可见的业务流程为参考。页面和回执标明模拟来源。企业接入时再增加遵循其正式协议的适配器。

| 路由 | 处理 |
| --- | --- |
| `POST /api/v1/care/events` | 接收 `event_id` 和可选 `area_id`。服务写入 `type=sos`、`source=simulated`，以事件 ID 去重 |
| `GET /api/v1/care/incidents/{id}` | 返回事件、来源、时间线、通知投递状态和确认结果 |
| `POST /api/v1/care/incidents/{id}/ack` | 已授权的测试联系人确认 |

流程为 `新事件 → 通知待确认 → 已确认或超时升级`。SQLite 保存事件和到期时间。确认与升级采用同一事件状态的原子更新。网页显示演示收件箱投递与测试家属确认两类记录。重复 SOS 只产生一个事件，正常确认停止升级，无确认只升级一次，服务重启后继续处理待确认事件。

基础求助由本地规则触发，AgentArts 工作流可以通过只读接口获取指定事件以生成摘要。当前通知全部写入演示收件箱。实物 SOS 按钮和感知复核可以在后续接入。

[粤安芯公开产品介绍](https://yaxgroup.com/)

## 4. 加入 CSI、WM 和 RSI

附件描述的是一条机器人研究路线。RSI 是递归自我改进。WM 是结合 CSI、RGB-D、本体状态与动作的多模态世界模型，可预测动作后的环境与观测；其预测还能用于主动查证和反事实规划。以下用智能窗帘把路线迁移到家居场景。文中的 ManiSkill 与 Sionna RT 方案可作为单独的机器人研究线。

### CSI 实采

网页默认展示合成数据。用 `python -X utf8 -B web_demo.py --csi-csv 'C:\captures\window.csv'` 可把本地 ESP-CSI CSV 的一段观测加入同页，回放结果会显示独立来源。窗帘预测和候选版本演示仍使用合成样本。数据要求见 [离线试验说明](../src/SenseHome-6G/v11/SENSING_TRIAL.md)。

1. 准备两块支持 CSI 的 ESP32 开发板和两根数据线，例如按官方入门示例选 C5/C6。用 ESP-IDF 分别构建和烧录 ESP-CSI 的 `csi_send` 与 `csi_recv`。选固定的同一频道和板位，串口先确认接收端连续输出 CSI。关闭 `idf.py monitor` 后再用官方串口解析脚本读取 Windows `COMx`。型号会影响 CSV 字段，先按官方样例检查记录完整性。
2. 在书房固定收发板。同步采集 CSI 时间、序号、频道、RSSI 和子载波 IQ，另外记录窗帘动作、视觉是否遮挡、独立行程或位置真值。按轮次采开、关、保持与正常中途停止。固定并记录板位，另留一批换位数据测试迁移。单房间先确认 CSI 是否能区分运动与静止，再尝试判断开合到位。
3. 在本地对滑动窗口提取幅度变化和丢包率等特征，先训练简单基线。软件端可以先通过回放联调事件，页面标明 `replay`。实采后输出 `area_id`、`event_type`、`observed_at`、`sensor_id`、`confidence` 和 `signal_quality`，再进入设备状态或照护复核支路。原始 IQ 留在本地。传感器未上报时记录缺测，和感知到静止分开。

ESP32-C6 的烧录命令在 ESP-IDF 终端运行。在 `examples/get-started/csi_send` 和 `examples/get-started/csi_recv` 各执行一次，将端口改成对应开发板的实际串口。采集端先在串口监视器确认有 CSI 记录，再关闭监视器启动解析脚本。

```powershell
idf.py set-target esp32c6
idf.py -p COM5 flash monitor
```

[Espressif ESP-CSI 入门例程](https://github.com/espressif/esp-csi/tree/master/examples/get-started) · [ESP-CSI 中文说明](https://github.com/espressif/esp-csi/blob/master/README_cn.md)

### 动作条件世界模型

先选一个可实测的问题。书房视觉被遮挡时，执行开合动作后能否更准确判断窗帘是否到位。输入用历史状态、可用的视觉观测、动作与 CSI，输出未来状态、预期观测和不确定度。独立行程反馈充当标签。比较持久状态或命令规则、历史状态与视觉加动作、同架构再加 CSI。所有方法使用相同留出轮次。通过正常中途停止或独立改位让相同指令可能产生不同结果，避免命令本身泄露结果。

在此基础上再做主动查证。预测低置信度时，只读取一种新的安全观测，如行程反馈或新一段 CSI，比较按模型选择查证与固定查询策略，在同预算下记录正确率和查询开销。规则式设备状态表负责聚合事实，训练出的动作后果预测通过上述对照验证后再作为世界模型成果报告。

### 递归自我改进

按「漂移检测 → 选择补采情形与待更新模块 → 离线训练候选 → 冻结回归验证 → 版本启用或回滚」实现第一轮。初期只允许更新 CSI 特征阈值或感知模型，不修改设备授权、执行策略或应急通知规则。随后用多轮反馈调整写明的补采或更新策略。和冻结模型、固定周期更新做相同预算对照，并记录性能退化、补采成本和回滚。完整 RSI 的证据包括更新策略本身在多轮迭代中得到改善，单次重新训练可作为受约束自适应原型展示。

## 5. 实施顺序和验收

| 批次 | 完成条件 |
| --- | --- |
| AgentArts 基线 | 云端工作流真实调用软件演示网关，网页确认后能查看 v11 回执和状态。否定、过期、同 ID 重试有预期结果 |
| 全屋与照护 | HA 至少一个真实区域有可独立核查的状态与控制，照护模拟的正常确认与超时升级均跑通，网页区分实物和模拟 |
| CSI | 串口实采和独立真值均保存，回放与实时来源可区分，状态缺失和低置信度可展示 |
| WM 与 RSI | 遮挡留出集的对照可复跑，候选更新有版本和回滚记录，按实际证据描述能力 |

本地已验证网页操作、定时执行、模拟求助和感知报告。正式云端验收需要团队 AgentArts 账户中部署的工作流、运行时地址、凭据和可达的演示网关。真实设备和 CSI 实采还需要接入各自硬件。初赛录屏应串起工作流运行、网页交互和执行回执。
