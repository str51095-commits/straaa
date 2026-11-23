@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ===== GoodWe auto-sync (optional): remove "rem " and fill in your SEMS account =====
rem set GOODWE_ACCOUNT=your_account
rem set GOODWE_PASSWORD=your_password

where py >nul 2>nul && (py -3 run.py & goto end)
where python >nul 2>nul && (python run.py & goto end)
echo Python 3 not found. Please install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during installation)
:end
pause
