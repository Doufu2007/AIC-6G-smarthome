$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root\..

Write-Host "[1/3] 运行 AI+6G runtime 单元测试"
python -m unittest discover -s SenseHome-6G/ai6g_runtime/tests -p "test_*.py" -v

Write-Host "[2/3] 运行离线冻结任务基线"
$out = "SenseHome-6G/ai6g_runtime/evidence/demo.json"
python SenseHome-6G/ai6g_runtime/benchmark.py --tasks SenseHome-6G/ai6g_runtime/tasks.jsonl --out $out

Write-Host "[3/3] 真实模型配置提示"
if ($env:AI6G_LOCAL_URL -or $env:AI6G_CLOUD_URL) {
    Write-Host "检测到模型端点环境变量；启动 HTTP 服务请另开终端运行："
    Write-Host "python SenseHome-6G/ai6g_runtime/serve.py --port 8090"
} else {
    Write-Host "当前未配置 AI6G_LOCAL_URL/AI6G_CLOUD_URL；本次结果仅覆盖离线规则路径。"
}
Write-Host "证据文件：$out"
