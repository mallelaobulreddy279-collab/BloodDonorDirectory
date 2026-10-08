@echo off
cd /d "%~dp0app"
where python >nul 2>nul && (python server.py) || (py server.py)
pause
