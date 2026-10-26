@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Panel del bot de ballenas
if not exist .venv\Scripts\activate.bat (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
call .venv\Scripts\activate.bat
python panel.py --abrir
pause
