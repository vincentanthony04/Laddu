@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_active_data_and_training_readiness.ps1" %*
set EXIT_CODE=%ERRORLEVEL%
echo.
if not "%EXIT_CODE%"=="0" echo Readiness cycle completed with blockers. Review the JSON report under ProgramData\ProjectLaddu\data\manifests\training_readiness.
pause
exit /b %EXIT_CODE%
