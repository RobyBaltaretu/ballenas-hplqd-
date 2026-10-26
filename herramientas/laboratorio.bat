@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\activate.bat (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
call .venv\Scripts\activate.bat
echo Laboratorio: prueba las estrategias con anos de datos reales y deja evidencia (no activa nada en real).
echo La primera vez tarda 30-60 min (descarga datos); las siguientes, bastante menos.
echo El bot puede seguir funcionando mientras tanto; al terminar, reinicia el bot para que use el resultado.
echo.
python laboratorio.py --guardar-memoria --acciones
echo.
echo Pasale a Claude el RESUMEN de arriba (o el archivo estrategia_elegida.json).
pause
