@echo off
setlocal
cd /d %~dp0

if not exist venv\Scripts\python.exe (
  py -3 -m venv venv
  if errorlevel 1 python -m venv venv
  if errorlevel 1 goto :error
)

call venv\Scripts\activate.bat
if errorlevel 1 goto :error
python -m pip install --upgrade pip
if errorlevel 1 goto :error
pip install -r requirements.txt
if errorlevel 1 goto :error
pip install -r requirements-dev.txt
if errorlevel 1 goto :error

echo.
echo Installation complete.
echo Next steps:
echo   Copy-Item .env.example .env
echo   run.bat python manage_agents.py bootstrap
echo   run.bat python manage_agents.py register-all
exit /b 0

:error
echo Installation failed.
exit /b 1
