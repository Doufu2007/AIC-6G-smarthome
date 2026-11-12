# MQTT 生命周期与持久数据路径修复

范围：F06、F10 的 MQTT 底层接口，以及 F07 的实体采样时间。初轮连接修复未修改 `durable_receipts.py`；独立服务审查发现执行等锁后的 deadline 缺口后，经主控追加授权修改该文件与 MQTT 发布边界，见下文补修。未操作原设备服务、云模型或封版结果；验证仅使用临时目录和本轮自己创建的 loopback broker/entity 进程。

## 根因与修改

旧适配器设置 `reconnect_on_failure=False`，broker 停止后网络循环不能恢复；订阅就绪事件只设置、不清除，断线后仍显示 ready。关闭也没有释放等待回执的线程。

`MQTTReceiptTransport` 现在允许 paho 恢复连接，每次成功连接均重新订阅独有回复主题；只在对应 SUBACK 成功后设置 ready。断线清除 ready 和订阅 MID，并释放当前等待者。`close()` 先在同一把锁内关闭接入、清除连接状态和释放等待者，再停止网络循环，且可重复调用。发布接入与关闭/断线回调共享锁，等待回执时不持锁。

`connection_state()` 返回不做网络 I/O 的缓存快照：`connected`、`subscribed`、`closed`、连接 `generation` 和最后连接/断线墙钟时间。实体 `snapshot()` 在读出 SQLite 当前状态时记录 `sampled_at`；这表示软件实体采样时间，不表示物理设备采样。

请求仍为 QoS 0、retain=false。连接未就绪时直接返回未知，不等待恢复后补发，也不使用 QoS 1/2 的自动重传队列。发布只等待匹配回复，去掉单独的 publish 等待，关闭和断线可立即释放回执等待。已有 `SQLiteReceiptExecutor` 的“先持久保存 unknown、只首次 submit、之后仅 lookup”边界保持不变。因此连接恢复不等于命令重新发送。

## 数据路径接口

新增 `LocalMQTTSession(run_dir, ..., data_dir=None)`，entity 子进程通过 `--data-directory` 接收数据库目录。

| 文件 | 所在目录 |
| --- | --- |
| `entity.sqlite3`、`gateway.sqlite3` 及 SQLite 辅助文件 | 显式 `data_dir`；缺省时仍为 `run_dir` |
| `initial_state.json`、ready/stop markers、MQTT 事件日志、进程日志、`processes.json`、`cleanup.json` | `run_dir` |

`environment['SENSEHOME_RECEIPT_DB']` 指向持久数据库，`SENSEHOME_MQTT_LOG` 指向本次运行日志。实体仍使用 `INSERT OR IGNORE` 初始化：旧实体状态、版本和回执不被后续启动的初态覆盖。`run_dir` 保持原有 fresh-marker 检查。部署级目录锁由 `IntegratedSession` 持有，底层 `LocalMQTTSession` 本身不额外持锁。

## 测试证据

新增 `SenseHome-6G/net_offload/tests/test_mqtt_lifecycle.py`。

- RED：最初 5 项运行得到 3 个断言失败、1 个缺少 `data_dir` 参数错误、1 个原并发查询通过。失败分别为旧 transport 在 broker 真正停启后始终返回 None、断线仍 ready、close 后等待线程仍活跃、构造器不接受持久目录。
- GREEN：最终 MQTT 集合 8/8（7 项新增 + 1 项原协议测试）。包含真实 broker 停启后同一个 transport 恢复查询与新提交、丢失 ACK 的原 unknown 命令仅回查、离线提交不排队、双查询相关 ID 不串包、关闭唤醒、跨新 run 目录加载旧数据库，以及实体采样时间。
- QoS 0 队列边界：测试暂停真实 paho 网络 writer，确认 submit 已进入其待发送包队列，再停止 broker；恢复循环并重启 broker 后实体版本仍为 0、实体无 submit 事件、网关 unknown 只能 lookup。此项验证实际 paho 行为，没有用模拟 transport 替代。
- 持久数据测试：第一轮执行后版本为 1、灯光为 60%；第二个运行目录传入不同初态后，仍恢复版本 1/60%，本次日志没有 submit，两个数据库仅存在于 data_dir。
- 单独实体采样时间 RED：旧快照缺 `sampled_at` 失败；实现后真实请求前后的墙钟区间包含实体返回时间，1/1 通过。
- 原 `test_durable_receipts.py`：7/7 通过，覆盖 ACK 丢失、重复 ID、进程中断、部分结果、冲突和无绑定回查。
- 过程中运行原 `*receipt*.py` 集合 10 项：9 过，`test_receipt_gateway_boundary.ReceiptGatewayTests.test_unknown_receipt_is_terminal_and_uses_original_command_binding` 由于模糊请求提前澄清导致 `contexts=[]`、抛出 `IndexError`。已向主控报告，由语言/网关工作区统一调整旧用例；本报告不将这次运行写成全通过。最终项目级集合由主控集成验证。

最终 MQTT 日志：`_work/intrinsic_fixes/mqtt_verification.txt`。复现命令：

```powershell
python -m unittest discover -s SenseHome-6G/net_offload/tests -p 'test_mqtt*.py' -v
python -m unittest discover -s SenseHome-6G/net_offload/tests -p test_durable_receipts.py -v
```

环境：Python 3.13、paho-mqtt 2.2.0、AMQTT 0.12.1。持久部署工作区对本文件的生命周期锁、发布接入、关闭和订阅路径进行了独立交叉审阅，未提出实质问题。

## 边界

恢复范围是 `127.0.0.1` 上的 MQTT 软件实体，不证明物理执行、实际 6G 链路或远程 broker 的可靠性。连接恢复存在一秒重试间隔；恢复前调用保守返回未知，不承诺等待网络恢复。回复订阅恢复不证明实体请求订阅已恢复，恢复瞬间的新动作仍可能保持 unknown，后续仅回查。

本次确认已提交/未发送命令不会因连接恢复而重放。它不将 QoS 0 变成可靠投递，也不把持久 unknown 自动视为失败或已完成。依赖升级后应重新运行未发送 QoS 0 包的队列边界测试。缓存新鲜度的失效计算由上层使用本机接收时的 monotonic 时间完成，`sampled_at` 用于显示来源，不作为跨时钟有效期依据。

原 MQTT 源字节 SHA256：`DDA6598FDEC0768981D61EE0CF5ADAA87D815ADC1F4541FE6A3AB5E7A1E50FBA`。主控备份位于 `_work/intrinsic_fix/before/SenseHome-6G/net_offload/mqtt_receipts.py`，本工作区另留 `_work/intrinsic_fixes/mqtt_before/mqtt_receipts.py`。

## 独立审查后的 deadline 补修

审查发现 bridge 执行入口检查期限后，SQLite reservation 或 MQTT publisher 锁仍可能等待至期限之后。主控明确授权补修：`SQLiteReceiptExecutor` 在首次 submit 前复检可信 `_deadline_monotonic`，优先调用 transport 的可选 `submit_before(payload, deadline)`；真实 MQTT 在获得发布锁后与实际 publish 前再次检查。错误保留为 `DEADLINE_EXCEEDED_BEFORE_SUBMISSION`，持久 unknown 记录不被重新发送，原 payload 合同和旧一参 stub 接口保持兼容。

新增 `test_receipt_deadlines.py` 两项先失败再通过：数据库等锁过期后 0 次发送、真实 MQTT 发布锁等候过期后 0 次发送；及时的新 ID 仍 applied。最终相关 `*receipt*.py` 12/12、MQTT 生命周期 7/7 通过；新增 2 项在 pytest `--import-mode=importlib` 下也通过。先前报告中的旧网关边界测试错误已在这次 12 项集合复验中通过。完整审查证据见 `docs/intrinsic_v5/service_review.md`。
