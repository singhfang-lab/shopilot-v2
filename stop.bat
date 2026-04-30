@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ============================================
echo   USB Assistant - Stopping Services
echo ============================================
echo.

:: Kill by port
call :kill_port 11434 "Ollama"
call :kill_port 18789 "OpenClaw"
call :kill_port 8081  "Backend (uvicorn)"
call :kill_port 3001  "Frontend (http.server)"

echo.
echo Done.
pause
exit /b

:kill_port
    set _found=0
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R ":%~1 " ^| findstr "LISTENING"') do (
        taskkill /PID %%p /F >nul 2>&1
        set _found=1
    )
    if "!_found!"=="1" (
        echo Stopped %-20s OK -- %~2
    ) else (
        echo %~2 -- (not running)
    )
    exit /b
