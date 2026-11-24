# 开发者独立试用

试用环境：Python 3.10+，普通电脑即可。默认仅使用本机软件状态，不需要GPU、Home Assistant、网络端点或API Key。整套基本流程约需10—20分钟。

## 运行一次

1. 解压试用包，在项目根目录打开终端，再进入 `SenseHome-6G/v11`。
2. 记录试用开始时间、操作系统、Python版本、你准备完成的低风险任务、成功条件和原有做法。
3. 初始化本机状态：

```powershell
python -X utf8 -B cli.py --home trial_home init
```

4. 可编辑 `trial_home/catalog.json` 中的显示名称或别名以匹配你的任务；不要改 `entity_id`。提出一个请求：

```powershell
python -X utf8 -B cli.py --home trial_home propose "打开阅读灯"
```

5. 检查返回的所有对象、动作和参数是否满足你预先写下的成功条件。只在确认无误时，将下面命令中的 `<本次proposal_id>` 与 `<本次digest>` 替换为刚才输出的真实值；不要原样输入尖括号占位符：

```powershell
python -X utf8 -B cli.py --home trial_home confirm --proposal-id <本次proposal_id> --digest <本次digest> --request-id trial-001 --confirm
python -X utf8 -B cli.py --home trial_home receipt trial-001
python -X utf8 -B cli.py --home trial_home status
```

如果结果是澄清、拒绝或失败，不要改写成成功；记录完整错误类型和你采取的处理。CLI操作的是软件状态，不会控制真实家电。

## 比较与回传

- 用相同任务、成功条件和可用资料，尝试你原有的薄脚本、HA原生automation/script或其他现成做法，分别记录从开始到满足条件的总耗时。没有HA或旧脚本也可以，注明“不适用”。
- 仅运行CLI算独立试用；实际改动代码并接入自己的任务才算外部接入。请区分两者，失败或选择不继续也有价值。
- 若隔一天后你主动增加第二个真实场景，再记录改动文件和耗时；如果没有发生，填写“未观察到次日复用”。
- 填写压缩包根目录的 `TRIAL_RETURN_TEMPLATE.md`。可匿名，只回传你同意分享的代码差异和脱敏日志摘要。不要回传 `trial_home` 数据库、未审查的完整日志或任何凭据。
- 记录包哈希时，在ZIP实际保存目录打开终端，或把ZIP实际路径填入命令；不要在 `SenseHome-6G/v11` 子目录下直接使用相对路径：

```powershell
Get-FileHash -LiteralPath "<ZIP实际路径>" -Algorithm SHA256
```

macOS/Linux：

```sh
sha256sum "<ZIP实际路径>"
```

若自愿连接自己的Home Assistant测试环境，只使用低风险实体；令牌仅保存在参与者自己的环境变量，绝不回传。回传前删除密钥、地址、真实设备名称、个人请求和URL凭据。需分别注明软件状态、HTTP fixture、官方helper、原生HA或物理设备。

没有参与者原始回传，就不填写外部试用、采用或次日复用结果。该试用流程不会证明真实模型优越性、双节点链路或6G测试床表现。
