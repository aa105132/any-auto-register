@echo off
taskkill /F /IM chrome-headless-shell.exe >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
echo killed
