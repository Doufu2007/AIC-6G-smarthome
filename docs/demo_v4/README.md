# 当前模型、正式网关与 MQTT 演示入口

从项目根目录执行：

```powershell
python -X utf8 docs/demo_v4/live_v4.py
```

终端打印本次 loopback 页面地址。默认启动两款真实本地模型、正式网关、实际 MQTT 服务及独立持久软件实体；模型位置依 `docs/demo_v3/local_models.json` 配置，模型和推理运行时不随审阅包分发。两模型在同一电脑上共享 GPU，不称独立端云节点。关闭页面不等于停止服务，请在终端正常结束，检查运行清单中的清理结果。

`--strategy auto` 读取冻结 v4 三臂结果，暂用 local-first；本轮对照只有4条复杂人审命令、六种受控条件、一次运行，不能推为统计显著优势或一般语言泛化。`--strategy local-first`、`second-first`、`joint` 可明确选择初选，其余共用恢复器。

`--rules-only` 明确关闭模型，仅适合规则路径开发和部署检查；`--json-only` 明确关闭 MQTT。不能把这两种降级模式的结果写成完整模型或 MQTT 证据。

执行通道为动作白名单校验、持久 command_id、平台适配、回执和状态查询。超时、未知状态或部分执行后只回查，不盲目重发；历史回执不会覆盖较新状态。MQTT 软件状态只证明软件平台执行，不证明灯具的物理动作、Matter认证或物理 exactly-once。

真实试用请用 `docs/trial_v4/run_trial.py --record-kind participant`。每位参与者单独启动一次；先需求前测，再自拟任务和反馈。必须取得知情授权，开发者和自动化记录不计为外部反馈。私有试用记录不进入公开包。

本轮完整模型自动捕获位于 `docs/video/v4/final_runtime_records.json`，运行版本和正常清理位于 `final_runtime_manifest.json`。视频是这些真实记录的编辑回放，不是连续现场录屏。
