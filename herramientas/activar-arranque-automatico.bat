@echo off
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut([Environment]::GetFolderPath('Startup')+'\Bot de ballenas.lnk'); $l.TargetPath='%CD%\INICIAR.bat'; $l.WorkingDirectory='%CD%'; $l.Save()"
echo El bot arrancara solo cada vez que inicies sesion en Windows.
echo Para quitarlo: desactivar-arranque-automatico.bat
pause
