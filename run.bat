@echo off
setlocal
cd /d "%~dp0"

python web_app.py
if errorlevel 1 (
    echo.
    echo The app could not start. Check the setup instructions in README.md.
    pause
    exit /b 1
)
