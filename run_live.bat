@echo off
title Lablelens Live Server
cd /d "%~dp0"
echo ========================================================
echo   Starting Lablelens with Cloudflare Live Tunnel
echo ========================================================
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe start_live.py
) else (
    python start_live.py
)
pause
