@echo off
rem Launcher for terminal Agent. Run directly from repo, or via installed shim.
set "ROOT=%~dp0.."
"D:\conda_envs\pytorch_env\python.exe" "%ROOT%\backend\cli_agent.py" %*
