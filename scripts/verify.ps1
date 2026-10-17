param([string]$Python = ".\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repositoryRoot
try {
    $env:PYTHONUTF8 = '1'
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
    if (-not $env:TEST_DATABASE_URL) { Write-Host 'PostgreSQL integration: skipped (TEST_DATABASE_URL not set)' }
    & $Python -m pytest agent/tests -q
    if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
    Push-Location web
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'web build failed' }
    } finally { Pop-Location }
    Write-Host 'Configured checks passed'
} finally { Pop-Location }
