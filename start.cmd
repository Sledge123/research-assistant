@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo 请先在项目目录运行：pwsh -NoProfile -File .\scripts\setup.ps1
    pause
    exit /b 1
)
if not exist "logs" mkdir "logs"
start "" ".venv\Scripts\pythonw.exe" -B -X utf8 "app.py" 1>"logs\launcher-stdout.log" 2>"logs\launcher-stderr.log"
exit /b %errorlevel%
