# 重启后端：杀掉 8000 端口进程，注入环境变量后以隐藏窗口启动 uvicorn。
# 2026-09-10 加固：Get-NetTCPConnection 找不到时回退 netstat 解析（曾静默漏杀，
# 新进程绑定 8000 报 10048 后退出、脚本却报成功）；启动后轮询 /api/health 验证。
$ErrorActionPreference = 'Stop'
# 项目根 = 本脚本的上一级（脚本位于 backend/），不写死个人目录
$root = Split-Path -Parent $PSScriptRoot

# 杀掉占用 8000 的进程（可能是热重载后的新 PID）
$targetPids = @()
try {
    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { $targetPids += [int]$conn.OwningProcess }
} catch { }
if ($targetPids.Count -eq 0) {
    $matches = netstat -ano | Select-String ':8000\s+\S+\s+LISTENING\s+(\d+)'
    foreach ($m in $matches) { $targetPids += [int]$m.Matches[0].Groups[1].Value }
}
foreach ($targetPid in ($targetPids | Select-Object -Unique)) {
    Write-Output ("killing old backend PID " + $targetPid)
    Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
}
# 等端口释放（最多 5s），避免新进程绑定失败
for ($i = 0; $i -lt 10; $i++) {
    $still = $null
    try { $still = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue } catch { }
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
}

# 本机凭据从 backend/.env.local 读取（gitignored，同 run.py 的加载约定：
# 只补缺失的键，不覆盖已存在的环境变量）
$envFile = Join-Path $root 'backend\.env.local'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
        $k, $v = $t.Split('=', 2)
        $k = $k.Trim()
        $v = $v.Trim().Trim('"').Trim("'")
        if ($k -and -not (Test-Path "env:$k")) { Set-Item -Path "env:$k" -Value $v }
    }
}
if (-not $env:MYSQL_URL) {
    Write-Host '提示：未找到 MYSQL_URL（可在 backend\.env.local 配置），会话记忆/模板将不可用。'
}
$env:SENSENOVA_API_KEY = [Environment]::GetEnvironmentVariable('SENSENOVA_API_KEY', 'User')
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')

Start-Process D:\conda_envs\pytorch_env\python.exe `
  -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
  -WorkingDirectory (Join-Path $root 'backend') `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root 'backend\uvicorn.out.log') `
  -RedirectStandardError (Join-Path $root 'backend\uvicorn.err.log')

# 验证启动成功（绑定失败会在数秒内退出，旧脚本此时仍报"restarted"）
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 1500
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8000/api/health'
        if ($r.StatusCode -eq 200) {
            Write-Output ('backend restarted and healthy: ' + $r.Content)
            exit 0
        }
    } catch { }
}
Write-Error 'backend 未在预期时间内就绪，请检查 backend\uvicorn.err.log'
exit 1
