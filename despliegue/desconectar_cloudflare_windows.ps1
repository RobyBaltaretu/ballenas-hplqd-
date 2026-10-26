# Quita el conector de Cloudflare Tunnel de ESTE PC (cuando el panel pasa a verse desde el servidor).
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$exe = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
foreach ($r in @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe")) {
    if (-not $exe -and (Test-Path $r)) { $exe = $r }
}
if (-not $exe) { Write-Host "cloudflared no está instalado en este PC: no hay nada que quitar."; Read-Host "Pulsa Enter para cerrar"; exit 0 }
Write-Host "Windows te pedira permiso de administrador: pulsa Si."
Start-Process -FilePath $exe -ArgumentList "service uninstall" -Verb RunAs -Wait
Start-Sleep -Seconds 3
if (Get-Service -Name cloudflared -ErrorAction SilentlyContinue) {
    Write-Host "El servicio sigue instalado. Prueba de nuevo o quitalo desde Servicios de Windows." -ForegroundColor Yellow
} else {
    Write-Host "Conector quitado de este PC. El panel se vera solo desde el servidor." -ForegroundColor Green
}
Read-Host "Pulsa Enter para cerrar"
