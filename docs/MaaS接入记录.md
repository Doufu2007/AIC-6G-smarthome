# 华为云 MaaS 接入试做

日期：2026-11-24

## 接入点

MaaS 通过 v11 `ModelPlanner` 生成操作提案。设备目录、状态读取、本地候选校验、用户明确确认、持久执行、执行后读回都仍由 v11 控制；MaaS 不直接连接或控制设备。

配置模板：`src/SenseHome-6G/v11/examples/huawei-maas.routes.example.json`。

## 华为官方 API 信息

华为云 MaaS 官方文档给出的 OpenAI 兼容请求地址为 `https://api.modelarts-maas.com/openai/v1/chat/completions`，认证使用 `Authorization: Bearer <MaaS_API_Key>`。文档列出了 `openpangu-2.1-pro` 模型 ID。来源：[华为云 MaaS 模型调用 - OpenAI 兼容接口](https://support.huaweicloud.com/model-call-maas/model-call-021.html)。

该 MaaS 配置不发 `response_format` 和 `seed`：现有 v11 默认会携带这两个可选字段，官方示例没有证明所选部署对它们的支持。模型 prompt 仍要求 JSON，解析后仍进入本地护栏。若实测返回格式不符，提案拒绝或澄清，不能绕过护栏。

## 本机试调用

在 PowerShell 当前会话安全设置环境变量；不要把 API Key 写入 JSON、代码或版本库：

```powershell
$env:MAAS_API_KEY = '<从华为云控制台获取的 API Key>'
cd 'src\SenseHome-6G\v11'
python -X utf8 -B cli.py --home demo_maas init
python -X utf8 -B cli.py --home demo_maas --routes examples/huawei-maas.routes.example.json propose '打开书房阅读灯'
```

该模板显式开启 `direct_model=true`，选择单个云端 MaaS 后端，且不提供质量校准文件。后端名为 `cloud-model`，不会把云端模型标记成本地模型。直连与校准模式互斥；普通 routes 不显式开启直连时保留原有校准流程。

v11 会把模拟设备目录和状态随请求发到这个 endpoint，不会声称模型质量已校准，也不表示路由优于规则。隐私请求含“不要上传”“仅在本地”“只在本地”等时不发送模型请求。预算为 `timeout_seconds=15` 与 `deadline_ms=15000` 中较短者；实际 openPangu 部署默认可能使用深度思考，若超时需依据真实调用耗时调整预算。

响应只生成提案，核对设备、动作和参数后才可使用返回的 ID 与摘要执行 `confirm --confirm`。模型 HTTP 错误或输出解析失败时，系统可能返回规则生成的提案；必须检查 `metadata.route.reason`，`PLANNER_ERROR_RULE_FALLBACK` 不能算作华为模型成功。模板要求 `MAAS_API_KEY` 存在，缺少时在本地报错，避免误用无鉴权回退。

## 本次验证与限制

- 代码新增请求选项控制：一般后端默认仍发送原有 `seed` 和 JSON response format；MaaS 模板显式省略它们。
- 新增 localhost HTTP 测试验证 URL、Bearer 头、请求参数、v11 真实动作格式、生产 Controller 提案—确认—软件执行闭环；另覆盖本地隐私、不匹配校准、期限、鉴权失败可见性、缺密钥及非法参数。测试没有访问公网、没有使用华为凭据，也没有调用真实 MaaS 模型。
- 本次新增 13 项测试，完整 v11 回归复跑 **211/211 通过**。首轮的 `test_slow_post_does_not_block_other_entity_or_get_retried` 曾一次返回 `unknown`，该测试随后单独复跑通过，失败原因尚未稳定复现。复测命令为 `python -X utf8 -B -m unittest discover -s tests -v`，在 v11 目录运行。
- 本机未找到 `MAAS_API_KEY`、`HUAWEI_MAAS_API_KEY` 或 `SENSEHOME_MODEL_KEY` 环境变量，所以真实云端调用尚未完成。设置好 `MAAS_API_KEY` 后可用上面的命令试跑，再记录实际模型响应、模型 ID、耗时与失败情况。
- 已通过现有 HTTP 传输向官方接口发送不含密钥、用户信息或设备数据的合成测试请求。公网接口可达，返回 HTTP 400、`ModelArts.81002` / `Failed to get the authorization header.`；这只证明到了鉴权入口，未发生成功模型推理。记录在 `docs/verification/maas_endpoint_check.json`。
- 这一步只接通 MaaS 推理入口；没有修改 Home Assistant 适配器、没有接入 IoTDA，也没有把 openvela Agent 与 Python Runtime 宣称为已集成。

## 变更复核

生产代码修改集中在 `controller.py` 的模型请求参数、`cli.py` 的显式单模型直连和配置校验、`catalog_planner.py` 的本地隐私识别。相对于 AIC 当前源码的差异及 SHA-256 清单保存在 `docs/verification/maas_source_changes.diff` 与 `maas_source_hashes.json`。AIC 源目录和归档目录均未修改。
