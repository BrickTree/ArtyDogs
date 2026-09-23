@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: creating a private Python environment in .venv ...
    python -m venv .venv || (echo Python 3.10 or newer is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
)
start "" ".venv\Scripts\pythonw.exe" wardogs_arty.pyw
