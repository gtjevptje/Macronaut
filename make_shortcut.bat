@echo off
cd /d "%~dp0"
echo Building Macronaut desktop shortcut...
echo.
python create_shortcut.py
if errorlevel 1 py create_shortcut.py
echo.
echo ----------------------------------------
echo Finished. You can close this window.
pause >nul
