@echo off
set /p SYMBOL=Stock symbol for latency proof [INFY]: 
if "%SYMBOL%"=="" set SYMBOL=INFY
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\AUDIT_QUANT_ML_ALPHA.ps1" -Symbol "%SYMBOL%" -PerformanceSamples 3 -SimulationPaths 5000
pause
