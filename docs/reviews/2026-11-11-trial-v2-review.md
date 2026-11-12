# 本机试用采集器独立只读审查

审查范围：`docs/trial_v2/collector.py`、`index.html`、`test_collector.py`、`smoke_http.py`与README。未修改这些文件、未改生产网关、未访问真实设备、未创建参与者数据，也未发真实模型推理。审查所用状态均为内存构造或临时目录测试夹具。

## Important：状态接口失败被计为空目标“无动作且状态不变”

`TrialStore.execute`保留了前后`/api/state`的完整HTTP结果，但将其`body`直接传入`assess_response`。后者在空目标路径只检查两个对象非空、相等且无动作，没有验证状态GET的成功状态、error或设备状态结构。

独立纯函数复现：

```python
assess_response(
    503,
    {'error': 'unavailable', 'actions': []},
    {'error': 'state_unavailable'},
    {'error': 'state_unavailable'},
    [],
)
```

实际返回`empty_target_no_action_no_mutation=True`。这里没有取得任何可用设备状态，不能据两个相同错误对象证明未变更设备。真实连接中T4等空目标可能因此生成错误的安全/状态证据。它没有把该请求计成非空任务完成，但“无变化”的独立指标仍错误。

修复建议：只有前后两次状态GET均200、无传输/业务error且含预期设备状态结构时才作为有效状态参与评分；缺失状态应把该证据标为unknown/未确认。给HTTP fixture增加状态GET 500返回相同错误对象的回归，并确认空目标不通过未变更状态验证。原始HTTP错误仍保留在试用记录中。已报告主线处理，审查员未代改。

## 其他本次关键检查

- 服务固定绑定127.0.0.1，网关URL只接受字面回环地址；源站、路径、凭据与query验证存在，系统代理与重定向被关闭。
- 写入API同时检查Host、Origin和页面token；HTTP提交还要求`confirm_submit=true`。请求路径固定，无任意文件路由。
- 不同意不创建会话，最终提交前仅内存保存；取消/撤回会移除对应内存及已提交文件。未把测试调用伪称真实人类。
- participant、developer_self_test和automation_test分目录计数；operator声明队列及匿名身份不可自动验证的限制已明示。有效接入试用分母保留失败尝试，未冒称成功率。
- 自拟任务没有参考oracle，完成值为null；预设非空目标检查动作集合和状态，HTTP200或模型文本不能单独算完成。
- JSON导出保留回答内容，CSV对公式起始符做文本处理。前后空白由`text_value.strip()`修剪，当前“原话”指保留正文内容，不能用于声称字节级原始输入归档。

现有11项行为测试通过；这不覆盖上述状态GET失败情形。无额外Critical发现。最终是否修复以实现者的新测试和主线验收为准，本记录不自动背书随后版本。

## 修后复核（2026-11-11）

上述 Important 已修复。`TrialStore.execute` 现先通过 `validated_state_response` 验证状态HTTP200、无传输或业务错误，以及非空lights/curtains/temperature结构和预期字段类型/范围，只有有效状态才传入评分器；缺失或无效状态保留原始响应，但未变更证据为null。`test_state_http_errors_cannot_confirm_empty_target_no_mutation` 使用真实本机HTTP503夹具覆盖了相同错误body，断言null及无执行确认，现通过。原始失败响应没有被丢弃。

同时复核新增 `run_isolated_gateway.py`：仅回环地址，状态/事件落在新建 `_work/trial_v2/` 子目录；导入生产桥接后仅覆盖进程内配置，空模型URL、关闭监视与巡检，复用原C策略、规则和执行器，不为预设增加成功分支。`test_isolated_gateway.py` 通过真实子进程与HTTP验证T1规则成功、未配置模型的T2保留失败、巡检路由404、结束后原状态/事件哈希一致。

复核结果：13项测试及6个子测试通过，HTTP smoke通过。测试均为自动化夹具或隔离模拟操作，外部实际反馈仍为0；尚待主线浏览验收，不把本记录当作真实人类试用证明。已消除本记录指出的 Important，本次无新增Critical/Important发现。
