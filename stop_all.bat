@echo off
rem ============================================================
rem  ZhiWen stop-all launcher (Windows)
rem  Stops backend(8000) / frontend(5173) / OpenSearch(9200)
rem  and orphaned MCP child processes. MySQL service is left as-is.
rem  Args are passed to scripts\stop_all.ps1 (e.g. -KeepMCP).
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_all.ps1" %*
echo.
pause
