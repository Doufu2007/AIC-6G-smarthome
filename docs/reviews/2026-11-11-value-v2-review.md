# 2026-11-11 value_v2 独立只读审查

审查员未编写本次value_v2实现；仅阅读代码和已保存结果、执行针对性测试/离线复算及临时本机HTTP夹具。未修改value_v2代码、旧实验、账本或原始数据；未发送模型推理、访问第三方或停止任何模型服务。本文仅列Critical/Important发现。

## 结论

发现1项Important（I1），无Critical。实际用量汇总、未知成本传播、报价/实际分离、币种/分摊比例与凭据路径/哈希在已核范围内未发现Critical/Important。I1修复前，不应把检查器的成功GET自动解释为来自声明的本机模型。

## I1 Important：loopback探测会被环境HTTP代理或自动重定向改变实际目标

位置：`docs/value_v2/deployment_check.py:37`附近，`urllib.request.urlopen(url.rstrip('/')+'/models', timeout=3)`。检查器只在调用前验证输入URL是回环地址，默认urllib仍使用环境/系统代理并跟随HTTP重定向。结果没有记录实际连接对端，也没有验证重定向后的目标，因而不能保证返回的模型清单来自声明的本机服务。

实际本机复现（两个临时HTTP夹具，不触碰真实模型）：

1. 目标服务监听`127.0.0.1:62509`，正常应返回`real-target-model`并记录收到的请求。
2. 另一个本机HTTP代理返回`proxy-supplied-model`。仅对子进程设置`http_proxy`和`HTTP_PROXY`指向该临时代理，清空`no_proxy`/`NO_PROXY`；配置local/cloud都指向目标服务`/v1`。
3. 运行`python docs/value_v2/deployment_check.py --config <临时配置> --probe-loopback`。
4. 实际结果：退出码0，报告local/cloud均HTTP200、模型`proxy-supplied-model`；**目标服务收到0个请求，代理收到2个绝对URL GET**（`http://127.0.0.1:62509/v1/models`）。被声明为loopback检查的响应实际由代理提供。

影响：真实目标未被探测也能产生“200+模型清单”，会使部署证据的来源失真。即使当前保存的旧端口检查最后已由直接http.client诊断确认拒绝连接，通用检查器仍存在此缺陷。默认自动重定向还可能在回环输入后访问其他主机或非`/models`路径；本审查未为证明该风险访问任何外部目标。

修复建议：回环探测禁用环境/系统代理，禁用重定向或逐跳验证scheme、hostname及限定路径；记录每次实际请求URL与响应状态。用本机目标+代理夹具验证环境代理不能截走请求，再以本机302夹具验证重定向被拒绝。不能只在文档中增加“请配置NO_PROXY”而继续给默认路径背书。

## 已通过的独立复核

- `python -m pytest -q docs/value_v2/test_evidence_tools.py docs/value/test_cost_calculator.py`：16项、9个子测试通过。
- 四个原始report SHA256与usage来源清单一致；独立提取415个原始POST，415个模型response ID全部存在且互异，没有重复ID。没有重复读取adapter的usage副本或GET。
- 逐调用输出`usage_calls.jsonl`与独立提取结果完全相同；分模型、分矩阵摘要重算一致。服务端报告prompt tokens合计246502、completion tokens合计29277；这些是调用量追溯，不是账单、质量或节能结论。
- 缺失usage保持null；已知小计与全量总计分开，未把未知token数量补0。当前415条usage均完整，无需按缺失例推测原始量。
- verification.json中记录的全部文件字节哈希均与当前文件一致。
- 当前账本的直接模型API分项0被明确限制为旧四矩阵已知本机llama.cpp端点，不含历史AutoDL、其他会话或整机运行成本；实际总成本和情景总额均保持null。
- `cost_ledger.py`要求五类各一项、同币种、有限非负金额、分摊比例[0,1]（拒绝bool）、actual条目的工作区内文件与匹配SHA256；报价和假设不能生成actual_total。文件哈希仅证明完整性，材料已明确不证明发票真实性或分摊公平。
- 静态部署检查始终保持`two_host_deployment_verified=False`，没有因不同URL或hostname就认定独立硬件/真实WAN。

## 审查范围限制

没有核实真实采购、电量、电价、维护账单、远端主机身份或任何收费提供商账户；当前材料没有这类实际凭据。没有把单机服务、程序标签或公开背景资料视为独立云边/用户需求证据。修复后的代码与结果需按实际新文件重新确定适用范围。

## I1 修复跟进（实现者复验）

上述发现与原始复现记录保留。后续实现已改为`http.client`直连字面量回环IP，不使用环境/系统代理、不解析`localhost`，并拒绝HTTP 3xx重定向。结果记录`requested_url`、`connected_peer`、HTTP状态与`direct-http.client-no-proxy-no-redirect`传输方式；原始历史探测结果未覆盖。

两项实际HTTP夹具测试先在旧实现下失败，再在修复后通过；完整工具与原成本测试合计19项、12个子测试通过。环境代理夹具收到0请求，声明目标收到2次`/v1/models`；302夹具原端收到2请求，重定向目标收到0请求，返回明确拒绝。记录见`docs/value_v2/results/deployment_probe_fix_verification.json`、`verification/deployment_probe_red_green.md`及测试日志。本节是实现者复验，不能替代后续独立审查结论。

另直接从四份原始report重算415 POST，逐调用位置及token与`usage_calls.jsonl`完全匹配；415个response ID互异，输入246502、输出29277。封存索引哈希全部匹配，0新推理；结果见`docs/value_v2/results/usage_independent_recheck.json`。实际总成本仍为null。
