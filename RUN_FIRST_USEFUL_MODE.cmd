@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_first_useful_mode.ps1" -InstallDir "%ProgramData%\ProjectLaddu" -Port 8086
exit /b %ERRORLEVEL%
