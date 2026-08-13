# 安装 pre-commit 钩子到 .git/hooks
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$hookPath = Join-Path $repoRoot ".git\hooks\pre-commit"

Copy-Item -LiteralPath (Join-Path $scriptDir "pre-commit") -Destination $hookPath -Force
Write-Host "已安装：$hookPath"
