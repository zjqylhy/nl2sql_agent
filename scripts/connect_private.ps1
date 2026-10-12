param(
    [string]$KeyFile = "$env:USERPROFILE\Downloads\aliyun_sk.pem",
    [string]$KnownHostsFile = "$PSScriptRoot\..\.cache\cloud\ecs-known-hosts"
)
$ErrorActionPreference = 'Stop'
if (Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction SilentlyContinue) {
    throw '本机 8088 已被占用，请先停止旧的本地入口或已有隧道。'
}
$keySource = (Resolve-Path -LiteralPath $KeyFile).Path
$knownHosts = (Resolve-Path -LiteralPath $KnownHostsFile).Path
$directory = Join-Path $env:TEMP ('nl2sql-access-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $directory | Out-Null
$acl = Get-Acl -LiteralPath $directory
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $directory -AclObject $acl
$temporaryKey = Join-Path $directory 'ecs.pem'
$process = $null
try {
    # OpenSSH 拒绝权限过宽的下载文件；只使用受限副本，不改原私钥。
    Copy-Item -LiteralPath $keySource -Destination $temporaryKey
    $start = [Diagnostics.ProcessStartInfo]::new((Get-Command ssh).Source)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    foreach ($argument in @('-N', '-T', '-i', $temporaryKey, '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', "UserKnownHostsFile=$knownHosts", '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-L', '127.0.0.1:8088:127.0.0.1:8088', 'root@8.155.128.79')) {
        $start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::Start($start)
    $connected = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($process.HasExited) { throw 'SSH 连接失败，请检查密钥和服务器网络。' }
        $listener = Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $process.Id }
        if ($listener) { $connected = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $connected) { throw 'SSH 转发启动超时。' }
    Write-Output "云端入口：http://localhost:8088；隧道进程 PID：$($process.Id)"
} catch {
    if ($process -and -not $process.HasExited) { $process.Kill() }
    throw
} finally {
    # 认证完成后 SSH 已加载密钥；删除本次临时副本。
    if (Test-Path -LiteralPath $temporaryKey) { Remove-Item -LiteralPath $temporaryKey -Force }
    Remove-Item -LiteralPath $directory -Force
}
