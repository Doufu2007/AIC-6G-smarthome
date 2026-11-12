# 可复算成本模型与真实资源边界

版本：2026-11-11。本轮只使用现有本机模型服务，没有新租用 AutoDL 或调用收费云 API。**没有云账单，不等于模型网关总成本为零。** 采购价、整机墙上电量、电价、维护和增量网络费尚未取得，因此当前不能给出实测月总成本或实测每千请求成本。

## 三类证据分别保存

| 类别 | 本次取得的事实/参数 | 可支持的结论 | 不可支持的结论 |
|---|---|---|---|
| 本机实际测量 | `docs/experiments/20261111_local_resources/baseline.json`：30样本 CPU/RAM、两模型 RSS/CPU累计时间、共享 GPU 显存与板卡功耗、权重字节/哈希 | 此电脑能在该窗口同时保持两服务；资源占用和余量可复查 | 最低硬件要求、长期并发、整机电费或每请求焦耳 |
| 公开云报价 | `public_sources.json` S9：2026-11-11 官方 DeepSeek token 价 | 给成本工具提供可替换的单位价 | 实际账单、真实模型等价质量、用户愿付价格 |
| 情景假设 | 请求量、token量、上云比例、时长、功率、购置/摊销、工时与电价 | 检验公式及参数敏感性 | 实测成本、节能收益、客户需求规模 |

当前资源摘要 `host_resources_summary.json` 保留原件 SHA256 `bc356694bf830ea696ae1c73cbe6be6a77bcf3e3462d59581af6af2d668c6544`。Qwen2.5-1.5B 与 Qwen3-4B 的量化文件分别为 1,117,320,736 B 与 2,497,280,256 B；采样进程 RSS 中位分别 1,170,309,120 B 与 2,724,466,688 B。整卡显存已用 3,735 MiB，不能分配归因到单个模型；主机总 RAM 为 16,344,801,280 B，可用 RAM 最低 906,969,088 B。它们是短时、同机、共享资源测量，不是量产网关规格。

`nvidia-smi` 板卡功耗本窗口为 2.51—2.58 W（中位2.53 W）。该值不是 CPU、内存、显示器、电源转换与其他主机组件功耗，也没有逐请求归因；本模型不把它填进 `idle_watts` 或 `active_watts`。CPU 百分比、CPU 时间、RSS、TDP 都不能直接换算整机电量。

## 计算口径

单位币种由配置指定；全部货币字段必须已处于同一币种，CLI 不做汇率换算。月按输入的运行小时数定义；不把每月720小时当测量事实。金额均为未含未填项目的参数化模型；工具不会把未知值偷偷置零。

| 符号 | 配置字段 | 单位/定义 |
|---|---|---|
| N | `monthly_requests` | 月总请求数，正整数；含失败/超时/被拒绝请求 |
| f | `cloud_fraction` | 计划路由到云模型的请求比例 [0,1]；不是成功比例 |
| a | `cloud_attempts_per_routed_request` | 每个上云请求的平均收费尝试数，≥1；含重试/回退的收费尝试 |
| I/O | `input_tokens_per_cloud_attempt` / `output_tokens_per_cloud_attempt` | 每次收费尝试平均输入/输出 token；系统提示和历史也需算输入 |
| pI/pO | `cloud_input_per_million` / `cloud_output_per_million` | 每百万输入/输出 token 单位价，同币种 |
| P/M | `gateway_purchase` / `amortization_months` | 分摊购置金额/正摊销月数；现有电脑也要声明增量或分摊口径 |
| H | `monthly_uptime_hours` | 网关本月开机小时；包括模型常驻待机 |
| W0/W1 | `idle_watts` / `active_watts` | 整机墙上待机/局部处理活跃平均瓦数，W1≥W0 |
| t | `local_seconds_per_request` | 每个非云请求的平均活跃秒数；包含该情景的规则/模型/失败开销分配 |
| e | `electricity_per_kwh` | 实际适用电价/千瓦时；不能用全国假定价代替 |
| U/B | `monthly_operations` / `monthly_network` | 维护摊销与增量网络费用；为0需要明确假设/事实依据 |

公式：

```text
云端收费尝试数 A = N × f × a
云 token 费 C_api = A × (I × pI + O × pO) / 1,000,000
本地串行活跃小时 h = N × (1 − f) × t / 3,600
整机电量 E_kWh = [W0 × H + (W1 − W0) × h] / 1,000
月电费 C_e = E_kWh × e
月硬件摊销 C_h = P / M
月总成本 C = C_h + C_e + C_api + U + B
每1,000总请求成本 = C × 1,000 / N
```

这是单网关、平均负载串行化的简化模型。若 `h > H`，工具拒绝该情景，不能用超过整月时长的串行功耗模型伪算并发。真实批处理/并发应采用电表积分或新的有效活跃时间口径。模型冷启动、失败重试、同步探测、云等待时本机工作等必须被平均活跃分配覆盖，或在实测电量中积分；当前简单情景没有测量这些附加开销。

云价只计 token API：不包含语音、搜索/工具调用、托管 GPU 小时、对象存储、数据传输、税费、融资、真实设备采购和故障赔偿，除非另行并入适当输入。模型下载与安装工时应进入部署成本分摊，不能因为是开源软件就计为零。重试平均 token 与失败计费以提供商账单为准；示例统一按平均收费尝试简化。

若需要“每千正确完成请求成本”，应使用独立标签核验得到的正确完成数 Q，计算 `C×1000/Q`，并保留所有失败开销；Q=0时不可定义。当前 CLI 不会用 HTTP 200、模型自报或预想质量生成 Q，也不据成本结果宣布模型等价。

## 未知项传播与证据标签

每个参数有 `value/status/source`：

- `measured`：实际测量，source 指向原始记录及口径。
- `quote`：公开报价，source 含发布方、URL/来源编号、访问日期和计价条件。
- `assumption`：情景参数，不能标成实测。
- `unknown`：value 必须为 `null`；其他状态则必须有有限非负数。

任何必要分项缺数据，CLI 的 `monthly_total`、`per_1000_requests_total` 保持 `null`，只显示已知分项小计。`f=0` 时没有云调用，云 token 费可为0，未使用的云价不影响其余已知分项；这不代表硬件、电费或维护费为0。工具同时输出每千请求的各分项、所有参数标签、未知参数和未知成本分项。

`cost_current_unknown.json` 采用1000请求归一化假设和本机部署方式；它不是本月流量。云 token 分项为0，硬件、电费、运维和网络仍未知，输出总成本未知。填入假设后即使得到完整数字，也仍然是情景测算，不能升级成实际月账单。

## 可修改的演示情景

**以下全为演示假设，唯云 token 单价是公开报价；不是本机实测，更不是用户价格。** 为避免汇率假设，三个情景统一以 USD 计价。假设相同网关购置$180、摊销36个月、720小时运行、3000请求、待机10W、活跃35W、本地每请求3秒、电价$0.15/kWh、维护$5/月、增量网络费$0/月、每次云尝试1500输入+200输出 token、平均收费尝试1次。

报价源：S9，访问当日 `deepseek-flash`（页面模型版本 DeepSeek-V4.1-Flash-1017），高峰/缓存未命中输入$0.30/百万 token，输出$1.20/百万 token。非高峰与缓存命中另价；示例不使用折扣。官方可能调整报价，正式部署应重取；本轮没有向其发送推理请求，也没有以其模型做质量对照。

| 演示配置 | 云比例 | 硬件/月 | 电费/月 | 云token/月 | 运维/月 | 总计/月 | 每千总请求 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `cost_scenario_local.json` | 0% | $5.000000 | $1.089375 | $0 | $5 | $11.089375 | $3.696458 |
| `cost_scenario_hybrid.json` | 25% | $5.000000 | $1.087031 | $0.517500 | $5 | $11.604531 | $3.868177 |
| `cost_scenario_cloud.json` | 100% | $5.000000 | $1.080000 | $2.070000 | $5 | $13.150000 | $4.383333 |

这里保留相同网关硬件，旨在展示分项和上云比例的数学影响。若全云部署允许更便宜的网关、不同 token、并发、质量或云开销，需修改参数。不能把三行差额称作本作品实测节费，更不能据此选择模型；真实质量、延时和可用性也应同时满足。

## 补齐真实成本的最短采证路径

1. 保留真实采购价或既有电脑分摊口径、可用年限、维护工时与电价来源。没有凭据则继续未知。
2. 以相同输入/模型版本、相同成功质量和网络条件成对运行，包含待机/加载/推理/失败/恢复；用插座电表累计 kWh，并记录起止、并发与其他进程。共享电脑无法归因时报告整机范围，不硬分摊给单请求。
3. 若未来使用收费服务，保存脱敏用量/账单（输入、输出、缓存、尝试、价格版本、时段），和本机流量分开计。当前无新增云调用可计费。
4. 用实际观测更新配置并保留旧版，把 `measured` 和 `quote` 指向原件。模型成本与设备采购、部署工时分别说明。完成后仍需声明摊销与资源归因范围。

## 复算命令与验证

```powershell
python -m unittest discover -s docs/value -p test_cost_calculator.py -v
python docs/value/cost_calculator.py --config docs/value/cost_current_unknown.json --json --out docs/value/cost_results/current_unknown.json
python docs/value/cost_calculator.py --config docs/value/cost_scenario_local.json --json --out docs/value/cost_results/scenario_local.json
python docs/value/cost_calculator.py --config docs/value/cost_scenario_hybrid.json --json --out docs/value/cost_results/scenario_hybrid.json
python docs/value/cost_calculator.py --config docs/value/cost_scenario_cloud.json --json --out docs/value/cost_results/scenario_cloud.json
```

脚本仅依赖 Python 标准库。7个行为测试覆盖独立手算、未知功率传播、重试收费、零云调用、无效/不可能输入、证据标签与未知字段、CLI配置读取。原始资源采样脚本另依赖其环境中的 `psutil`/`nvidia-smi`；这不影响成本 CLI 的离线复算。
