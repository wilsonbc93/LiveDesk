@echo off
cd /d "%~dp0"
echo LiveDesk setup - installs the Python packages listed in requirements.txt.
echo No administrator access is required.
if not exist ".venv\Scripts\python.exe" goto try_launcher
.venv\Scripts\python.exe -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto try_launcher
set "LIVE_DESK_PYTHON=.venv\Scripts\python.exe"
goto setup
:try_launcher
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto try_python
set "LIVE_DESK_PYTHON=py -3"
goto setup
:try_python
python -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 goto missing
set "LIVE_DESK_PYTHON=python"
:setup
%LIVE_DESK_PYTHON% bootstrap.py setup
set "LIVE_DESK_RESULT=%errorlevel%"
if not "%LIVE_DESK_RESULT%"=="0" echo Setup could not finish. Read setup.log for details.
if /I not "%~1"=="--no-pause" pause
exit /b %LIVE_DESK_RESULT%
:missing
echo Python 3.11 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/ then run this again.
echo Choose a normal writable folder, and extract the ZIP before setup.
if /I not "%~1"=="--no-pause" pause
exit /b 1
