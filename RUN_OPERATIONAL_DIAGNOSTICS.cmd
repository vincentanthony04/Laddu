@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\COLLECT_OPERATIONAL_EVIDENCE.ps1" -Minutes 120
pause
