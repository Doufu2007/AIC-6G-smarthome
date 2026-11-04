# AutoDL 大小模型顺序对照

本工具扩展现有小模型恢复流程。小模型已完成两轮草案预检；大模型已冻结 revision `a09a35458c702b33eeacc393d103063234e8bc28` 并开始分片恢复，因镜像 Range 长时间停滞而保留断点中断，尚无完整权重和推理结果。先完成小模型原始证据回收并退出其 vLLM/wrapper，再在同一 GPU 上运行大模型。两次顺序实验用于比较动作规划，不代表双模型同时在线、实时边云调度或真实 6G 空口。两次都使用相同 20 条 `human_reviewed=false` 草案和相同重复次数；结果只能作开发预检。

## 本地生成扩展包

```powershell
python docs/deployment/prepare_autodl_recovery.py --include-large --output _work/autodl-recovery-large.zip
```

包内包含 small/large 两个恢复脚本、根目录 `eval_model.py`、`backends/` 下的 wrapper/当前设备动作白名单/原始草案，以及逐文件 SHA256 manifest，共 7 文件。用新目录上传解压，不覆盖旧实验结果。当前上游 wrapper 如有修复，应重新打包以包含实际版本。

## 远端复用系统 conda

非登录 SSH 的 PATH 可能找不到 Python 和 `ninja`。实际环境是 `/root/miniconda3/bin/python`，已安装 Torch/vLLM，执行前设置：

```bash
export PATH=/root/miniconda3/bin:$PATH
cd /root/autodl-tmp/<实际解压恢复目录>
/root/miniconda3/bin/python autodl_large_recovery.py weights --workers 4
```

不创建 venv、不安装依赖，也不额外改 CUDA 编译路径。`sys.executable` 的 bin 目录必须在 PATH 中，vLLM 调用的 `ninja` 才能找到。第一次大模型权重恢复从 `Qwen/Qwen2.5-7B-Instruct` 镜像 API 的 `main` 获得真实 commit，立即以 immutable revision API 和 commit header 再核对并冻结。再次执行复用该冻结 commit，不追随后来变动的 main。

只接受 API 明确列出的 config、generation config、tokenizer 相关文件、`model.safetensors.index.json` 及该已验证索引实际引用的分片。配置/索引必须通过 Git blob ID 或明确 LFS 身份验证，权重分片必须同时有 64 位 LFS SHA256 和正整数字节数；缺失元数据直接停止。模型 revision、文件列表和哈希均从实际 API 获取，脚本没有预填 7B 权重哈希。

大分片按约 193 MB 数字块顺序保存到 `large_parts/<shard>/part_0000...`，最多 4 个并发 Range 传输。复用已完成块，短块只续传尾部；每次请求核对精确 Content-Range。最后按 manifest 的明确顺序拼接，逐完整 shard 校验 API LFS SHA256，通过后才成为模型权重。磁盘为所有权重预留总量两倍（分块与拼接副本），恢复时扣除已经占用该预留空间的合格大小文件；不要删除未完成证据后重试。

## 顺序运行大模型预检

```bash
/root/miniconda3/bin/python autodl_large_recovery.py preflight --repeat 1
```

启动前要求小模型端口 8001/18092 均空闲，GPU 无其他计算进程；不满足时只报错，不杀进程。大模型使用 `sensehome-large`、上游 8002、wrapper 18093、`tier=cloud`，仍绑定回环地址。模型长度 4096、显存预算 0.9、随机种子 20261103，与小模型生成设置一致。

若已上传实际的 `capture_real_gateway.py` 和其依赖源码，可显式添加：

```bash
/root/miniconda3/bin/python autodl_large_recovery.py preflight --repeat 1 --gateway-capture-script /root/autodl-tmp/<实际目录>/capture_real_gateway.py
```

云档位的 capture 必须显式声明注入的 LAN 测试夹具（它不是物理 LAN 测量）：

```bash
/root/miniconda3/bin/python autodl_large_recovery.py preflight --repeat 1 \
  --gateway-capture-script /root/autodl-tmp/<实际目录>/capture_real_gateway.py \
  --gateway-capture-inject-lan-ok
```

小模型同样支持 `--gateway-capture-script` 和 `--gateway-capture-inject-lan-ok`。云档位传入 capture 脚本却未显式设置后者会阻断，防止把未知 LAN 当作实测；设置后命令明确包含 `--inject-lan-ok`，证据标注 `inject_lan_ok_source=explicit_preflight_flag`。该路径必须是本机已存在文件；脚本以当前 Python、参数数组和 `check=True` 执行，不通过 shell。调用发生在完整评测文件写出、逐条模型身份核对后且模型退出前，命令为 `--url <wrapper回环地址> --tier gateway|cloud --out <本次证据>/gateway_capture`，另存源码哈希和 stdout。

白名单拒绝等 HTTP 错误保留为错误，状态为 `completed_with_interface_errors`；`all_http_success=false`，不把这些请求算作成功或正确。完整身份可核对时仍允许 capture 记录拒绝边界。上游不可达/超时、预热失败、条数或模型身份不符继续阻断。整个预检保留 `formal_accuracy_evidence=false`，不修改标签人工审核状态。

证据目录依次为 `large_weights_<UTC时间>`、`large_start_gate_<UTC时间>` 和 `large_preflight_<UTC时间>`。原始 API、冻结 revision、下载块 manifest、逐 shard 哈希、磁盘预留、软件/GPU、启动参数、接口与逐条评测都必须回收本地，并与小模型结果并列。对照时同时报告 HTTP 成功率、动作正确率、分类型结果、请求 p50/p95；HTTP 失败不得从准确率分母删除。

```powershell
scp -P <SSH端口> -r root@<AutoDL主机>:/root/autodl-tmp/sensehome-evidence _work/autodl-evidence-large
```
