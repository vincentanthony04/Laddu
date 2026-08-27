@echo off
setlocal EnableExtensions
cd /d "%~dp0"
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell.exe -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0RUN_FINAL_PRODUCT_ACCEPTANCE.ps1\"'"
  exit /b 0
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_FINAL_PRODUCT_ACCEPTANCE.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo FINAL PRODUCT ACCEPTANCE PASS.) else (echo FINAL PRODUCT ACCEPTANCE FAILED OR REMAINS UNPROVEN. Review the evidence path above.)
pause
exit /b %RC%
