@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python-Umgebung fehlt. Bitte zuerst die Einrichtung in README.md ausfuehren.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m bookpromo serve %*
set "bookpromo_exit=%errorlevel%"
if not "%bookpromo_exit%"=="0" (
    echo.
    echo Start fehlgeschlagen. Bitte die Meldung oben pruefen.
    pause
)
exit /b %bookpromo_exit%
