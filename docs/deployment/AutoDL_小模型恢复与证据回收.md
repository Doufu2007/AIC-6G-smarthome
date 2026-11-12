# AutoDL 小模型恢复与证据回收

本目录是 SSH 恢复后使用的脚本与操作说明。这里只完成本地编写和验证，没有据此登录实例、补完权重或获得推理结果。远端已记录 vLLM 0.33.0、Torch 2.14.0+cu130；实际解释器为 `/root/miniconda3/bin/python`，复用系统 conda，不新建 venv、不重新安装依赖。非登录 shell 的 PATH 没有 `python`，远端命令必须使用该绝对路径。先完成小模型管线预检，再处理大模型。

## 1. 本地生成正确目录的恢复包

从项目根目录运行；输出文件不能已经存在：

```powershell
python docs/deployment/prepare_autodl_recovery.py --output _work/autodl-recovery-20261103.zip
```

压缩包只有 6 个文件，不包含密码、SSH 配置、原始实验结果或任何 reviewed 标签：

```text
autodl_small_recovery.py
eval_model.py
backends/openai_backend.py
backends/device_actions.py
backends/target_contract.py
backends/eval_commands.jsonl
bundle-manifest.json
```

`eval_model.py` 导入 `backends.openai_backend`，所以不能把所有文件平铺到同一层。`device_actions.py` 与 wrapper 放在 `backends/` 同目录，复用 Dashboard 的当前白名单。打包器验证全部标签仍为 `human_reviewed=false`，并保存逐文件 SHA256。

## 2. SSH 恢复后上传与解压

使用 AutoDL 控制台给出的实际 SSH 地址/端口，在密码提示中交互输入凭据；不要把密码放在命令或文件中。下列占位符必须替换：

```powershell
scp -P <SSH端口> _work/autodl-recovery-20261103.zip root@<AutoDL主机>:/root/autodl-tmp/
ssh -p <SSH端口> root@<AutoDL主机>
```

远端解压到新的目录，避免覆盖已有实验资产：

```bash
mkdir -p /root/autodl-tmp/sensehome-adapter-recovery
/root/miniconda3/bin/python -m zipfile -e /root/autodl-tmp/autodl-recovery-20261103.zip /root/autodl-tmp/sensehome-adapter-recovery
cd /root/autodl-tmp/sensehome-adapter-recovery
/root/miniconda3/bin/python -c 'import hashlib,json,pathlib; p=pathlib.Path("."); m=json.loads((p/"bundle-manifest.json").read_text()); assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==s for n,s in m["files"].items()); print("bundle_sha256_ok")'
```

若该恢复目录已存在，先改用新的目录名再解压。旧 `/root/autodl-tmp/sensehome-adapter/` 中机器生成的 `eval_commands.reviewed.jsonl` **不是人工核对数据，不可用于正式评测**。预检脚本会把旧目录、当前恢复目录及各自 `backends/` 下名称含 `reviewed` 的 JSONL 原样移入本次证据的隔离目录，不会将任何行改为 `human_reviewed=true`。本包只用显式的草案文件做预检。若旧目录另有实际位置，以 `--legacy-adapter-dir <旧目录>` 明确指定。

## 3. 恢复分片并验证完整快照

先确认旧下载进程已经停止，避免正在写的分片被同时读取。不要再次运行旧 `cat part_*` 命令，它会把 `.log` 等文件拼进去。

```bash
/root/miniconda3/bin/python autodl_small_recovery.py weights
```

脚本固定 Qwen2.5-1.5B-Instruct 的待核对 revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`。它先从镜像 API 与 immutable config 的 `x-repo-commit` 重新核对 revision，再核对权重 LFS 对象的大小和 SHA256。commit header 可来自 302/307 重定向过程或最终响应，脚本只保存选定的 commit/status，不保存带签名的 CDN URL；任意已观察 commit 不一致都会停止。如果镜像不能提供完整证明，脚本也停止，不猜测 revision。

权重总大小应为 `3087467144` 字节；`part_00` 至 `part_14` 各 `192966697` 字节，`part_15` 为 `192966689` 字节。上次未完成的是 00 和 12，但脚本会重新检查全部 16 片，**只下载缺失或字节数不符的分片**。已有短分片会先复制到 `.download`，只补缺失尾部，完整替换前保留原片。下载必须返回精确的 HTTP 206 `Content-Range` 且不压缩；每次 Range 请求使用独立 query，降低镜像/CDN 重用错误范围缓存的可能。脚本有明确 UA、90 秒 socket timeout、每次分片 900 秒预算，最多重试 3 次网络/短传输/部分 5xx 错误；短传输会从已保存位置继续。Range 不符和最终哈希不符直接阻断。已存在且大小正确的分片复用，最终通过完整 SHA256 一次验证其内容。

拼接严格按 `part_00`、`part_01`、…、`part_15`，写入临时 `.assembling` 文件。总大小和以下 SHA256 都匹配后，才替换 `model.safetensors`：

```text
dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee
```

若大小正确但内容损坏，完整哈希会阻断启动；脚本不会盲目重下全部 16 片。保留失败文件并按证据定位或重新获取有问题的分片。配置与 tokenizer 文件以该 revision 的 Git blob ID 验证，合格现有文件也复用；已完成且哈希正确的整权重直接复用。成功后生成 `models/small/sensehome-verified-snapshot.json` 与独立证据目录。

## 4. 复用已装 vLLM，执行草案预检

使用实例已安装 vLLM 的系统 conda 解释器运行：

```bash
/root/miniconda3/bin/python autodl_small_recovery.py preflight --repeat 1
```

脚本会重新验证快照和草案标签，确认 8001/18092 空闲，保存 GPU、软件版本和源码哈希，然后依次启动 vLLM 和 wrapper。vLLM 参数为：小模型、`127.0.0.1:8001`、`max-model-len=4096`、显存预算 0.9、种子 20261103；wrapper 仅绑定 `127.0.0.1:18092`，上游 timeout 60 秒，模型身份固定为 `sensehome-small` 与核对后的 revision。

之后实测 `/v1/models`、`/probe`、`/health`、一次预热 `/infer`，再执行：

```text
eval_model.py --url http://127.0.0.1:18092 --cases backends/eval_commands.jsonl --out <本次证据目录>/eval.draft.json --repeat 1 --allow-draft-labels
```

每个请求保留实际动作、HTTP 状态、来源、模型身份、模型墙钟与端到端请求时延。预热遇到 HTTP 错误也保存错误 JSON。回环请求显式绕过系统代理，模型启动禁用联网权重获取和使用量遥测。脚本检查来源是 `real-model`，但**草案标签对应的准确率不是正式准确率**。预检不改变任何物理设备，也不证明家庭网关、双模型实时切换或真实 6G 空口。

如需执行可选的 C/网关 capture，添加 `--gateway-capture-script <已上传的capture_real_gateway.py>`；云档位还必须显式添加 `--gateway-capture-inject-lan-ok`，这会将 LAN 可用作为注入夹具并在证据中标注，不能当作物理 LAN 测量。网关拒绝等 HTTP 错误会保留并以 `completed_with_interface_errors` 结束，不能从准确率分母中删除。

完成或失败后，脚本只停止自己启动的 wrapper/vLLM 进程组，保留 stdout/stderr。端口已被占用时会停止，不会杀掉既有服务。大模型应在小模型进程退出后另做，不在本恢复脚本范围内。脚本启动等待上限 1200 秒，每 2 秒检查，不把启动耗时写入模型推理时延。

## 5. 必须回收本地原始证据

证据在 `/root/autodl-tmp/sensehome-evidence/weights_<UTC时间>/` 和 `small_preflight_<UTC时间>/`。包含快照 API/commit header、逐文件哈希、`nvidia-smi`、软件版本、启动参数、vLLM/wrapper 日志、实际接口 JSON 和 `eval.draft.json`。失败也保存 `status.json`，不得删除失败后只展示成功运行。

在本地运行：

```powershell
scp -P <SSH端口> -r root@<AutoDL主机>:/root/autodl-tmp/sensehome-evidence _work/autodl-evidence-20261103
```

确认本地目录存在且逐条结果可读，再终止租用或清理远端。不要仅保留截图/汇总表；模型服务未成功、下载未完成、SSH 仍拒绝连接时，应如实保留“未获得真实推理结果”。正式评测需参赛者之后逐条人工复核并冻结独立 reviewed 标签、记录核对人/日期/版本，再移除 `--allow-draft-labels` 重跑；本脚本不会替人作出该声明。
