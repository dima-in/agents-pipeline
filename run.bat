@echo off
setlocal
cd /d %~dp0

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

python start.py %*
