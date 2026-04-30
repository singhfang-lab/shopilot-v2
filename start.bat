@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

cd /d "%~dp0"
set PROJECT_DIR=%~dp0

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"

echo ============================================
echo   USB Assistant - Starting Services
echo ============================================
echo.

:: ---- Helper: check if port is in use ----
:: Usage: call :port_in_use PORT  -> sets PORT_USED=1 or 0
goto :main

:port_in_use
    set PORT_USED=0
    for /f "tokens=*" %%i in ('netstat -ano ^| findstr /R ":%~1 " ^| findstr "LISTENING"') do (
        set PORT_USED=1
    )
    exit /b

:wait_for_port
    :: %1 = port, %2 = name, %3 = label to goto on success
    set /a _count=0
:_wait_loop_%1
    curl -s "http://localhost:%1" >nul 2>&1
    if !errorlevel! == 0 exit /b 0
    if !_count! geq 30 (
        echo   [TIMEOUT] %2 did not respond within 30s
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    set /a _count+=1
    goto _wait_loop_%1

:main

:: ---- 1. Ollama ----
<nul set /p="Starting Ollama...       "
call :port_in_use 11434
if "!PORT_USED!"=="1" (
    echo (already running) OK
) else (
    set OLLAMA_ORIGINS=*
    start /B "" ollama serve > "%PROJECT_DIR%logs\ollama.log" 2>&1
    :: Wait for Ollama API
    set /a _cnt=0
:wait_ollama
    curl -s "http://localhost:11434/api/tags" >nul 2>&1
    if !errorlevel! == 0 (
        echo OK
        goto ollama_done
    )
    if !_cnt! geq 30 (
        echo FAILED  (check logs\ollama.log^)
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    set /a _cnt+=1
    goto wait_ollama
:ollama_done
)

:: ---- 2. OpenClaw ----
<nul set /p="Starting OpenClaw...     "
call :port_in_use 18789
if "!PORT_USED!"=="1" (
    echo (already running) OK
) else (
    set OPENCLAW_BIN=
    if exist "%PROJECT_DIR%openclaw.exe"        set OPENCLAW_BIN=%PROJECT_DIR%openclaw.exe
    if exist "%PROJECT_DIR%scripts\openclaw.exe" set OPENCLAW_BIN=%PROJECT_DIR%scripts\openclaw.exe

    if defined OPENCLAW_BIN (
        start /B "" "!OPENCLAW_BIN!" > "%PROJECT_DIR%logs\openclaw.log" 2>&1
        set /a _cnt=0
:wait_openclaw
        curl -s "http://localhost:18789" >nul 2>&1
        if !errorlevel! == 0 (
            echo OK
            goto openclaw_done
        )
        if !_cnt! geq 30 (
            echo FAILED  (check logs\openclaw.log^)
            exit /b 1
        )
        timeout /t 1 /nobreak >nul
        set /a _cnt+=1
        goto wait_openclaw
:openclaw_done
    ) else (
        echo (skipped -- binary not found^)
    )
)

:: ---- 3. FastAPI backend (uvicorn) ----
<nul set /p="Starting Backend...      "
call :port_in_use 8081
if "!PORT_USED!"=="1" (
    echo (already running) OK
) else (
    if exist "%PROJECT_DIR%venv\Scripts\activate.bat" (
        call "%PROJECT_DIR%venv\Scripts\activate.bat"
    )
    pushd "%PROJECT_DIR%backend"
    start /B "" uvicorn main:app --host 0.0.0.0 --port 8081 > "%PROJECT_DIR%logs\backend.log" 2>&1
    popd
    set /a _cnt=0
:wait_backend
    curl -s "http://localhost:8081" >nul 2>&1
    if !errorlevel! == 0 (
        echo OK
        goto backend_done
    )
    if !_cnt! geq 30 (
        echo FAILED  (check logs\backend.log^)
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    set /a _cnt+=1
    goto wait_backend
:backend_done
)

:: ---- 4. Frontend (python http.server) ----
<nul set /p="Starting Frontend...     "
call :port_in_use 3001
if "!PORT_USED!"=="1" (
    echo (already running) OK
) else (
    pushd "%PROJECT_DIR%frontend"
    start /B "" python -m http.server 3001 > "%PROJECT_DIR%logs\frontend.log" 2>&1
    popd
    set /a _cnt=0
:wait_frontend
    curl -s "http://localhost:3001" >nul 2>&1
    if !errorlevel! == 0 (
        echo OK
        goto frontend_done
    )
    if !_cnt! geq 30 (
        echo FAILED  (check logs\frontend.log^)
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    set /a _cnt+=1
    goto wait_frontend
:frontend_done
)

:: ---- Done ----
echo.
echo ============================================
echo   All services running.
echo   Opening http://localhost:3001 ...
echo ============================================
timeout /t 1 /nobreak >nul
start "" "http://localhost:3001"
