@echo off
setlocal enabledelayedexpansion

echo Current directory: %cd%
echo.

REM Check if venv directory exists
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Error: Failed to create venv
        exit /b 1
    )
    echo Virtual environment created.
    echo.
    echo Installing dependencies...
    venv\Scripts\pip.exe install -r requirements.txt
    if errorlevel 1 (
        echo Error: Failed to install requirements
        exit /b 1
    )
    echo numpy installed successfully.
) else (
    echo venv directory found.
)

echo.
echo Running bms_renderer.py...
venv\Scripts\python.exe bms_renderer.py

pause