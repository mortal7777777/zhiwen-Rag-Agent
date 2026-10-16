# 知问 ZhiWen 一键启动：检查/启动 依赖服务 + 后端 + 前端，就绪后打开浏览器
# 用法：双击根目录 start_all.bat；或 powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1 [-NoBrowser]
param(
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot   # 仓库根（本脚本位于 scripts/ 下）

# OpenSearch 原生安装目录。探测顺序（同 .zhiwen-python 的本机文件约定）：
#   环境变量 ZHIWEN_OPENSEARCH_HOME > 仓库根 .zhiwen-opensearch 文件（gitignore）> 通用默认
$OpenSearchHome = if ($env:ZHIWEN_OPENSEARCH_HOME) {
    $env:ZHIWEN_OPENSEARCH_HOME
} elseif (Test-Path (Join-Path $root '.zhiwen-opensearch')) {
    # -Encoding UTF8：PS 5.1 默认按 ANSI 读，路径含中文时会乱码导致找不到目录
    (Get-Content (Join-Path $root '.zhiwen-opensearch') -First 1 -Encoding UTF8).Trim()
} else {
    'D:\opensearch\opensearch-3.5.0'
}

function Test-Port([int]$Port) {
    # 优先用 Get-NetTCPConnection（同时覆盖 IPv4/IPv6；vite dev 只监听 ::1 的坑）
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) { return $true }
    } catch { }
    # 回退：直接 TCP 连接（127.0.0.1 与 ::1 都试）
    foreach ($addr in @('127.0.0.1', '::1')) {
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect($addr, $Port)
            $client.Close()
            return $true
        } catch { }
    }
    return $false
}

function Wait-Port([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-Port $Port)
}

function Wait-Health([string]$Url, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

Write-Host ''
Write-Host '========== 知问 ZhiWen 一键启动 ==========' -ForegroundColor Cyan

# ---------- 0/5 环境预检（缺什么先说清楚，避免后面的超时提示误导排查）----------
# 解释器探测顺序同 pre-commit 钩子：ZHIWEN_PYTHON > 仓库根 .zhiwen-python > 默认路径
$Python = if ($env:ZHIWEN_PYTHON) { $env:ZHIWEN_PYTHON }
    elseif (Test-Path (Join-Path $root '.zhiwen-python')) {
        (Get-Content (Join-Path $root '.zhiwen-python') -First 1).Trim()
    } else { 'D:\conda_envs\pytorch_env\python.exe' }
$backendOk = $true
$frontendOk = $true
if (-not (Test-Path $Python)) {
    $backendOk = $false
    Write-Host ('[0/5] 后端环境     : Python 解释器不存在：' + $Python) -ForegroundColor Red
    Write-Host '      处理：设置环境变量 ZHIWEN_PYTHON，或编辑仓库根 .zhiwen-python 指向本机 python.exe' -ForegroundColor DarkGray
} else {
    & $Python -c 'import fastapi, uvicorn' 2>$null
    if ($LASTEXITCODE -ne 0) {
        $backendOk = $false
        Write-Host '[0/5] 后端环境     : 依赖未安装（缺 fastapi / uvicorn）' -ForegroundColor Red
        Write-Host ('      处理：' + $Python + ' -m pip install -r ' + (Join-Path $root 'backend\requirements.txt')) -ForegroundColor DarkGray
    } else {
        Write-Host '[0/5] 后端环境     : 就绪（Python + 依赖）' -ForegroundColor Green
    }
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    $frontendOk = $false
    Write-Host '[0/5] 前端环境     : 未找到 node（前端需要 Node.js 18+）' -ForegroundColor Red
} elseif (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) {
    $frontendOk = $false
    Write-Host '[0/5] 前端环境     : 依赖未安装（frontend\node_modules 不存在）' -ForegroundColor Red
    Write-Host '      处理：cd frontend 后运行 npm install' -ForegroundColor DarkGray
} else {
    Write-Host '[0/5] 前端环境     : 就绪（node + 依赖）' -ForegroundColor Green
}

# ---------- 1/5 MySQL（3306）----------
if (Test-Port 3306) {
    Write-Host '[1/5] MySQL        : 已运行 (3306)' -ForegroundColor Green
} else {
    Write-Host '[1/5] MySQL        : 未运行，尝试启动服务 MySQL93 ...' -ForegroundColor Yellow
    try {
        Start-Service -Name 'MySQL93' -ErrorAction Stop
    } catch {
        Write-Host ('      启动失败（可能需要管理员权限）：' + $_.Exception.Message) -ForegroundColor Red
    }
    if (Wait-Port 3306 30) { Write-Host '      MySQL 已就绪' -ForegroundColor Green }
    else { Write-Host '      MySQL 未就绪：请手动启动（services.msc -> MySQL93）' -ForegroundColor Red }
}

# ---------- 2/5 OpenSearch（9200）----------
if (Test-Port 9200) {
    Write-Host '[2/5] OpenSearch   : 已运行 (9200)' -ForegroundColor Green
} else {
    $osBat = Join-Path $OpenSearchHome 'bin\opensearch.bat'
    if (Test-Path $osBat) {
        Write-Host '[2/5] OpenSearch   : 未运行，启动中（就绪约需 30-90 秒）...' -ForegroundColor Yellow
        Start-Process -FilePath $osBat -WorkingDirectory $OpenSearchHome -WindowStyle Hidden
        if (Wait-Port 9200 90) { Write-Host '      OpenSearch 已就绪' -ForegroundColor Green }
        else { Write-Host '      OpenSearch 启动超时，请查看其 logs 目录' -ForegroundColor Red }
    } else {
        Write-Host ('[2/5] OpenSearch   : 未运行，且未找到：' + $osBat) -ForegroundColor Red
        Write-Host '      可用环境变量 ZHIWEN_OPENSEARCH_HOME 指向安装目录' -ForegroundColor DarkGray
    }
}

# ---------- 3/5 SearXNG（8888，可选）----------
if (Test-Port 8888) {
    Write-Host '[3/5] SearXNG      : 已运行 (8888)' -ForegroundColor Green
} else {
    $started = $false
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        try {
            $hit = docker ps -a --format '{{.Names}} {{.Image}}' 2>$null |
                Select-String -Pattern 'searxng' | Select-Object -First 1
            if ($hit) {
                $name = ($hit.ToString().Trim() -split '\s+')[0]
                Write-Host ('[3/5] SearXNG      : 未运行，尝试启动容器 ' + $name + ' ...') -ForegroundColor Yellow
                docker start $name | Out-Null
                $started = Wait-Port 8888 20
            }
        } catch { }
    }
    if ($started) {
        Write-Host '      SearXNG 已就绪' -ForegroundColor Green
    } else {
        Write-Host '[3/5] SearXNG      : 未运行（可选组件，需 Docker Desktop；不启动不影响基本使用）' -ForegroundColor DarkGray
    }
}

# ---------- 4/5 后端（8000）----------
if (-not $backendOk) {
    Write-Host '[4/5] 后端 FastAPI : 跳过（环境预检未通过，见 [0/5]）' -ForegroundColor Red
} elseif (Test-Port 8000) {
    Write-Host '[4/5] 后端 FastAPI : 已运行 (8000)' -ForegroundColor Green
    Write-Host '      提示：改过后端代码请执行 backend\restart_backend.ps1 重启' -ForegroundColor DarkGray
} else {
    Write-Host '[4/5] 后端 FastAPI : 启动中（127.0.0.1:8000）...' -ForegroundColor Yellow
    # 读取 backend\.env.local（与 run.py / restart_backend.ps1 约定一致：只补缺失键）
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
    foreach ($key in @('DEEPSEEK_API_KEY', 'SENSENOVA_API_KEY')) {
        if (-not (Test-Path "env:$key")) {
            $val = [Environment]::GetEnvironmentVariable($key, 'User')
            if ($val) { Set-Item -Path "env:$key" -Value $val }
        }
    }
    Start-Process -FilePath $Python `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000' `
        -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden
    if (Wait-Health 'http://127.0.0.1:8000/api/health' 90) {
        Write-Host '      后端已就绪（RAG 模型在后台预热）' -ForegroundColor Green
    } else {
        Write-Host '      后端启动超时，请查看 backend\uvicorn.err.log' -ForegroundColor Red
    }
}

# ---------- 5/5 前端（5173）----------
if (-not $frontendOk) {
    Write-Host '[5/5] 前端 Vite    : 跳过（环境预检未通过，见 [0/5]）' -ForegroundColor Red
} elseif (Test-Port 5173) {
    Write-Host '[5/5] 前端 Vite    : 已运行 (5173)' -ForegroundColor Green
} else {
    Write-Host '[5/5] 前端 Vite    : 启动中（新窗口；关闭该窗口即停止前端）...' -ForegroundColor Yellow
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', 'title ZhiWen Frontend && npm run dev' `
        -WorkingDirectory (Join-Path $root 'frontend')
    if (Wait-Port 5173 45) { Write-Host '      前端已就绪' -ForegroundColor Green }
    else { Write-Host '      前端启动超时，请看新打开的窗口输出' -ForegroundColor Red }
}

Write-Host '==========================================' -ForegroundColor Cyan
if ($NoBrowser) {
    Write-Host '（已跳过打开浏览器）' -ForegroundColor DarkGray
} else {
    Start-Process 'http://localhost:5173'
    Write-Host '已在浏览器打开 http://localhost:5173' -ForegroundColor Cyan
}
Write-Host '完成：本窗口可关闭（前端运行在独立窗口；后端为隐藏进程）。' -ForegroundColor Cyan
