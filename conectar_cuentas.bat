@echo off
cd /d "%~dp0"
echo Conectando cuentas de Lumepic y Momentia en Brave...
.venv\Scripts\python.exe -m app.connect lumepic momentia
pause
