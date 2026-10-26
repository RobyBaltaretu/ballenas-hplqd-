@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Ejecuta antes INICIAR.bat & pause & exit /b 1)
.venv\Scripts\python.exe comprobar.py
pause
