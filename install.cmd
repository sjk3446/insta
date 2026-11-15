@echo off
setlocal
pushd "%~dp0" || exit /b 1
set PYTHONUTF8=1
where python >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.10 or later from python.org, then run this file again.
    popd
    pause
    exit /b 1
)
python install.py
set "status=%errorlevel%"
popd
pause
exit /b %status%
