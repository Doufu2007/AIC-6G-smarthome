# 设备适配合同与回执 fixture

这是独立、可复现的 **HTTP JSON 软件设备合同**，用于提前检验真实设备适配时的命令标识、幂等、并发版本、回执和未知状态处理。没有接入旧网关或物理设备，**不是 Matter 协议实现或认证，也不是 Home Assistant、openvela 实物兼容证明**。初始温湿度是固定测试数据，不是传感器读数。

实现只使用 Python 标准库，并复用项目既有 `device_actions.execute_device_actions` 白名单；没有改动旧 C/Python 源码。JSON Schema 在 `device_contract.schema.json`。测试使用当前环境已有的 `jsonschema` 验证 schema，运行服务本身不需要它。

## 可验证的行为

- `command_id` 与规范 JSON payload 的 SHA256 绑定。同一 ID、同一 payload 返回原回执，状态只提交一次；对象键顺序不影响指纹。同一 ID、不同 payload 返回 HTTP 409，保留原回执。
- 用锁将 ID 查询、状态版本校验、候选状态校验和提交串成一个事务；16 个并发重复命令只提交一次。
- `expected_state_version` 防止基于过期状态覆盖新状态。新命令版本不符返回 409，版本不增长。已存在的重复命令先返回原回执，不会因版本已经增长而再次执行。
- 整批动作先在私有深拷贝上经过既有白名单，全部有效才交换 fixture 状态。合法第一动作后接非法第二动作时返回 422，零提交、无部分状态变化。燃气、门锁、报警关闭、shell 等均不在支持清单。
- 回执含命令 ID、payload 哈希、store epoch、前后版本、创建时间、动作观察和 `fixture_commit_count`。拒绝也有可查询回执，不能悄悄换 payload 重用 ID。
- 客户端每次 `submit` 只发 **1 次 POST**。POST 超时、5xx 或回执不完整/矛盾时最多发 1 次 GET 回查。GET 暂未查到、网络仍不可达或来源/哈希/版本不符时保持 `unknown`，不能改写为失败或成功；没有自动 POST 重试。
- `submit` 及其自动GET回查都绑定原请求的 ID、payload 哈希、`expected_state_version`、动作数量和逐项 `name/params/顺序`。只有与本次原请求一致的完整软件回执才能得到 `applied`；回执自报“版本99→100”或“空动作已提交”不能冒充本次执行。
- POST 已提交但 ACK 延迟：GET 查到回执可以确认 fixture 已提交。POST 尚在等待处理：首次 GET 可能404，此时仍未知；稍后显式 GET 可确认。测试同时覆盖这两种时序。

`fixture_commit_count` 和 `applied_commands` 仅计软件状态提交，**不代表物理执行次数**。成功的新批次使状态版本增长1，即使是只读或重复设置相同值；它是事务序号。`state_age_ms` 是本进程最近一次 fixture 提交以来的时间，不是设备遥测新鲜度。

## HTTP 合同

服务只监听 `127.0.0.1`。客户端只接受 literal `127.0.0.1` 或 `[::1]` 的 HTTP URL，直接使用不跟随重定向的 `http.client`，不读取环境代理。

| 接口 | 含义 | 关键返回 |
|---|---|---|
| `GET /health` | 当前软件 fixture 存活及 store epoch | 200；明确 evidence_kind |
| `GET /state` | 软件状态和当前版本 | 200；state_version、applied_commands、state_age_ms |
| `POST /commands` | 提交一个有固定 ID 的动作批次 | 200 applied；400 请求格式错误；409 ID/版本冲突；422 动作不在合同内 |
| `GET /commands/<command_id>` | 只读查询回执 | 200 返回原回执；404 是 unknown，不能据此判断之前未执行 |

请求只接受下面三个顶层字段，动作上限16个。房间必须存在于实例初始状态中，不能仅靠 schema 判断授权。

```json
{
  "command_id": "receipt-demo-001",
  "expected_state_version": 0,
  "actions": [
    {"name": "light_control", "params": {"location": "客厅", "brightness": 60}}
  ]
}
```

支持 `light_control`、`curtain_control`、`read_temperature`。亮度/位置必须是0—100的整数，布尔值不能冒充整数。服务读取的 JSON body 上限64 KiB。非法请求格式不会预留 ID；动作422拒绝和状态版本409拒绝会预留 ID，之后同一 ID 更换 payload 仍会冲突。

## 复现

在项目根目录运行：

```powershell
python -m unittest discover -s docs/demo_v3/device_receipts -p test_device_contract.py -v
python docs/demo_v3/device_receipts/run_demo.py --out docs/demo_v3/device_receipts/verification/http_demo_new.json
```

`run_demo.py` 启动临时 loopback HTTP 服务，实际发送成功、重复、冲突、批次非法和超时后回查请求，验证不变量并保存原请求、响应、HTTP方法轨迹与前后状态。必须使用新的输出文件名，避免覆盖旧证据。修复审查问题后的实跑结果见 `verification/http_demo_reviewed.json`，其中 `physical_device_count=0`、`matter_stack_present=false`。早期演示 `verification/http_demo.json` 保留作历史记录，未覆盖成新代码结果。

需要单独启动服务时：

```powershell
python docs/demo_v3/device_receipts/device_contract.py --port 0
```

第一行输出包含随机空闲端口、evidence_kind、store_id。可用 Python 导入 `DeviceContractClient(url).submit(payload)`。只知道 ID 时，`lookup(command_id)` 仅返回 `reported_applied/reported_rejected`（`confirmation_scope=reported_receipt`），表示服务自报回执；有原请求时用 `lookup(command_id, expected_payload=payload)` 才进行请求绑定确认。`--response-delay-ms 150` 只是在软件提交后延迟 ACK 的故障注入，不能拿它当真实网络或设备时延。

## 验收记录

2026-11-11：首次17项行为测试 RED（尚无实现），保存于 `verification/red.txt`。自审新增不完整/矛盾回执测试，4个子例 RED，保存于 `verification/receipt_validation_red.txt`；收紧确认条件后修复。Schema根入口原先仅有 `$defs`，直接加载时不约束输入；补充根入口测试先得到RED（`verification/schema_root_red.txt`），再增加 `oneOf` 合同入口修复。

独立审查发现回执虽检查版本递增，但未与本次请求的旧版本/动作绑定，可能把“99→100”或空动作误确认为本次提交。新增4子例RED（`verification/review_binding_red.txt`）后修复；同步收紧 schema，禁止空动作applied回执（`verification/schema_empty_applied_red.txt`）。修复后 **19项测试全部通过**，完整输出见 `verification/review_final_clean.txt`。独立审查另行复验19项通过（9.655秒），并原样重放两条真实HTTP攻击性fixture：现在均为unknown，且恰好1POST+1GET，无动作重发。此前18项及各修复阶段日志保留作历史。

真实 loopback HTTP 测试覆盖幂等/冲突、非法批次零提交、提交后 ACK 超时回查成功、提交前暂未查到保持unknown，以及不完整/矛盾回执不冒充成功。`run_demo.py` 另一次实际演示的不变量检查全部通过。以上全部属于软件合同验证，不计入真实家庭试用、物理设备验证或模型准确率。

## 接真实设备前仍需完成

存储目前只在内存中，`store_id` 随进程重启变化。**幂等保证仅限同一 store epoch，未实现重启后的持久幂等。** GET404可能表示未处理、尚未完成、重启后记录消失，始终保留unknown。真实接入前需要可靠持久化 outbox/回执与设备 ID 绑定，并在重启/超时后对账。

当前客户端没有可信预置的store epoch或身份认证，回执的store_id是服务自报字段，因此明确输出 `epoch_verified=false`。`request_bound_receipt` 只表示回执内容与原请求相符，不能推出跨epoch一致性、服务身份可信或物理动作完成。真实设备适配还需绑定可信设备身份/epoch并验证ACK或遥测。

建议接线为：已有网关计划与白名单 → 持久 command_id/请求哈希/待确认记录 → 授权设备适配器 → 真实设备 ACK 或可核对遥测 → 校验 ID/epoch/版本后的执行确认。状态未知时停止自动动作重发；由设备支持的去重协议或人工对账决定后续操作。

本 fixture 的“批次失败零提交”依赖内存候选状态原子交换，**不能推出多个物理执行器具备原子回滚**。真实适配必须分别记录已执行、未执行与未知步骤，验证掉电、网络断连、设备 ACK 丢失、部分执行和联锁。还需真实协议映射、设备授权、TLS/鉴权、持久化、速率/容量控制、遥测新鲜度、传感器交叉确认以及长期稳定性测试。当前没有认证、TLS、物理联锁或 Matter 支持；仅用于本机软件合同开发。
