# v4 公平恢复策略比较

这是同一台 RTX4050 主机上的预注册回归实验，两个候选分别为 Qwen2.5-1.5B 和 Qwen3-4B。本地/第二端是逻辑槽位，设备为隔离 JSON 状态，不代表独立云、物理设备、WAN 或 6G。

使用现有 20 条人工冻结标签中的全部四条可执行复杂命令 C01—C04，六条件 × 三臂 = 72 行主分析。另八条澄清/拒绝任务仅健康条件 × 三臂 = 24 行空计划结构检查；不计作复杂任务完成，不评判回答文字是否恰当。没有新人工留出或用户泛化声明。

三臂 local-first / second-first / joint 分别使用同一个 C 程序的 static-local / static-cloud / joint 模式。除初选模式外，恢复器、两模型、生产提示、15s 推理超时、256 max_tokens、temperature=0、10s 备用启动预算、初态、执行器、评分完全共用。每块每臂新建 C 进程；同块共用实际 health 观测。四命令 × 六条件的第一执行臂恰好各八次。

六种条件：healthy、local_wait700、second_wait700、local_post503、second_post503、both_post503。等待在 loopback HTTP 代理中真实发生，health 暴露上一次实测固定等待和样本年龄，不称 GPU 队列。503 仅在块内共享 health 后对 POST 生效，GET inventory 继续健康；不称模型崩溃。每条件每模型一条固定非评测预热，原始结果另存。

冻结来源在 frozen_source/，保持原目录布局，隔离生产网关/bridge 的并行修改；原 v3 文件未改动。source_capture.json 是复制时的哈希记录。注册之后不得修改注册的 runner、离线核验器、配置、标签、初态和源码。正式输出不允许覆盖或有行后重跑，失败照存。

操作：

    python -X utf8 -m unittest discover -s docs/experiments_v4 -p test_comparison.py -v
    python -X utf8 docs/experiments_v4/run_comparison.py register
    python -X utf8 docs/experiments_v4/run_comparison.py run
    python -X utf8 docs/experiments_v4/verify_run.py

上述 register/run 仅能各执行一次；复现新实验应复制整个目录到新版本，保留原目录与哈希，不得删行重跑。runtime 使用原 docs/demo_v3/runtime.py 的冻结副本，解析原本机模型路径到绝对路径，先校验 exe/DLL/GGUF，再启动并只清理本轮拥有的子进程。无 AutoDL 或模型下载。

timing_ms 的分段：

- c_decision_ipc：实际 C decide 命令写入至读取响应的往返，含进程通信；c_internal=null，未测 C 内部 CPU 指令耗时。
- policy_adapter：原网关实际策略计时，含观测接线与 C 通信，非配置中的估计 policy_model_ms。
- observation_health：每行实际两端 health 总墙钟；策略仍使用块内同一份观测；共享观测的额外实测开销在 blocks.json。
- first_model、recovery_health、backup_model：原恢复器分别实测；未调用为 0，未知为 null。
- executor：正式冻结执行器的实际调用墙钟；软件 JSON 执行。
- gateway_wall：原网关实际 handle 墙钟。
- total_wall：从行 health 开始到网关返回并读完状态的连续实际墙钟，不是分段估计求和。C 进程启动、初态重置和共享观测是实验控制开销，不属于该服务请求区间。

rows.jsonl 保存全部原始模型 POST、响应文字、usage、代理转发/503、恢复记录、策略命令与状态；warmups.json 单列预热，禁止并入评测 usage。results.json 按臂/条件汇总，主结果含失败时延。

默认推荐规则在执行前冻结：先最大化 72 行内各臂完成数，再最小化各臂所有请求实际总墙钟中位数，最后按 local-first / second-first / joint 破同分。六条件等权只属于测试设计，不代表真实用户负载频率；推荐仅该条件回归的暂定候选，不外推统计显著性。另报告完成数/模型调用数/健康时延的 Pareto 比较。

独立 verify_run.py 不导入 runner/生产评分，不访问网络，重新核对顺序/源码/标签/动作/状态/执行/POST/usage/计时/汇总与推荐，并要求 runtime 已停止。verification.json 是离线复核报告。
