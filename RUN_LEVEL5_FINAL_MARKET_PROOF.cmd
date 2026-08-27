@echo off
setlocal
cd /d "%~dp0"
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0validation\run_level5_final_market_proof.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo Level-5 final market proof finished with blockers. Exit code %RC%.
if "%RC%"=="0" echo Level-5 final market proof PASS.
pause
exit /b %RC%
