# 部署探测 I1 修复验证

## RED（修改实现之前）

运行 `python -m pytest -q docs/value_v2/test_evidence_tools.py -k loopback_probe`，结果 2 failed、9 deselected。

- 环境代理夹具测试：代理收到两次绝对 URL GET，目标服务未收到预期两次 `/v1/models`，触发 `Loopback inventory request reached environment proxy`。
- 重定向夹具测试：原实现受环境/系统代理影响，原始回环服务没有收到请求，不能证明请求源头或重定向限制，测试失败。该 RED 本身不作为成功跟随重定向的证据。

## 修复

`deployment_check.py`使用标准库`http.client.HTTPConnection/HTTPSConnection`直连，忽略环境/系统代理。探测入口须为字面量回环 IP，避免名称解析改变目标；HTTP 3xx 记录`HTTP_REDIRECT_REFUSED`，不访问 Location。记录请求 URL、状态与实际连接对端，非200不解析为模型清单。静态配置检查与独立硬件验收边界保持不变。

## GREEN（修改实现之后）

完整命令：`python -m pytest -q docs/value_v2/test_evidence_tools.py docs/value/test_cost_calculator.py`。

结果：19 passed、12 subtests passed，实际输出见`tests_after_deployment_fix.log`。

额外临时 HTTP 夹具实跑记录：`../results/deployment_probe_fix_verification.json`。

- 环境代理全部设为另一个回环夹具且 NO_PROXY 清空；声明目标收到2次`/v1/models`，代理收到0次，返回声明目标的模型 ID。
- 原始服务返回302并指向另一个回环夹具；原始服务收到2次请求，重定向目标收到0次，两个槽位都记录302和`HTTP_REDIRECT_REFUSED`。
- 拒绝`localhost`及非回环名称/地址的探测；没有为验证访问任何外部主机。

这些测试使用临时模拟模型清单，不调用真实模型，不证明独立两主机部署或业务任务成功。旧探测失败文件保留为历史记录，不替换为新实现成功记录。
