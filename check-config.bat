@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python-Umgebung fehlt. Bitte zuerst die Einrichtung in README.md ausfuehren.
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m bookpromo check-config %*
exit /b %errorlevel%
