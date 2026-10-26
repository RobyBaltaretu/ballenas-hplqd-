@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
set PY=%CD%\.venv\Scripts\pythonw.exe
if not exist "%PY%" set PY=%CD%\.venv\Scripts\python.exe
schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI,SAT /ST 02:30 /TN "Bot ballenas - investigacion" /TR "\"%PY%\" \"%CD%\investigacion.py\" --ideas 60 --minutos 90 --presupuesto 300"
if errorlevel 1 (echo No se pudo crear la tarea. & pause & exit /b 1)
echo.
echo Listo: de lunes a sabado a las 02:30 el bot probara ideas nuevas (el domingo va el laboratorio).
echo Solo recibiras un aviso por Telegram si alguna idea pasa o se juzga. No se activa nada solo.
echo Para quitarlo: desactivar-investigacion-nocturna.bat
pause
