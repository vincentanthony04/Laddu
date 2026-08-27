@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_focused_market_diagnostics.ps1" %*
set EXIT_CODE=%ERRORLEVEL%
pause
exit /b %EXIT_CODE%
