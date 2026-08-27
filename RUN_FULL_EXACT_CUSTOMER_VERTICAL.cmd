@echo off
setlocal
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0RUN_EXACT_PRODUCT_ACCEPTANCE.ps1\" -FullLive'"
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_EXACT_PRODUCT_ACCEPTANCE.ps1" -FullLive %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo FULL VERTICAL NOT ACCEPTED. Review the evidence path above.
exit /b %RC%
