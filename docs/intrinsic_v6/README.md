# V6 项目本体修复与公开复现

修复范围见 `docs/reviews/2026-11-12-intrinsic-project-audit-v6.md`。设计和任务计划位于 `docs/superpowers/specs/2026-11-12-intrinsic-audit-v6-repairs-design.md` 与对应 plans 文件。

## 本机启动

以下入口不需要模型权重，使用本机 MQTT 软件实体。先准备 Python 3.13、可用的 C99 编译器和项目策略程序：

```powershell
python -m pip install -r docs/integration_v4/requirements-lock.txt pytest pytest-subtests
# Windows 安装 gcc 后编译；此命令不改旧实验结果。
powershell -ExecutionPolicy Bypass -File SenseHome-6G/net_offload/build.ps1 -SkipPythonTests
python -X utf8 docs/demo_v4/live_v4.py --rules-only
```

服务仅绑定 `127.0.0.1`。当前完整明确的灯光/窗帘/温湿度命令可直接编译执行；不完整的目标需澄清。界面中的 MQTT 平台状态仍只代表软件实体，默认初始光照/人体值不能授权巡检自动控制。运行目录保存在本机 `_work/`，不进入 Git。

## 原始证据

两个历史 `raw_requests.jsonl` 以同目录 `.jsonl.gz` 无损压缩件公开，避免 GitHub 单文件限制。原件没有修改，压缩件解压后的大小和 SHA256 记录在 `SenseHome-6G/evidence/network_compute/20261111_fused/public_raw_manifest.json`。在运行旧校验工具前，从仓库根目录执行：

```powershell
python -X utf8 docs/intrinsic_v6/restore_public_evidence.py
```

脚本核验压缩件与解压结果；已存在且哈希一致的文件只验证，不覆盖。也可用 `--output-root` 指定其他目录。原始日志代表其冻结版本的实验；当前源码修复的验证结果单独记录。

## 回归

```powershell
python -X utf8 docs/intrinsic_v6/run_checks.py
```

需要项目已有 Python 测试依赖及本机 C 策略程序，详细依赖与构建方式见 `SenseHome-6G/net_offload/README.md`。检查使用私有临时状态及自有 MQTT 软件实体，不启动真实模型或连接实物。测试通过只能证明所覆盖的软件行为。

本机 `_work/`、运行状态/事件、志愿者原话及内部审阅包不属于本次公开上传范围。旧封版材料和旧结果保留其原始版本语义；当前代码状态以修复报告为准。
