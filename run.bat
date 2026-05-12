@echo off
setlocal
set "AGENTS_PIPELINE_LAUNCH_CWD=%CD%"
cd /d %~dp0
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist .env (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)

if exist venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
)

if "%~1"=="python" (
  python %2 %3 %4 %5 %6 %7 %8 %9
  exit /b %errorlevel%
)

where openclaw.CMD >nul 2>nul
if %errorlevel%==0 (
  call openclaw.CMD gateway start >nul 2>nul
)

python run_launcher.py %*
