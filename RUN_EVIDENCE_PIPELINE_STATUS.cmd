@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_EVIDENCE_PIPELINE_STATUS.ps1" %*
set EC=%ERRORLEVEL%
echo.
pause
exit /b %EC%
