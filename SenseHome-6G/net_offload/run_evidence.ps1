param(
    [string]$Out = "",
    [int]$InferN = 6,
    [int]$TraceSeed = 20261103,
    [switch]$SkipTransition
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Out) { $Out = Join-Path $scriptRoot "results" }
# Resolve the caller's relative output before entering the script directory.
$Out = [System.IO.Path]::GetFullPath($Out)
$asciiBase = if (Test-Path "C:\temp") { "C:\temp" } else { "C:\Windows\Temp" }
$asciiTemp = Join-Path $asciiBase "sensehome_ascii_temp"
New-Item -ItemType Directory -Force -Path $asciiTemp | Out-Null
$env:TEMP = $asciiTemp
$env:TMP = $asciiTemp
$env:PYTHONPATH = $scriptRoot

& (Join-Path $scriptRoot "build.ps1")
if ($LASTEXITCODE -ne 0) { throw "build failed" }

Push-Location $scriptRoot
try {
    $benchArgs = @("offload_bench.py", "--out", $Out, "--cli", (Join-Path $scriptRoot "policy_cli.exe"), "--infer-n", $InferN, "--trace-seed", $TraceSeed)
    if ($SkipTransition) { $benchArgs += "--skip-transition" }
    & python @benchArgs
    if ($LASTEXITCODE -ne 0) { throw "evidence benchmark failed" }

    Copy-Item -LiteralPath (Join-Path $scriptRoot 'build_manifest.json') -Destination (Join-Path $Out 'build_manifest.json') -Force

    $env:RESULTS_JSON = Join-Path $Out "results.json"
    & python -c "import json, os, sys; sys.path.insert(0, os.getcwd()); from evidence import validate_results_schema; data=json.load(open(os.environ['RESULTS_JSON'], encoding='utf-8')); errors=validate_results_schema(data); print('schema valid' if not errors else '\n'.join(errors)); raise SystemExit(0 if not errors else 3)"
    if ($LASTEXITCODE -ne 0) { throw "evidence schema validation failed" }
    Write-Output "Evidence written to $Out"
}
finally {
    Pop-Location
}
