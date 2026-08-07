@echo off
rem Double-click me after copying phone CSVs into the data folder.
rem Rebuilds trials_all.csv and windows_all.csv from every session in data\.
cd /d "%~dp0"
python tools\merge_sessions.py data
echo.
pause
