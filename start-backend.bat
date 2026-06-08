@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
title Any Auto Register - Backend

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing .venv\Scripts\python.exe
  echo Please run start-dev.bat first.
  pause
  exit /b 1
)

call ".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8899

echo.
echo Backend exited.
pause
