@echo off
setlocal
chcp 65001 >nul

cd /d "%~dp0\frontend"
title Any Auto Register - Frontend

if not exist "node_modules\.bin\vite.cmd" (
  echo [ERROR] Missing frontend dependencies.
  echo Please run start-dev.bat first.
  pause
  exit /b 1
)

call npm run dev -- --host 127.0.0.1 --port 5173

echo.
echo Frontend exited.
pause
