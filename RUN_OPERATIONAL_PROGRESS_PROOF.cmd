@echo off
setlocal
title Project Laddu - Operational Progress Self-Proof
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_OPERATIONAL_PROGRESS_PROOF.ps1"
echo.
pause
