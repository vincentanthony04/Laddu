@echo off
setlocal
cd /d "%~dp0"
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0validation\run_premarket_customer_acceptance.ps1" %*
set RC=%ERRORLEVEL%
echo.
if %RC% EQU 0 (
  echo PREMARKET CUSTOMER ACCEPTANCE: PASS
) else (
  echo PREMARKET CUSTOMER ACCEPTANCE: FAIL/BLOCKED ^(exit %RC%^)
)
exit /b %RC%
