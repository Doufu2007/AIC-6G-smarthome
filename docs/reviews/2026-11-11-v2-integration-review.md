# 第二版集成证据独立核验

核验范围：受控 HTTP 故障实验的原始证据与汇总、上一版封存四矩阵及审阅包的完整性、当前运行时可追溯性。审查者为本轮独立子代理；未运行模型、未改原始记录、未执行会覆写 `audit.json` 的 `verify_run.py`，也未刷新旧索引。只写入本报告。

结论：本范围未发现未解决的 Critical 或 Important 计数、评分、哈希或配对一致性问题。受控实验结论有原始 HTTP 与模型响应支持。当前缺少运行期间生成的模型进程命令快照，必须保留为溯源局限；不得将事后整理写成当时已取得的启动清单。

## 1. 新实验计数与评分

独立逐条件读取 `registration.json`、`summary.json`、`report.json`、`report.gateway_raw.json`、JSONL、代理 trace 与预热记录，重新调用冻结的 `score_outcome` 对全部 120 行评分，并核对保存字段。未调用网络或模型。

| 条件 | 行 / 四臂配对块 | 实际模型 POST（全部 HTTP 200） | 未转发代理 503 | fixed-local 完成 | fixed-cloud 完成 | network-only 完成 | joint 完成 |
|---|---:|---:|---:|---:|---:|---:|---:|
| healthy | 24 / 6 | 24 | 0 | 6/6 | 6/6 | 6/6 | 6/6 |
| cloud_http503 | 24 / 6 | 18 | 6 | 6/6 | 0/6 | 6/6 | 6/6 |
| local_http503 | 24 / 6 | 12 | 12 | 0/6 | 6/6 | 0/6 | 6/6 |
| cloud_admission700 | 24 / 6 | 24 | 0 | 6/6 | 6/6 | 6/6 | 6/6 |
| local_admission700 | 24 / 6 | 24 | 0 | 6/6 | 6/6 | 6/6 | 6/6 |

主矩阵合计 120 次客户端至代理 POST，102 次转发真实模型，18 次代理实际返回 503 而未调用模型；不能将 120 次都写成模型推理。另有 10 次预热代理 POST，其中 8 次转发模型、2 次代理 503，已与主矩阵 trace 分离。主矩阵 60 次模型清单 GET 和 60 次代理遥测 GET 计数一致。

每行 POST 的 trace ID 与代理记录一一对应，无跨行复用；各条件的全部代理 POST 恰好等于主矩阵 trace 加预热 trace。全部行状态为 finished，运行器失败和跳过均为 0。报告行与 JSONL 完全一致，原始网关记录重新汇总与保存摘要一致。

30 个配对块中，每块恰好包含四臂，顺序与 block 记录一致；四臂命令、输入哈希、观测哈希、初始模拟状态和预期动作一致。遥测 ready、可达性和 queue_ms 与分块观测一致。全部评分重新计算通过，不能将接口成功、空动作、回退或快速失败替代非空任务完成。

## 2. 时延与操纵真实性

| 条件 | fixed-local 中位 ms | fixed-cloud 中位 ms | network-only 中位 ms | joint 中位 ms |
|---|---:|---:|---:|---:|
| healthy | 729.914 | 1113.954 | 721.004 | 708.981 |
| cloud_http503 | 712.716 | 5.517（任务失败） | 714.764 | 696.539 |
| local_http503 | 4.985（任务失败） | 1054.510 | 5.185（任务失败） | 1108.434 |
| cloud_admission700 | 714.990 | 1790.390 | 719.141 | 712.163 |
| local_admission700 | 1420.246 | 1046.806 | 1431.163 | 1042.006 |

中位数直接由各条件、各臂的六行 `gateway_wall_ms` 复算，属于全请求时延。两组 503 的约 5ms 是失败响应，不能宣传成业务时延收益。健康条件不支持普遍或显著优于其他策略的结论。

受控延迟确实发生在代理转发前：第二端准入等待实际为 701.068-714.228ms，本地端为 700.899-714.735ms（含预热）；每次配置值与对应注册条件一致。503 条件均未向底层模型转发。该证据是软件代理注入的失败与固定准入等待，不能描述成 GPU 排队、真实 WAN 故障、独立云边或 6G 空口结果。

## 3. 注册与源码

注册时间早于每个条件的运行开始。登记的配置与每条件配置一致。登记的 12 个源码/二进制 SHA256 与当前文件、总快照和每条件所含快照一致；冻结标签、所选子集与校准哈希一致。未以 H05 留出结果修复或改动源码。

| 文件 | 本次核对的 SHA256 |
|---|---|
| registration.json | `926b4e18b0aa1428f31d8f7a90886c65e928f8ce7191abd4a2a4cdff4decf630` |
| summary.json | `9b64bb49ffcba595133d791dd0adc1290a64101f66172e30f351256cd3e7f767` |

## 4. 上一版保全

旧 ZIP 的 SHA256 仍为 `90fb5b1d162400109526ad860f476b44282d0210597915ffb9a5fcc9ba98bd27`，大小 24,134,445 字节，634 个文件，ZIP 全部 CRC 检查通过。

当前旧索引和旧离线审计文件逐字节等于该 ZIP 中的版本。四矩阵索引列出的 24 个结果文件全部同时通过索引 SHA256 与 ZIP 内字节比对；全部登记生产源码/二进制当前哈希也一致。旧索引 SHA256 为 `c7a5f98cc4c81f498f7454e19d64d6bc3d0011c7bd1b919c41ce1be243544490`。

| 旧矩阵 | 实际保存行 | 从原始 exchange 独立计数的模型 POST |
|---|---:|---:|
| reviewed_20_sealed_repeat3 | 240 | 84 |
| challenge_12_sealed_development_repeat3 | 144 | 144 |
| controlled_faults_sealed | 72 | 55 |
| holdout_12_programmatic_repeat3 | 144 | 132 |
| 合计（仅执行量） | 600 | 415 |

这是保全和执行量核验，不是跨数据集合并效果指标的授权。人工冻结回归、程序开发集、受控条件和程序留出仍必须分别解释。

## 5. 运行时溯源局限

本次检查时 `local_runtime_manifest.json` 不存在，psutil 未发现活跃 `llama-server.exe`，因此没有通过当前进程核验本轮模型启动 argv、PID 或进程创建时间。主线已获知，不重跑模型来补造历史快照。

登记文件和响应原始记录可以支持本轮逻辑端点、模型 ID、请求与完成时间等事实；它们不能替代当时的启动命令快照或权重载入证明。若另补事后文件哈希与历史工具启动记录，应标明采集时间、来源与可验证范围，并披露与旧轮次相比的 `--reasoning-budget 0` 差异。以上缺项不使已保存的 HTTP trace 和计数失效，但不允许声称已经核验独立物理云边、真实 6G、实体执行或真实用户泛化。

## 6. 部署探测与试用修复独立复核

本次继续只读检查实现，并运行相关自动化测试。命令为：

```text
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider docs/value_v2/test_evidence_tools.py docs/trial_v2/test_collector.py docs/trial_v2/test_isolated_gateway.py
```

结果为 **25 passed, 9 subtests passed**。使用真实本机临时 HTTP 服务和隔离子进程，不调用真实模型，不形成外部参与者反馈。pytest 缓存和 Python 字节码写入已关闭；子进程临时模拟状态在测试目录内清理。未改实现、旧封存证据或原有审计文件。

### 部署 I1：环境代理与重定向

`deployment_check.py` 已使用 `http.client.HTTPConnection/HTTPSConnection` 直接建立目标连接。CLI 先校验 URL 无凭据、query 或 fragment；实际探测进一步仅允许字面量回环 IP，不依赖可被重新解析的 `localhost`。不使用 `urllib` 的环境或系统代理路径；HTTP 3xx 在读取模型清单前返回 `HTTP_REDIRECT_REFUSED`，不访问 Location。

独立运行测试中的两项真实 HTTP 夹具通过：

- 为子进程设置大小写 HTTP/HTTPS/ALL 代理并清空 NO_PROXY 后，声明目标收到两次 `/v1/models` GET，代理收到 0 次；返回模型 ID 来自声明目标，记录的 connected_peer 为 127.0.0.1。
- 目标返回 302 后，两次探测均记录拒绝重定向；重定向目的服务收到 0 次请求。

结论：原 I1 修复在所测 CLI 路径已生效，未发现未解决的 Critical/Important。本检查仍只能证明只读清单探测路径，不能证明双物理主机、WAN 或任务执行。

### 试用：状态错误不得确认空动作未变更

`TrialStore.execute` 保存原始前后状态 HTTP 响应，但只将 `validated_state_response` 验证通过的状态交给评分器。HTTP 非 200、传输错误、业务错误、缺少或空设备结构，以及关键字段类型/范围异常均使状态为 None；空目标的未变更结果保留为 null。

已有真实 HTTP503 夹具通过：前后相同错误 body 不再产生空目标“状态未变”确认。另独立构造八种状态异常：HTTP503、传输错误、HTTP200 内业务错误、空 lights、窗帘位置越界、布尔值被整数代替、NaN 温度、缺失 temperature；全部返回无有效状态，`empty_target_no_action_no_mutation` 和 `task_completed` 为 null，`state_execution_confirmed` 为 false。正确的完整设备状态则可通过验证。

结论：原 Important 修复生效；状态查询失败没有被当成空动作成功。空目标的安全未变更字段与非空业务完成字段仍分开表达。

### 隔离启动器：保留原状态和生产行为

独立复核 `run_isolated_gateway.py`：新运行目录必须是 `_work/trial_v2/` 下尚不存在的子目录；事件路径在导入桥接前覆盖，设备状态在初始化后覆盖到新目录；模型 URL 显式置空，随后关闭导入时网关并以 `start_monitor=False` 重建，巡检关闭。HTTP 仅绑定 127.0.0.1，并限制状态、健康、命令路由。生产 C 策略、规则、白名单和执行器被复用，没有根据 T1/T2 文本编写特殊成功分支。

真实子进程与 HTTP 测试通过：T1 使用生产规则完成并改变隔离设备状态；未配置模型的 T2 保持 `semantic_complete=false` 和空动作；巡检触发路由为 404；带随机令牌的关闭请求成功退出；运行中仅隔离目录生成状态和事件，原 `openvela_smart_home/device_state.json` 与 `offload_events.jsonl` 在前后 SHA256 比较中一致。

结论：两项要求均满足所测范围，不新增 Critical/Important。试用仍是本机模拟，自动化测试不计实际用户样本。

本次复核实现版本：

| 文件 | SHA256 |
|---|---|
| docs/value_v2/deployment_check.py | `6f5b317809d2105ce99c72d7df0b03f27e3c3c4a4f9057ca92d3cf4bbab391d2` |
| docs/trial_v2/collector.py | `6385eaaa603c587445c7ba0b0c703c49f148e42bb4757b50401764410d6d0ef9` |
| docs/trial_v2/run_isolated_gateway.py | `1333d1bb0ffc479d20707b44438f9815b2af613cf0acb9e06c1cec5ec24eef60` |
