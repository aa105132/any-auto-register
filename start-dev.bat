@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
title Any Auto Register - Launcher

echo.
echo ========================================
echo   Any Auto Register Local Launcher
echo ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.11+
  goto :fail
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm not found. Please install Node.js 20.19+
  goto :fail
)

for /f "tokens=1 delims=." %%v in ('node -p "process.versions.node"') do set NODE_MAJOR=%%v
for /f "tokens=2 delims=." %%v in ('node -p "process.versions.node"') do set NODE_MINOR=%%v
if %NODE_MAJOR% LSS 20 (
  echo [ERROR] Node.js 20.19+ is required. Current:
  node -v
  goto :fail
)
if %NODE_MAJOR% EQU 20 if %NODE_MINOR% LSS 19 (
  echo [ERROR] Node.js 20.19+ is required. Current:
  node -v
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :fail
)

echo [2/5] Installing backend requirements...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

if not exist "frontend\node_modules\.bin\vite.cmd" (
  echo [3/5] Installing frontend dependencies...
  pushd frontend
  call npm install
  if errorlevel 1 (
    popd
    goto :fail
  )
  popd
) else (
  echo [3/5] Frontend dependencies already installed, skip npm install
)

echo [4/5] Starting backend window...
start "" "%~dp0start-backend.bat"

echo [5/5] Starting frontend window...
start "" "%~dp0start-frontend.bat"

echo Waiting for backend to be ready (up to 30s)...
set /a _tries=0
:waitloop
set /a _tries+=1
if %_tries% gtr 30 (
  echo [WARN] Backend did not respond within 30s, opening browser anyway.
  goto :openpage
)
curl -s -o nul -w "" http://127.0.0.1:8899/api/platforms >nul 2>nul
if errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto :waitloop
)
echo Backend ready after %_tries%s.
:openpage
start "" "http://127.0.0.1:5173"

echo.
echo Done.
echo Backend:  http://127.0.0.1:8899
echo Frontend: http://127.0.0.1:5173
echo.
echo Optional browser-mode dependency:
echo   .venv\Scripts\python.exe -m playwright install chromium
echo.
goto :eof

:fail
echo.
echo Startup failed. Please read the error above.
pause
exit /b 1
