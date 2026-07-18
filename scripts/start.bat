@echo off
REM EdgeLite Gateway 启动脚本 (Windows)
REM 用法: scripts\start.bat [dev|prod]
REM
REM dev:  开发模式 (DEV_MODE=true, 自动生成密钥, 启用调试)
REM prod: 生产模式 (DEV_MODE=false, 必须配置密钥)

setlocal enabledelayedexpansion

set "PROJECT_ROOT=%~dp0.."
cd /d "%PROJECT_ROOT%"

set "MODE=%1"
if "%MODE%"=="" set "MODE=dev"

if "%MODE%"=="dev" (
    set "DEV_MODE=true"
    echo Starting EdgeLite in DEVELOPMENT mode...
) else if "%MODE%"=="prod" (
    set "DEV_MODE=false"
    if "%EDGELITE_SECURITY__SECRET_KEY%"=="" (
        echo ERROR: EDGELITE_SECURITY__SECRET_KEY must be set in production mode
        exit /b 1
    )
    if "%EDGELITE_MASTER_KEY%"=="" (
        echo ERROR: EDGELITE_MASTER_KEY must be set in production mode
        exit /b 1
    )
    echo Starting EdgeLite in PRODUCTION mode...
) else (
    echo Usage: %0 [dev^|prod]
    echo   dev   Development mode (default^)
    echo   prod  Production mode (requires secret keys^)
    exit /b 1
)

REM Load .env if exists
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "line=%%a"
        if not "!line:~0,1!"=="#" (
            set "%%a=%%b"
        )
    )
    echo Loaded .env
)

REM Ensure data directories exist
if not exist "data\logs" mkdir "data\logs"
if not exist "data\backups" mkdir "data\backups"
if not exist "data\ota" mkdir "data\ota"
if not exist "data\scada" mkdir "data\scada"

REM Set defaults
if "%EDGELITE_SERVER__HOST%"=="" set "EDGELITE_SERVER__HOST=127.0.0.1"
if "%EDGELITE_SERVER__PORT%"=="" set "EDGELITE_SERVER__PORT=8080"

echo    Host: %EDGELITE_SERVER__HOST%
echo    Port: %EDGELITE_SERVER__PORT%
echo    Mode: %DEV_MODE%
echo.

REM Start the application
python -m edgelite
