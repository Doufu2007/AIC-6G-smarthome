# 视频素材与验收记录

输出：`dist/SenseHome_AI6G_演示视频_20261103.mp4`。

本版是**证据讲解视频（七页设计图 + 中文合成旁白）**，不是软件运行录屏。依据现有分镜与 `SenseHome-6G/net_offload/results/results.json` 的锁定数字制作。设计示意、桩合同、模拟设备和真实模型待验证均已在画面/旁白明确，未宣称真实物理设备、家庭试点、6G 空口或真实 GPU 模型准确率。

- 时长：245.047 秒（4 分 05 秒）。
- 画幅：1920×1080，30 fps，H.264 High/yuv420p。
- 音轨：AAC，中文 Microsoft Huihui 合成语音；七段按分镜对齐，结尾不截断。
- 文件：6,305,073 B，低于 300 MB。
- SHA256：`57183794c991a26e027fa5b0052b6f5cdc7f67a4a9520028b5d967928056fcac`。
- 编解码/时长证据：`docs/video/ffprobe.json`。
- 已目视检查第 0、125、235 秒抽帧，文字未溢出且数字清楚；音轨非静音，无截断风险。

## 素材清单

| 文件 | 用途 |
|---|---|
| `docs/video/make_video.py` | 用 Pillow 生成七张 1080p 画面 |
| `_work/video_frames/scene_01.png` 至 `scene_07.png` | 视频七个分镜画面 |
| `docs/video/narration.txt` | 中文旁白全文 |
| `docs/video/voice.ps1` | Windows SAPI 生成中文语音 |
| `docs/video/align_audio.py` | 七段语音与分镜对齐并封装 MP4 |
| `docs/video/narration_aligned.wav` | 245 秒最终旁白 |
| `_work/video_manifest.json` | 分镜时长、画幅、证据范围 |
| `_work/video_check_00.png` / `125.png` / `235.png` | 从最终 MP4 解码抽帧 |

## 后续替换点

真实模型评测通过后，更新第 7 镜的进度和第 5 镜模型身份素材。要达到运行演示视频质量，仍建议把第 2 镜替换为 Dashboard 真实操作录屏，把第 5 镜替换为真实异常及回退操作录屏；静态讲解版可作当前可播放审阅稿。
