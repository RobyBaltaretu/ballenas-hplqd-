@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
echo Ronda de investigacion: prueba ideas nuevas con todo el rigor del laboratorio (hasta 90 minutos).
echo No envia ordenes ni activa nada. Puedes dejar el bot funcionando a la vez.
echo.
.venv\Scripts\python.exe investigacion.py --ideas 60 --minutos 90 --presupuesto 300
echo.
echo Informe: registro\investigacion_informe.md  (y en el panel, pestana Investigacion)
pause
