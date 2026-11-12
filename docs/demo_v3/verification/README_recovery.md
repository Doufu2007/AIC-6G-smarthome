# 独立纯规划恢复适配器验收（2026-11-11）

实现：`docs/demo_v3/planning_recovery.py`。测试：`docs/demo_v3/test_planning_recovery.py`。旧 C 策略、原网关、模型适配器、提示词与标签均未修改；没有运行 AutoDL 或真实模型推理。

## 接入方式

```python
from planning_recovery import PlanningRecoveryBackend

wrapped = PlanningRecoveryBackend(
    primary_backend, secondary_backend,
    primary_name="cloud-model", secondary_name="local-model",
    primary_url="http://127.0.0.1:18101/v1",
    secondary_url="http://127.0.0.1:18102/v1",
    launch_budget_ms=120000,
)
# 将 wrapped 放入原网关 cloud-model 槽位即可；原 C 初选不变。
```

支持已有 `HttpJsonBackend.base_url` 与 `DirectModelBackend.config.upstream_url`。显式 URL 必须与实际配置一致，构造时及每次调用前检查。只接受 `http://127.0.0.1` 或 `http://[::1]`，拒绝域名、外部 IP、凭据、query、fragment 和显式启用重定向的传输。现有两种 HTTP 后端都使用不跟随重定向的 `http.client`；307 fixture 验证不会访问目的地址或备端。任意自定义 Backend 也必须遵守此传输合同，本类无法证明第三方对象内部没有其他网络行为。

## 行为边界

- `health()` 仅转发首端健康观测，不合成备端健康，不改变初选语义。
- 仅复杂任务、首端 `ok=False`、无动作且未完成、明确瞬态传输错误时考虑备端。
- 仅允许 HTTP 503/504 与明确 Timeout/ConnectionRefused/Reset 等传输异常。DirectModelBackend 的 HTTP 502 必须同时具有明确 `UPSTREAM_HTTP_503/504` 或对应传输异常码；笼统 502 不允许。
- 4xx、模型拒绝、非法 JSON、unsafe、clarification、白名单拒绝、已存在动作/完成标志均终止恢复。原响应体与 BackendResult 矛盾时按保守终止处理。
- 备端必须当次重新 `health()`，且 `ready is True`、`reachable is True`，不能用缓存或 context 指定路线替代。
- 最多调用另一个规划器一次；拒绝包装器嵌套、相同实例/端点；两端拿到相同 command 与隔离的 context 深拷贝。
- 适配器不执行设备动作。成功计划交回旧网关，再经过原动作校验和执行器一次；unsafe 计划仍由旧网关拒绝。
- `launch_budget_ms` 仅限制是否发起备端调用；健康探测和同步推理超时由实际 Backend 配置，不保证总截止时间。计时范围是适配器，未包含 C 决策或网关外部探测。

## 透明审计

`response.actual_backend`、`response.recovery.actual_backend` 与 `source` 中的实际槽位一致。`recovery` 保留首端/备端状态、原响应、原错误、即时备端健康、各段墙钟、预算与原因。备端成功时结果 error 为 None，首端失败在 `primary_error` 和 `primary_response` 中保留；未将备端成功写成首端成功。

**原网关顶层 `backend` 仍是初选槽位。** 消费方应同时显示 `selected_backend` 和实际来源。两端失败时原网关可能用规则回退结果覆盖 response，所以运行器还需保存 `wrapped.recovery_records`；它是适配器原始审计，不应改写成原网关原生字段。

## 测试记录

1. 首次 RED：23 项均因适配器尚未实现而失败，见 `recovery_red.txt`。
2. 安全自审 RED：新增矛盾失败响应测试，6 个子例失败（原响应已含动作/完成/终止码仍触发恢复），见 `recovery_conflict_red.txt`；加入终止守卫后修复。
3. 最终 GREEN：`python -m unittest discover -s docs/demo_v3 -p test_planning_recovery.py -v`，**27 项通过，5.745 秒**。
4. 旧网关相关完整回归：`python -m pytest -q SenseHome-6G/net_offload/tests`，**145 passed, 36 subtests passed，69.83 秒**。
5. 旧部署完整回归：`python -m pytest -q docs/deployment`，**29 passed，11.18 秒**。

实际 loopback HTTP 测试覆盖 503→备端健康→备端推理、422拒绝、畸形JSON、307重定向；额外用真实 DirectModelBackend 配合 HTTP fixture 验证其上游503→HTTP502包装后仍能恢复，INVALID_MODEL_JSON 不恢复。原 OffloadGateway 集成证明恢复动作只执行一次，拒绝/两端失败/unsafe 结果执行次数为0。

以上 HTTP 服务与执行器均是测试 fixture，**不是大模型准确率、真实家居设备、真实网络或 6G 空口证据**。本任务不评价正式模型矩阵，也未使用 H05 留出失败调优。

## 自审结论

本范围内未发现未解决的功能缺陷。集成仍须设置各后端合理超时、保留恢复记录，并在展示层区分原 C 初选与实际模型来源。外部试用、实物设备和真实双地网络仍需独立取得证据。
