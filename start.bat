@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt -q
echo.
echo Starting Sonar Fleet Allocator at http://localhost:5000
echo Default login: admin / admin
echo.
python app.py
pause
