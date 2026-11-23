# 停止 ZhiWen 本机服务：后端(8000) / OCR Worker(8090) / 前端(5173) / OpenSearch(9200) + 残留 MCP 子进程
# 用法：stop_all.bat  或  start_all.bat -Stop
# 注意：MySQL 是 Windows 服务，不在此脚本停止（Stop-Service MySQL93）
param(
    [switch]$KeepMCP
)

$ErrorActionPreference = 'Continue'

function Get-PortPids([int]$Port) {
    $found = @()
    try {
        $found += Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess
    } catch { }
    if (-not $found) {
        try {
            $matches = netstat -ano | Select-String (":$Port\s+\S+\s+LISTENING\s+(\d+)")
            foreach ($m in $matches) { $found += [int]$m.Matches[0].Groups[1].Value }
        } catch { }
    }
    return ($found | Sort-Object -Unique)
}

function Stop-Tree([int]$ProcessId, [string]$Label) {
    if (-not $ProcessId) { return }
    Write-Host ("停止 " + $Label + " (PID " + $ProcessId + ") ...") -ForegroundColor Yellow
    & taskkill.exe /F /T /PID $ProcessId 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ''
Write-Host '========== 知问 ZhiWen 停止服务 ==========' -ForegroundColor Cyan

# 后端：先杀进程树，MCP 的 npx/node/browser 子进程会跟着退出
foreach ($procId in Get-PortPids 8000) { Stop-Tree $procId '后端 8000' }

# OCR Worker：PP-StructureV3 常驻服务（CPU 模型约 3-6GB 内存）
foreach ($procId in Get-PortPids 8090) { Stop-Tree $procId 'OCR Worker 8090' }

# 前端：vite 在独立 cmd 窗口里，先按端口杀 node，再关掉 ZhiWen Frontend 窗口
foreach ($procId in Get-PortPids 5173) { Stop-Tree $procId '前端 5173' }
try {
    $frontendWindows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq 'cmd.exe' -and $_.CommandLine -and $_.CommandLine -match 'ZhiWen Frontend'
    })
    foreach ($wnd in $frontendWindows) { Stop-Tree ([int]$wnd.ProcessId) '前端 cmd 窗口' }
} catch { }

# OpenSearch：9200 可能被别的 Java 占用，先核对命令行再杀
foreach ($procId in Get-PortPids 9200) {
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $procId) -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match 'opensearch') {
        Stop-Tree $procId 'OpenSearch 9200'
    } else {
        Write-Host ("9200 被非 OpenSearch 进程占用（PID " + $procId + "），跳过") -ForegroundColor DarkYellow
    }
}

# OpenSearch 的 cmd 启动器（Java 被停后可能还挂着）
try {
    $osLaunchers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -match 'opensearch\.bat'
    })
    foreach ($launcher in $osLaunchers) { Stop-Tree ([int]$launcher.ProcessId) 'OpenSearch 启动器' }
} catch { }

# 历史残留：上次后端被强杀后遗留的 MCP stdio 子进程
if (-not $KeepMCP) {
    $mcpPattern = '@playwright/mcp|@upstash/context7-mcp|mcp-mermaid|excalidraw-mcp|firecrawl-mcp|arxiv-mcp-server'
    try {
        $orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and $_.CommandLine -match $mcpPattern
        })
        foreach ($proc in $orphans) {
            Stop-Tree ([int]$proc.ProcessId) ("残留 MCP:" + $proc.Name)
        }
        if ($orphans.Count -gt 0) {
            Write-Host ("已清理 " + $orphans.Count + " 个残留 MCP 进程") -ForegroundColor Green
        }
    } catch {
        Write-Host ('清理 MCP 残留失败：' + $_.Exception.Message) -ForegroundColor DarkYellow
    }
}

Write-Host 'MySQL 是 Windows 服务，未停止（如需停止：Stop-Service MySQL93）' -ForegroundColor DarkGray
Write-Host '==========================================' -ForegroundColor Cyan
