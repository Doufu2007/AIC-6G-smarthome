# 演示视频 v2：真实软件事件回放

输出：`dist/SenseHome_AI6G_演示视频_20261103_v2.mp4`。

在原 4:05 视频的 **0:25—1:00** 中，用 35 秒真实软件事件回放替换原第二镜。回放只使用 Python/Pillow/ffmpeg，没有操作任何桌面或浏览器 UI。第一版 MP4 保留不变。

回放执行的是当前 Dashboard 的 `process_command`，连接当前 C `policy_cli.exe`，使用临时隔离的 `STATE_FILE`，将 WAN/LAN 都注入为不可用。四条命令依次真实执行并保留完整结果、事件及状态前后：

| 步骤 | 命令 | 实际状态/结果 |
|---|---|---|
| 1 | 打开客厅的灯 | 客厅关闭/0% → 开启/100% |
| 2 | 查询安防状态 | 只读返回未布防，状态不变 |
| 3 | 关闭客厅的灯并打开卧室的灯 | `actions=[]`，`semantic_complete=false`，全部状态不变 |
| 4 | 关闭客厅的灯 | 客厅开启/100% → 关闭/0% |

画面明确显示“真实软件事件回放 / PC模拟设备 / 非现场录屏”，并注明编辑播放节奏不是执行耗时。画面上的每次墙钟是这次运行的单个样本（n=1），不作为 p95 或性能结论。完整原始数据是 `software_replay.json` 与 `events.jsonl`。`software_replay.json` 包含源码/策略二进制 SHA256；原项目状态文件已按字节核对未变。

## 验收

- 245.047 秒，符合 3—5 分钟。
- 1920×1080，30 fps，7349 个视频帧；H.264 + AAC。
- 6,594,060 B，低于 300 MB。
- MP4 全片解码无错误；抽检第 28、36、45、56、235 秒。
- 七段主场景时长保持不变；新第二镜四段音频分别对齐 8/8/11/8 秒，尾部保留半秒余量，未截断。
- 无单位 Logo、学校名称、个人姓名、密码或连接凭据。
- 仍然不包含真实模型推理、物理执行器或 6G 空口结果。

v2 SHA256：`cfcf37ab4f5630abf4306a37c16ebad18ecaf1392c99c1964d8efa3f4bddd782`。

v1 保持 SHA256：`57183794c991a26e027fa5b0052b6f5cdc7f67a4a9520028b5d967928056fcac`。

## 文件

- `capture_replay.py`：真实运行采集，拒绝覆盖原始证据；重新运行需改输出目录或先归档。
- `software_replay.json` / `events.jsonl`：实际完整记录。
- `render_replay.py`：从实际 JSON 渲染四幅回放画面。
- `_work/video_v2_frames/replay_01.png` 至 `replay_04.png`：回放帧。
- `narration_scene2.txt` / `voice_scene2.ps1`：新旁白和语音生成。
- `build_v2.py`：保持第一版，替换第二镜并对齐音频。
- `manifest.json`：媒体参数、输入/输出/证据哈希。
