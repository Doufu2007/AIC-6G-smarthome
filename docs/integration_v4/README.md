# v4 正式网关持久回执与本机 MQTT 软件实体

这条链路通过原 `OffloadGateway`、Dashboard 原动作白名单与真实 MQTT 协议，把命令写入独立软件实体。它验证软件状态提交、回执绑定和故障恢复，不构成物理设备完成、WAN 或 6G 实测。

## 启用方式

可选依赖的精确版本在 `requirements-lock.txt`。本次运行使用 Windows Python 3.13.7、AMQTT 0.12.1、paho-mqtt 2.2.0。

```powershell
python -m pip install -r docs/integration_v4/requirements-lock.txt
python -X utf8 docs/integration_v4/run_integration.py
```

`run_integration.py` 每次建立新的 `runs/<UTC>_<随机ID>/`，不会覆盖已有目录。程序只绑定 `127.0.0.1`，服务端口由系统分配，MQTT broker 与软件实体运行在两个自有子进程。`process_launches.jsonl` 保存完整 argv/PID，`cleanup.json` 保存退出码；只停止本次创建的进程。

其他入口可复用以下 context manager。`initial_state` 需要与隔离 Dashboard 的房间清单一致，包含 `lights`、`curtains`、`temperature`。

```python
sys.path.insert(0, str(ROOT / 'SenseHome-6G/net_offload'))
from mqtt_receipts import LocalMQTTSession

with LocalMQTTSession(new_run_dir / 'mqtt', initial_state=initial_state) as session:
    # 在初始化 bridge/LiveSession 之前设置，退出时由调用方恢复环境。
    os.environ.update(session.environment)
    # 创建并使用隔离 bridge；结束时先 bridge.close_gateway()。
```

`session.environment` 给出 `SENSEHOME_RECEIPT_DB`、`SENSEHOME_MQTT_HOST`、`SENSEHOME_MQTT_PORT`、`SENSEHOME_MQTT_PLATFORM`、`SENSEHOME_MQTT_LOG`。可选 `SENSEHOME_MQTT_TIMEOUT_S` 默认 2 秒。未设置 `SENSEHOME_RECEIPT_DB` 时沿用原配置；开启后连接失败会关闭网关，不会默默降级为本机文件执行。`session.restart_entity()` 只重启本 context 拥有的软件实体并保留其数据库。

请求可在 `/api/command` 的 `context.command_id` 提供最多 64 字符的稳定 ID；未提供时生成新 ID。回查使用 `GET /api/receipts/<command_id>`。命令 ID 相同但原指令、动作或平台不同会返回 `COMMAND_ID_CONFLICT`。同一合法 ID 的历史回执不会触发新的动作提交。

## 保证边界

- 网关 SQLite 先用事务持久记录 `unknown`，之后最多进行一次应用层发送；数据库绑定 ID、原始指令、平台和完整动作的 SHA256。
- 未知状态、ACK 丢失及重启后只查询，查询不到仍为未知。MQTT 命令采用 QoS 0 并关闭自动重连；适配器不自动重发应用命令。它优先避免重复执行，代价是部分未送达命令会保持未知。
- 原 `execute_device_actions` 在发送前校验整个动作批次；实体也再次用同一白名单校验。软件状态和实体回执在同一个 SQLite 事务提交。
- 回执验证命令、平台、哈希、动作、完成索引、版本和成功/失败字段的一致性。未知、部分、拒绝及执行异常不会触发规则回退重做。
- 历史回执只证明历史提交。页面单独查询当前软件平台快照并校验平台、版本、结构和值；不会把旧回执动作重新应用。快照不作为某个未知命令成功的证据。
- 开启持久 MQTT 模式时，旧自动巡检与直接写状态的离家场景明确停用，防止绕过白名单和回执；明确灯光/窗帘/温度指令及模型合格动作走正式适配器。
- 本机 loopback、匿名 broker、无 TLS、无硬件反馈、无电力中断试验。SQLite 提供当前本机软件进程重启持久性；没有生产部署认证、物理 exactly-once 或跨机器身份认证声明。

## 结果与复核

最终目录由 `LATEST.json` 指定。每个成功目录有 `summary.json`（`sensehome.integration-v4.summary.v1`）、`versions.json`、`source_manifest.json` 和 `offline_audit.json`。六类场景为重复、异载荷冲突、提交后丢 ACK、网关与实体重启、部分执行、绑定矛盾。部分执行使用明确标注的固定双动作协议探针，不作为真实模型推理证据。绑定矛盾是在真实 MQTT 回执到达边界后注入平台 ID 矛盾，保留原件和注入件。

`normal`、`ack_loss`、`partial` 的 `http_results.json` 与 `offload_events.jsonl` 是正式 HTTP 网关原始结果；每个场景下的 `mqtt/` 保存双方消息、双数据库、初始状态、进程和清理日志。`normal` 另测一条较新的关灯命令之后再回查旧开灯回执，当前镜像及软件实体都保持关灯。

```powershell
python -X utf8 docs/integration_v4/verify_integration.py <新的运行目录> --output <新的运行目录>/offline_audit.json
python -X utf8 -m unittest discover -s SenseHome-6G/net_offload/tests
python -X utf8 -m unittest discover -s openvela_smart_home/dashboard/tests
```

离线核验器独立读取原始 MQTT JSONL 与两份 SQLite，重算请求哈希、每 ID 发送次数、实体提交版本、网关状态和自有进程退出码，不依赖 summary 的 PASS 字样。`verification/` 保存红灯和最终回归记录。根目录裸 `pytest -q` 也实际运行过，因工作区历史解压副本测试重名及 docs 测试导入冲突发生 36 项收集错误，详见 `verification/project_pytest.txt`；因此不能称整个混合工作区 pytest 全绿。

第一轮 `20261111T063352Z_099fc803` 在部分执行探针的 policy 注入处失败，原始目录保留；之后的中间成功目录也保留，不冒充最终源码运行。没有重写 v1—v3 封存，没有提交或推送。

## 新目录和离线安装

复制项目到新目录后，以 `ROOT` 相对路径和 `sys.executable` 启动；证据里的绝对 argv 是运行时实录，重放时重新生成。Python 与可选包不是项目源码的一部分。若目标电脑没有网络，先在相同 Windows/Python 架构上下载依赖，再带 wheel 目录过去：

```powershell
python -m pip download -r docs/integration_v4/requirements-lock.txt --dest <wheel目录>
python -m pip install --no-index --find-links <wheel目录> -r docs/integration_v4/requirements-lock.txt
```

依赖下载/安装是环境准备，不计为真人独立部署或真实外部用户反馈。
