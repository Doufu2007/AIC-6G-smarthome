# 原始模型用量核验

仅从封存报告中的原始 POST 响应读取服务端 usage；不重复计算 adapter 副本、GET 或规则行。没有进行新推理。

| 独立矩阵 | 模型 | POST | 输入 token | 输出 token | HTTP中位/p95 ms |
|---|---|---:|---:|---:|---:|
| challenge_12_sealed_development_repeat3 | Qwen2.5-1.5B-Instruct-Q4_K_M | 108 | 64125 | 8781 | 911.1134999999999 / 1585.61 |
| challenge_12_sealed_development_repeat3 | Qwen3-4B-Q4_K_M | 36 | 21519 | 1305 | 814.3009999999999 / 2375.202 |
| controlled_faults_sealed | Qwen2.5-1.5B-Instruct-Q4_K_M | 36 | 21150 | 2862 | 839.4535000000001 / 962.974 |
| controlled_faults_sealed | Qwen3-4B-Q4_K_M | 19 | 11236 | 621 | 714.043 / 966.225 |
| holdout_12_programmatic_repeat3 | Qwen2.5-1.5B-Instruct-Q4_K_M | 99 | 59076 | 9612 | 959.917 / 1974.888 |
| holdout_12_programmatic_repeat3 | Qwen3-4B-Q4_K_M | 33 | 19824 | 1236 | 826.083 / 1105.053 |
| reviewed_20_sealed_repeat3 | Qwen2.5-1.5B-Instruct-Q4_K_M | 63 | 37116 | 4230 | 613.887 / 5993.541 |
| reviewed_20_sealed_repeat3 | Qwen3-4B-Q4_K_M | 21 | 12456 | 630 | 633.21 / 1395.128 |

四矩阵记录共415次实际POST；非loopback目标0次。校准、历史AutoDL和其他会话不在该口径。
每次调用的文件SHA256、行索引、attempt/exchange索引、case和pair ID见usage_calls.jsonl；token是服务端报告，不是收费提供商账单。
时延为上游HTTP墙钟，不是整条网关时延；不同矩阵和模型分开列出，不用这些计数宣布质量、节能或节费优势。
采购分摊、整机电量、电价、维护及网络费用未知，实际总成本仍为null。GPU板卡功率未代入整机电费。
