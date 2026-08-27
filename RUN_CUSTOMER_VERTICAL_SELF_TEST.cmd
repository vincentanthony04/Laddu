@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
python validation\verify_customer_vertical_slice.py
set RC=%ERRORLEVEL%
echo.
if %RC% EQU 0 (
  echo CUSTOMER VERTICAL SLICE SELF-TEST: PASS
) else (
  echo CUSTOMER VERTICAL SLICE SELF-TEST: FAIL ^(exit %RC%^)
)
exit /b %RC%
