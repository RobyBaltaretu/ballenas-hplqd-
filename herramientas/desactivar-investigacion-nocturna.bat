@echo off
chcp 65001 >nul
schtasks /Delete /F /TN "Bot ballenas - investigacion"
echo Investigacion nocturna desactivada en este PC.
pause
