@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LAUNCH_LOG=C:\Temp\ProjectLaddu\installer\INSTALL_UPDATE-LAUNCHER.log"
if not exist "C:\Temp\ProjectLaddu\installer" mkdir "C:\Temp\ProjectLaddu\installer" >nul 2>&1
>"%LAUNCH_LOG%" echo [%DATE% %TIME%] Project Laddu R21A install launcher started from %~dp0

echo.
echo Project Laddu - single-authority install/update
echo Complete clean-machine install or in-place upgrade. Existing data and secure state are preserved.
echo This window now waits for the elevated installer and will not silently close on failure.
echo Launcher log: %LAUNCH_LOG%
echo Package: v131.1.6 / PL46 Simple Install R6 + Closed-Market Lineage + UI7
echo IMPORTANT: run only from a fresh Windows Extract All folder; never overwrite an older package extraction.
echo.

if not exist "%~dp0installer\install.ps1" (
  echo INSTALLER CANNOT START FROM AN INCOMPLETE OR ZIP-PREVIEW FOLDER.
  echo Use Windows Extract All, then run this command from the extracted folder.
  >>"%LAUNCH_LOG%" echo [%DATE% %TIME%] FAIL missing installer\install.ps1
  echo.
  pause
  exit /b 2
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
set "RC=%ERRORLEVEL%"
>>"%LAUNCH_LOG%" echo [%DATE% %TIME%] Installer returned exit code %RC%

echo.
if not "%RC%"=="0" (
  echo INSTALL FAILED. Exit code %RC%.
  echo Review C:\Temp\ProjectLaddu\installer and the launcher log above.
  echo.
  pause
  exit /b %RC%
)

echo INSTALL PASS. The elevated installer completed successfully.
echo Evidence is under C:\Temp\ProjectLaddu\installer.
echo.
echo Press any key to open Project Laddu in the browser.
pause >nul
start "" "http://127.0.0.1:8086/"
exit /b 0
