@echo off
setlocal
set "RUNTIME=%ProgramData%\ProjectLaddu\installer\runtime.ps1"
if not exist "%RUNTIME%" (
  echo Project Laddu is not installed. Run INSTALL_UPDATE.cmd first.
  pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RUNTIME%" -Action START
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
