@echo off
cd /d "%~dp0"
python smallcap_auto_v3.py
if errorlevel 1 echo Run failed. Please take a screenshot of the error.
pause
