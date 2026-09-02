# Install zhiwen command into user PATH (WindowsApps by default)
param(
    [string]$BinDir = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
)

$repo = Split-Path -Parent $PSScriptRoot
$target = Join-Path $BinDir 'zhiwen.cmd'
$shim = "@echo off`r`ncall `"$repo\scripts\zhiwen.cmd`" %*`r`n"
Set-Content -LiteralPath $target -Value $shim -Encoding ASCII
Write-Host "Installed: $target"
Write-Host "Now you can run: zhiwen"
