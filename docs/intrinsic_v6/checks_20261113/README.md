# 2026-11-13 演示修复回归记录

本目录保留修复当日的机器可读结果和原始测试输出。活动设备状态与事件日志在运行前后哈希一致；这些检查只覆盖软件路径，不代表实物设备、真实用户试用或6G空口测量。

- `result.json`：测试数、退出码、状态哈希及覆盖边界。
- `python_tests.txt`：V6补充回归，427项与258个子测试通过。
- `historical_compatibility_tests.txt`：历史兼容检查，5项通过。
- `dashboard_client_tests.txt`：浏览器请求恢复检查，5项通过。

本轮新加的界面合同覆盖规则模式快捷项、初始空状态、旧请求回查提示；无房间温湿度查询测试确认只澄清房间、不产生动作且设备状态不变。作品方案和答辩PPT为11月12日封版，保留其中423项当时测试的版本记录。

完整补充回归命令：

```powershell
python -X utf8 -m pytest --import-mode=importlib -q SenseHome-6G/net_offload/tests openvela_smart_home/dashboard/tests docs/demo_v4 docs/trial_v2 docs/trial_v4 docs/intrinsic_v5 docs/deployment/fused_eval_tests.py docs/deployment/test_real_fault_eval_v2.py docs/demo_v3/test_planning_recovery.py docs/demo_v3/test_race_matrix.py docs/demo_v3/test_runtime.py docs/demo_v3/test_run_live.py docs/demo_v3/device_receipts/test_device_contract.py
```

该选集427项、258个子测试通过；另运行 `python -X utf8 -m pytest --import-mode=importlib -q docs/experiments_v4/test_comparison.py`（5项通过）及 `node --test docs/intrinsic_v5/test_dashboard_requests.cjs`（5项通过）。完整命令、退出码与输入源码哈希保存在 `_work/intrinsic_v6_fix/checks_20261113T082649Z/result.json`。
