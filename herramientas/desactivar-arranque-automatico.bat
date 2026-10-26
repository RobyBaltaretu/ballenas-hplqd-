@echo off
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup')+'\Bot de ballenas.lnk') -ErrorAction SilentlyContinue"
echo Arranque automatico desactivado.
pause
