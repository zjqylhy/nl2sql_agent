param(
    [string]$CaFile = '',
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $CaFile) { $CaFile = Join-Path $taskRoot '.cache/gateway-server-ca.pem' }
$resolvedCa = (Resolve-Path -LiteralPath $CaFile).Path
$gatewayKey = [Environment]::GetEnvironmentVariable('NL2SQL_AGENT_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($gatewayKey)) { throw 'Missing NL2SQL_AGENT_API_KEY in Windows user environment' }
$previousKey = $env:NL2SQL_AGENT_API_KEY
$previousCa = $env:GATEWAY_CA_FILE
try {
    # 已运行的 PowerShell 不会自动继承新增用户变量，每次切换重新读取。
    $env:NL2SQL_AGENT_API_KEY = $gatewayKey
    $env:GATEWAY_CA_FILE = $resolvedCa
    $composeArgs = @('compose', '--project-directory', $taskRoot, '--env-file', (Join-Path $taskRoot '.env'),
        '-f', (Join-Path $taskRoot 'compose.yaml'), '-f', (Join-Path $taskRoot 'deploy/compose.gateway-local.yaml'))
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Gateway Compose configuration is invalid' }
    $startArgs = @('up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '120')
    if ($Build) { $startArgs += '--build' }
    $startArgs += @('api', 'worker')
    & docker @composeArgs @startArgs
    if ($LASTEXITCODE -ne 0) { throw 'Gateway API/Worker activation failed' }
} finally {
    $env:NL2SQL_AGENT_API_KEY = $previousKey
    $env:GATEWAY_CA_FILE = $previousCa
    $gatewayKey = $null
}
