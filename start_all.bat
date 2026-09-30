@echo off
rem ============================================================
rem  ZhiWen one-click launcher (Windows)
rem  Double-click to check/start: MySQL / OpenSearch /
rem  SearXNG(optional) / backend(8000) / frontend(5173),
rem  then open the browser. Already-running parts are skipped.
rem  Args are passed to scripts\start_all.ps1 (e.g. -NoBrowser).
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1" %*
echo.
pause
