@echo off
rem Launcher for terminal Agent. Run directly from repo, or via installed shim.
set "ROOT=%~dp0.."
rem 解释器探测顺序同 pre-commit 钩子：ZHIWEN_PYTHON > 仓库根 .zhiwen-python > 默认路径
set "PY=%ZHIWEN_PYTHON%"
if not defined PY if exist "%ROOT%\.zhiwen-python" (
  for /f "usebackq delims=" %%i in ("%ROOT%\.zhiwen-python") do set "PY=%%i"
)
if not defined PY set "PY=D:\conda_envs\pytorch_env\python.exe"
"%PY%" "%ROOT%\backend\cli_agent.py" %*
