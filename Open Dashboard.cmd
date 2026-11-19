@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" dashboard.py open
if errorlevel 1 pause
