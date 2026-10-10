# 安装 pre-commit 钩子（钩子本体在 scripts/githooks/pre-commit，随仓库版本化）
#
#   .\scripts\install-git-hooks.ps1
#   .\scripts\install-git-hooks.ps1 -Python D:\conda_envs\pytorch_env\python.exe
#
# 做三件事：
#   1) git config core.hooksPath scripts/githooks —— 让 git 直接跑版本化钩子
#      （不再往 .git/hooks 复制，避免"改一处、两台机器不一致"）；
#   2) 写 .zhiwen-python（gitignored，本机解释器路径提示，钩子会读它）；
#   3) 尽量把钩子文件的执行位写进 git 索引（Linux/macOS 需要；Windows 无所谓）。
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$hookRel = "scripts/githooks/pre-commit"
$pythonHint = Join-Path $repoRoot ".zhiwen-python"

if (-not $Python) {
    $candidates = @(
        "D:\conda_envs\pytorch_env\python.exe",
        (Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source -ErrorAction SilentlyContinue)
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $Python = $candidates | Select-Object -First 1
}

Push-Location $repoRoot
try {
    # 1) 版本化钩子目录
    git config core.hooksPath "scripts/githooks"
    if ($LASTEXITCODE -ne 0) { throw "git config core.hooksPath 失败" }
    Write-Host "core.hooksPath = scripts/githooks"

    # 2) 解释器提示文件（无 BOM、无换行：钩子用 head -n 1 + tr -d '\r' 读取）
    if ($Python) {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($pythonHint, $Python, $utf8NoBom)
        Write-Host "解释器已记录：$Python  ->  .zhiwen-python"
    }
    else {
        Write-Host "未探测到 python 解释器；请手动运行：.\scripts\install-git-hooks.ps1 -Python <path>" -ForegroundColor Yellow
    }

    # 3) 执行位（仅对已纳入 git 的文件有效；未跟踪时提示首次提交后重跑）
    #    注意：未跟踪时 git 会往 stderr 写错误，$ErrorActionPreference=Stop 下
    #    会被当成终止错误——探测前临时放开，只按退出码判断。
    $ErrorActionPreference = "Continue"
    git ls-files --error-unmatch $hookRel > $null 2>&1
    $tracked = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    if ($tracked) {
        git update-index --chmod=+x $hookRel
        Write-Host "已写入执行位：$hookRel"
    }
    else {
        Write-Host "提示：$hookRel 尚未纳入 git；首次提交后重跑本脚本即可写入执行位（Linux/macOS 需要）。" -ForegroundColor Yellow
    }

    Write-Host "安装完成。可用 'git commit' 触发；临时跳过用 git commit --no-verify。" -ForegroundColor Green
}
finally {
    Pop-Location
}
