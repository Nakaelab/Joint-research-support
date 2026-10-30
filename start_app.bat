@echo off
cd /d "%~dp0"
echo Starting Fukui University Research Matching Server on http://127.0.0.1:8000 ...
start "" http://127.0.0.1:8000
python server.py
pause
