@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call "Setup LiveDesk.cmd" --no-pause
if errorlevel 1 goto failed
.venv\Scripts\python.exe app.py start
if errorlevel 1 goto failed
exit /b 0
:missing
echo Run Setup LiveDesk.cmd first.
:failed
echo LiveDesk could not finish. Check README.md and Help and diagnostics.
pause
exit /b 1
