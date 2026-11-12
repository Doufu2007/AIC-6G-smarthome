# v4 当前软件证据回放

成片：`dist/final/20261111_v4/SenseHome_当前软件证据回放_20261111_v4.mp4`。

本片是本轮真实运行数据回放与证据说明，**不是连续现场录屏**。全部画面使用 PIL 根据 UTF-8 原始记录自绘为证据卡，没有伪造界面截图，没有使用旧 v1 视频画面，也没有调用电脑或浏览器 UI 工具。

固定顶栏：`真实运行数据回放 · 非连续现场录屏 · 软件实体非实物`。底栏说明镜头停留是编辑节奏；显示的毫秒数分别来自当前 HTTP 计时或注册实验原始分段计时。

八段编辑时长为 24 / 44 / 28 / 32 / 34 / 34 / 32 / 22 秒，共 250 秒。第二段包含简单规则控制和实际模型双动作计划两个画面；最终容器时长以 ffprobe 为准。旁白由 Windows System.Speech 的 Microsoft Huihui Desktop 合成，音频按镜头做保留音高的速度对齐。

输入：

- `final_runtime_records.json`：最终完整模型 + 正式网关 + MQTT 软件实体的四条真实 HTTP 捕获；含动作、回执、状态与计时。
- `final_runtime_manifest.json`：已停止的本轮运行版本与原文件保护记录；源快照在 `final_capture/source_snapshot/`。
- `docs/experiments_v4/registration.json`、`results.json`、`rows.jsonl`：96 行同恢复三臂实验及原始单 POST503 / 双失败记录。视频构建不会重新推理。
- `docs/integration_v4/LATEST.json` 指向的最新审查通过例程：六类持久回执场景与离线原始日志/数据库复核。
- `docs/operations_v4/results.json`：120 秒规则路径负载、自动化新目录部署、未知成本与真人反馈待采。

构建：

```powershell
python -X utf8 docs/video/v4/build_video_v4.py --prepare-only
python -X utf8 docs/video/v4/build_video_v4.py
```

脚本默认只接受最终两个捕获文件，不会自动退回历史或中间捕获。每次准备/构建创建新的 `builds/<UTC>/`，已有最终视频会拒绝覆盖。构建目录保存原始与对齐 WAV、旁白 JSON、语音 PowerShell、场景 PNG、实际编码视频抽帧、ffprobe 和构建命令日志。

`manifest.json` 保存 MP4 相对路径、时长、字节数、SHA256、来源哈希、镜头时长、工具版本、合成语音及编辑边界。`visual_review.json` 记录对实际编码后九张抽帧的逐项检查。没有永久网盘链接；不会将本地文件路径当作外部永久链接。
