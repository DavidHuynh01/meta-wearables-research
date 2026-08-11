@echo off
rem Double-click me after copying phone CSVs into data\app.
rem Rebuilds trials_all.csv and windows_all.csv from every session there.
cd /d "%~dp0"
python tools\app\merge_sessions.py data\app
echo.
pause
