param(
    [switch]$SkipPythonTests
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)
$asciiBase = if (Test-Path "C:\temp") { "C:\temp" } else { "C:\Windows\Temp" }
$asciiTemp = Join-Path $asciiBase "sensehome_ascii_temp"
New-Item -ItemType Directory -Force -Path $asciiTemp | Out-Null
$env:TEMP = $asciiTemp
$env:TMP = $asciiTemp
$env:PYTHONPATH = $scriptRoot

Push-Location $scriptRoot
try {
    & gcc -std=c99 -Wall -Wextra -O2 -o policy_cli.exe policy_cli.c offload_policy.c
    if ($LASTEXITCODE -ne 0) { throw "policy_cli build failed" }
    & gcc -std=c99 -Wall -Wextra -O2 -o test_offload_policy.exe test_offload_policy.c offload_policy.c
    if ($LASTEXITCODE -ne 0) { throw "policy test build failed" }
    & gcc -std=c99 -Wall -Wextra -O2 -o test_compute_policy.exe test_compute_policy.c offload_policy.c
    if ($LASTEXITCODE -ne 0) { throw "compute test build failed" }

    & .\test_offload_policy.exe
    if ($LASTEXITCODE -ne 0) { throw "policy tests failed" }
    & .\test_compute_policy.exe
    if ($LASTEXITCODE -ne 0) { throw "compute tests failed" }
    if (-not $SkipPythonTests) {
        & python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "net_offload Python tests failed" }
        & python -m unittest discover -s (Join-Path $repoRoot "openvela_smart_home/dashboard/tests") -v
        if ($LASTEXITCODE -ne 0) { throw "dashboard tests failed" }
    }

    $commit = (& git -C $repoRoot rev-parse HEAD 2>$null)
    if (-not $commit) { $commit = "unknown" }
    $gccVersion = (& gcc --version | Select-Object -First 1)
    $pythonVersion = (& python --version 2>&1)
    $sourceFiles = @(
        Get-ChildItem -LiteralPath $scriptRoot -File | Where-Object { $_.Extension -in '.c', '.h', '.py', '.ps1' }
        Get-ChildItem -LiteralPath (Join-Path $scriptRoot 'backends') -File -Filter '*.py'
        Get-ChildItem -LiteralPath (Join-Path $scriptRoot 'tests') -File -Filter '*.py'
        Get-ChildItem -LiteralPath (Join-Path $repoRoot 'openvela_smart_home/dashboard') -File | Where-Object { $_.Extension -in '.py', '.html' }
        Get-ChildItem -LiteralPath (Join-Path $repoRoot 'openvela_smart_home/dashboard/tests') -File -Filter '*.py'
    )
    $sources = @($sourceFiles | Sort-Object FullName | ForEach-Object {
        [ordered]@{ path = $_.FullName.Substring($repoRoot.Length + 1).Replace('\', '/'); sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    })
    $manifest = [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        commit = [string]$commit
        os = [System.Environment]::OSVersion.VersionString
        compiler = [string]$gccVersion
        python = [string]$pythonVersion
        working_tree_dirty = [bool](& git -C $repoRoot status --porcelain --untracked-files=normal 2>$null)
        python_tests_run = -not [bool]$SkipPythonTests
        sources = $sources
        artifacts = @(
            [ordered]@{ path = "policy_cli.exe"; sha256 = (Get-FileHash .\policy_cli.exe -Algorithm SHA256).Hash },
            [ordered]@{ path = "test_offload_policy.exe"; sha256 = (Get-FileHash .\test_offload_policy.exe -Algorithm SHA256).Hash },
            [ordered]@{ path = "test_compute_policy.exe"; sha256 = (Get-FileHash .\test_compute_policy.exe -Algorithm SHA256).Hash }
        )
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText((Join-Path $scriptRoot 'build_manifest.json'), $manifestJson, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "build_manifest.json written"
}
finally {
    Pop-Location
}
