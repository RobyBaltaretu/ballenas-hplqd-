@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
set PY=%CD%\.venv\Scripts\pythonw.exe
if not exist "%PY%" set PY=%CD%\.venv\Scripts\python.exe
schtasks /Create /F /SC WEEKLY /D SUN /ST 04:00 /TN "Bot ballenas - laboratorio semanal" /TR "\"%PY%\" \"%CD%\laboratorio_semanal.py\""
if errorlevel 1 (echo No se pudo crear la tarea. & pause & exit /b 1)
echo.
echo Listo: cada domingo a las 04:00 se pasara el laboratorio (con pocas peticiones, para no molestar al bot).
echo Solo recibiras un aviso por Telegram si alguna estrategia cambia de estado. No se activa nada solo.
echo El resultado queda en registro\laboratorio_semanal.jsonl. Para quitarlo: desactivar-laboratorio-semanal.bat
pause
