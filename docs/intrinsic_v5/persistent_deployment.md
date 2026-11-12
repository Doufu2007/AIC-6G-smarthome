# 稳定数据目录部署

从项目根目录启动，不传 `--data-dir` 时仍是每次全新状态的隔离演示。要在整体退出后恢复同一套数据，使用固定目录：

```powershell
python -X utf8 docs/demo_v4/live_v4.py --rules-only --data-dir "_work/deployment/home"
```

退出后使用相同命令再次启动。需要真实模型时去掉 `--rules-only`，模型路径沿用 `docs/demo_v3/local_models.json`。`--json-only` 使用独立的 JSON 仿真模式；MQTT 和 JSON 模式不得共用同一数据目录。

| 路径 | 保存内容 |
|---|---|
| `<data-dir>/schema.json` | 数据版本、执行模式、软件平台 ID |
| `<data-dir>/.instance.lock` | 应用持有的操作系统独占锁 |
| `<data-dir>/mqtt/entity.sqlite3` | MQTT 软件实体当前状态、版本及实体命令记录 |
| `<data-dir>/mqtt/gateway.sqlite3` | 绑定完整动作请求的命令回执、applied/partial/rejected/unknown 状态 |
| `<data-dir>/bridge/device_state.json` | 网关状态及最近观测缓存 |
| `<data-dir>/bridge/command_requests.sqlite3` | 规划前绑定原始请求的请求账本 |

运行标记、子进程日志、事件和运行清单仍分别放在每次新建的 `_work/demo_v4/<run>/` 与 `_work/demo_v3/<run>/`，不会复用旧的 ready/stop 文件。启动输出和 `integrated_manifest.json` 的 `persistence` 记录实际数据目录、是否恢复已有目录，以及 MQTT 启动快照对账结果。

持久 MQTT 模式启动时先读取当前软件实体快照，更新网关缓存后才就绪。旧命令回执只能证明历史结果，不能作为当前状态；`unknown` 命令保留并只允许查询，启动不会自动重新提交。当前实体快照无法取得时，持久 MQTT 启动失败并清理本次拥有的进程。

第二个进程使用正在运行的数据目录会收到 `DATA_DIRECTORY_ALREADY_IN_USE`。正常退出或进程终止后，操作系统释放锁；遗留 `.instance.lock` 文件无需删除，也不会单独阻止重启。不要在应用运行期间删除、替换或搬走这个文件。

这是首个持久部署格式，schema 为 `sensehome.integrated-data.v1`、`version: 1`。空目录会初始化；缺少 schema 的非空目录拒绝自动接管，未知/损坏版本拒绝启动并保留原文件。已有 v4 隔离运行目录不会自动迁移为部署数据。未来版本如需修改数据格式，应先在全部实例停止后备份，再使用针对该版本的显式迁移工具；不能靠手工改版本号绕过检查。

备份或搬迁时先正常退出应用，完整复制 `<data-dir>`，然后从新路径启动。原项目 `openvela_smart_home/device_state.json`、`offload_events.jsonl` 和旧封版证据不参与持久部署初始化写入。

回归验证命令：

```powershell
python -X utf8 -m unittest discover -s docs/demo_v4 -p test_persistent_runtime.py -v
```

这些验证使用真实 loopback HTTP、MQTT broker 和软件实体，不启动模型或物理设备；不能据此声称物理执行或物理 exactly-once。
