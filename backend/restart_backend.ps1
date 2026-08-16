# 重启后端：杀掉 8000 端口进程，注入环境变量后以隐藏窗口启动 uvicorn
$ErrorActionPreference = 'Stop'
$root = 'C:\Users\user\PycharmProjects\PythonProjectPytorch1\langchain01\rag_knowledge_base'

# 杀掉占用 8000 的进程（可能是热重载后的新 PID）
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Output ("killing old backend PID " + $conn.OwningProcess)
    Stop-Process -Id $conn.OwningProcess -Force
    Start-Sleep -Seconds 2
}

$env:MYSQL_URL = 'mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/rag_assistant?charset=utf8mb4'
$env:SENSENOVA_API_KEY = [Environment]::GetEnvironmentVariable('SENSENOVA_API_KEY', 'User')
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')

Start-Process D:\conda_envs\pytorch_env\python.exe `
  -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
  -WorkingDirectory (Join-Path $root 'backend') `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root 'backend\uvicorn.out.log') `
  -RedirectStandardError (Join-Path $root 'backend\uvicorn.err.log')

Write-Output 'backend restarted'
