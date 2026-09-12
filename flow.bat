@echo off
cd /d "%~dp0"
python -u "%~dp0flow.py" %*
pause
