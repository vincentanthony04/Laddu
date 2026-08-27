@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_brand_asset_refresh.ps1" %*
set EXIT_CODE=%ERRORLEVEL%
pause
exit /b %EXIT_CODE%
