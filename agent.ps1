# 快速启动终端版 Agent：.\agent.ps1 [--tool auto|knowledge|web|none] [--conversation <id>]
$python = 'D:\conda_envs\pytorch_env\python.exe'
$cli = Join-Path $PSScriptRoot 'backend\cli_agent.py'
& $python $cli @args
