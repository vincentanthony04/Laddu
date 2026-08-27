@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_EXACT_PRODUCT_ACCEPTANCE.ps1" %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo ACCEPTANCE FAILED. Review the evidence path above.
exit /b %RC%
