@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>nul
echo.
echo ========================================
echo   JetBrainsReg Launcher
echo ========================================
echo.
py -m jetbrainsreg %*
if %errorlevel% neq 0 (
    echo.
    echo ========================================
    echo   JetBrainsReg exited with error.
    echo   Possible causes:
    echo     1. Port 7860 is in use
    echo     2. Run: pip install -r requirements.txt
    echo     3. Python 3.10+ required
    echo ========================================
    echo.
)
pause
