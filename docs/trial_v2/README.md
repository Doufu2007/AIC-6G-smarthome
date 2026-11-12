# 本机匿名试用采集器

此工具补齐“可实际组织试用的入口”，不代替真实反馈。当前外部参与者反馈 **0条**。自动化验收记录单独存放，不计入用户研究。旧生产网关、标签和600条封存记录未改动。

## 一步启动

在项目根目录执行以下命令，可同时启动**隔离PC网关和采集页**：

```powershell
python docs/trial_v2/run_isolated_gateway.py --with-collector
```

打开 `http://127.0.0.1:8877`。采集器默认为 `developer_self_test`，网关固定在 `127.0.0.1:8878`；原生产网关和模型服务不会被启动、停止或改写。每次运行在 `_work/trial_v2/` 下创建新目录保存模拟状态、路由事件和运行清单，主动巡检关闭，只开放状态读取与命令接口。按 `Ctrl+C` 停止本次服务，结束时核对原状态/事件文件哈希。

此入口复用生产C策略、规则后端与白名单执行器，**不配置模型端点**。T1开客厅灯可以由既有规则完成；T2跨房间调光需要模型而当前未接入，因此应保留失败结果。该失败不是试用工具自动修复或预设成功的对象。当前页面是PC模拟试用入口，不代表完整模型能力演示。

自动化浏览验收必须单独指定模式：

```powershell
python docs/trial_v2/run_isolated_gateway.py --with-collector --record-kind automation_test
```

愿意参与的外部使用者实际到这台电脑试用时，操作者可改为 `--record-kind participant`；由对方自行阅读同意和决定保存，不把开发者或自动化操作计成外部反馈。

若只需未接网关的输入练习，执行：

```powershell
python docs/trial_v2/collector.py
```

默认是 `developer_self_test`，没有配置网关时仅练习输入，不执行动作、不计有效技术试用。服务固定绑定本机回环地址，不开公网端口，不启动或停止已有模型/网关。上述两种入口使用同一端口，不要同时启动。

网关已由操作者确认连接**隔离PC模拟设备、关闭主动巡检**后，实际外部使用者自愿来这台电脑试用时运行：

```powershell
python docs/trial_v2/collector.py --record-kind participant --gateway http://127.0.0.1:8080
```

端口8080只是现有Dashboard默认地址；没有运行就保持失败，不自动用演示成功结果补齐。只接受字面量 `127.0.0.1` 或 `::1` 的HTTP源站，固定调用 `/api/state` 与 `/api/command`，不跟随重定向、不使用系统代理。不要把地址改为真实家电控制系统。

## 一次真实试用怎么做

1. 由你本人当面邀请愿意尝试的人，说明只操作电脑中的模拟灯/窗帘，可能失败、可随时停止。工具不会自动联系任何人。
2. 对方自行阅读并勾选同意；原话公开引用默认未勾选，不记录姓名、联系方式、IP或地址。开发者参与时选择“项目开发者自测”，不要计成外部使用者。
3. 让对方先输入自己的命令；若需要演示，可以选T1/T2/T4预设练习，记录中会保留 `origin`。任务失败也保留。当前工具不提供伪装WAN故障按钮，没有实际软件条件就跳过原试用表T3。
4. 对方按自己的理解回答，可以不知道、想不起来、不愿回答，也可以只填一个问题。不要替对方填写好评。
5. 最后由对方点击“确认保存匿名记录”才写入磁盘；取消会丢弃内存记录。页面会话仍有效时，“撤回并删除本次记录”删除已保存文件。结束并回到开始页后，浏览器不再持有匿名编号；操作者可按对方保留的匿名编号删除文件。

原五分钟问题及人工理解核对规则见 `docs/value/five_minute_trial.md`。不要把愿意再试写成愿意购买。对方未允许引用原话时，只可在本机查看和做匿名聚合，未经另行同意不将原话放入公开参赛附件。

## 数据和验收口径

- 默认存 `docs/trial_v2/data/<record_kind>/<32位随机编号>.json`。未最终提交的内容只在进程内存，服务关闭后消失。
- `participant_feedback_count`仅计启动为participant、本人同意且明确提交、身份自报为user/integrator、有至少一条非空原话的记录。开发者和automation_test单列。
- `valid_connected_trial_count`另要求至少一次实际本机网关尝试；网关超时或返回失败也会进入会话，不能为好看而排除。仅练习输入不进入此分母。
- 无身份认证，匿名提交本身无法证明真实人类；计数仍需操作者核对实际招募过程，不将脚本调用改名为真实用户。
- 自拟任务没有冻结参考目标，自动 `task_completed=null`。预设任务核对完整动作集合、前后模拟状态、网关执行确认与错误；HTTP200、模型说“完成”、初始目标恰好满足都不单独算完成。T4空目标只记录无动作/状态不变，不计非空任务完成。
- 记录中的 `total_user_ms`包括前状态读取、命令与后状态读取，是采集工具的总墙钟；不能替换成模型时延或物理执行耗时。原始HTTP状态、网关文本和状态分别保存。
- 每次任务带预设/自拟来源，练习/实际HTTP模式，全部不等于物理设备、独立云边、真实家庭或6G空口试点。

JSON保留原话，不清洗改写。CSV导出把开头可能被表格解释为公式的 `= + - @` 转成文本，防止打开导出表时执行公式。导出可从本机浏览器开发者工具执行同源API（页面内部token已自动注入），或在Python中调用：

```python
from pathlib import Path
import sys
sys.path.insert(0, 'docs/trial_v2')
from collector import TrialStore
store = TrialStore(Path('docs/trial_v2/data'))
Path('trial_export.json').write_text(store.export_json(), encoding='utf-8')
Path('trial_export.csv').write_text(store.export_csv(), encoding='utf-8-sig')
```

这里读取所有三类目录并分别标注，不覆盖原件。文件是本地明文，应仅由操作者保管；不把data目录、导出表或原话上传GitHub/赛事，除非取得对应公开授权。

## 验证

```powershell
python -m pytest -q docs/trial_v2/test_collector.py
python -m pytest -q docs/trial_v2/test_isolated_gateway.py
python docs/trial_v2/smoke_http.py
```

测试覆盖同意前不保存、撤回、自动化不混入真实计数、练习不算执行、部分动作不算完整完成、外部URL/路径拒绝、文本限制和CSV公式转义。新增状态GET返回503的真实HTTP夹具要求未变更状态证据为null，不能据两个相同错误对象证明设备不变。隔离启动器的真实子进程/HTTP测试确认T1成功、无模型T2失败、巡检路由关闭、原状态/事件哈希不变。HTTP smoke使用本机测试夹具，明确automation_test，不是生产模型实验或真实用户反馈。
