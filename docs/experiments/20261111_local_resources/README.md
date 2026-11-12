# 本机资源与模型资产记录

2026-11-11 按参赛者“用本机”的要求，复用已运行的两个 llama.cpp 服务。没有连接或计费 AutoDL，没有重新下载模型，也未重启、停止或修改共享模型服务。

## 资源实测

`baseline.json` 保存 30 次、间隔约 1 秒的主机采样。该窗口是共享主机的观测窗口，不能当作严格空闲基线或受控推理能耗实验。

| 项目 | 当前记录 |
|---|---|
| CPU | AMD Ryzen 9 7940H，8 核 / 16 线程 |
| 内存总量 | 16,344,801,280 B |
| 采样窗口可用内存 | 864.95—1053.14 MiB |
| GPU | NVIDIA GeForce RTX 4050 Laptop GPU，nvidia-smi 报告 6141 MiB |
| 小模型进程 RSS 中位数 | 1116.09 MiB |
| 4B 模型进程 RSS 中位数 | 2598.25 MiB |
| GPU 板卡功率读数范围 | 2.51—2.58 W；属于该采样窗口共享板卡读数 |

RSS 不是专属 GPU 显存占用，也不能与共享显存简单相加。WMI 的 AdapterRAM 对该显卡截断为约 4 GB，实际记录采用 nvidia-smi。板卡功率不含整机 CPU、屏幕、电源损耗，不能据此计算每请求能耗、电费或节能收益。

## 模型与运行时

| 资产 | 字节数 | SHA256 |
|---|---:|---|
| Qwen2.5-1.5B-Instruct Q4_K_M | 1,117,320,736 | `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e` |
| Qwen3-4B Q4_K_M | 2,497,280,256 | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama-server.exe | — | `f156c6fcfb61c7dd259e2c577ccbab214185083650f18a9cdfdfb9c38d6cf3b3` |

运行时为 llama.cpp b11010，两个服务使用同一 GPU 的 Vulkan 后端，每服务并行槽位 1、上下文 2048。服务别名分别为 `Qwen2.5-1.5B-Instruct-Q4_K_M`、`Qwen3-4B-Q4_K_M`。只读健康和模型列表检查见相邻 `20261111_fused_real/probe/health_probe.json`。

`provenance/` 保留从已有本机资产复制的模型许可、运行时许可、4B 下载身份和仓库文件元数据；文件哈希见其 `manifest.json`。下载身份文件中的 `inference_started=false` 是原下载时状态，不能代替本赛道后来实际调用的记录。模型权重和运行时二进制不随材料分发。

## 复测

运行 `python docs/deployment/capture_local_resources.py --help`，按当前服务进程号传入 `--model-pid small=PID`、`--model-pid large=PID`，输出到新文件。脚本拒绝覆盖已有采样。

本轮新增云租用费为零，整机购置价格、电价、墙上功耗、部署维护工时尚未取得，不等于总拥有成本为零。成本参数与情景计算见 `docs/value/`。
