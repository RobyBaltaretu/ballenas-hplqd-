# Prepara un paquete SOLO CON EL CÓDIGO (sin .env, sin config.json y sin datos) para actualizar el servidor.
# Lo lanza herramientas\preparar-actualizacion.bat. Deja bot-actualizacion.tar.gz en el Escritorio.
# En el servidor: tar -xzf bot-actualizacion.tar.gz && cd bot-ballenas && sudo bash despliegue/actualizar_vps.sh
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Raiz = Split-Path -Parent $PSScriptRoot
$Padre = Split-Path -Parent $Raiz
$Nombre = Split-Path -Leaf $Raiz
$Salida = Join-Path ([Environment]::GetFolderPath("Desktop")) "bot-actualizacion.tar.gz"
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    Write-Host "Este Windows no tiene 'tar' (viene con Windows 10 de 2018 en adelante)." -ForegroundColor Red
    Read-Host "Pulsa Enter para cerrar"; exit 1
}
Write-Host ">> Empaquetando solo el código (sin .env, config.json ni datos)..." -ForegroundColor Cyan
& tar -czf $Salida --exclude=".venv" --exclude=".env" --exclude=".copia.env" --exclude="config.json" `
      --exclude="config.json.*" --exclude="*.bak" --exclude="datos_*" --exclude="registro" --exclude="logs" `
      --exclude="__pycache__" --exclude=".ruff_cache" --exclude="*.lock" --exclude="*.lock.info" --exclude="*.tmp" `
      --exclude="bot.log*" --exclude="*.json" --exclude="*.csv" --exclude="*.tar.gz" --exclude="*.zip" `
      -C $Padre $Nombre
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Salida)) {
    Write-Host "No se pudo crear el paquete." -ForegroundColor Red; Read-Host "Pulsa Enter para cerrar"; exit 1
}
$lista = & tar -tzf $Salida
if ($lista | Where-Object { $_ -match '(^|/)\.env$' }) {
    Remove-Item $Salida
    Write-Host "El paquete contenía un .env: lo he borrado por seguridad. Avisa del problema." -ForegroundColor Red
    Read-Host "Pulsa Enter para cerrar"; exit 1
}
Write-Host "Listo: $Salida. No contiene claves, configuración ni datos." -ForegroundColor Green
Write-Host "Súbelo con: scp -i `"RUTA\A\TU\clave.key`" `"$Salida`" ubuntu@IP_DEL_SERVIDOR:~"
Read-Host "Pulsa Enter para cerrar"
