# Ajustes de energía para que el PC de casa no se duerma con el bot en marcha.
# Solo afectan cuando el PC está ENCHUFADO a la corriente; con batería no cambia nada.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Write-Host "Este ajuste hace que, cuando el PC este ENCHUFADO:"
Write-Host "  - no se suspenda nunca por inactividad"
Write-Host "  - no hiberne"
Write-Host "  - no se suspenda al cerrar la tapa (si es portatil)"
Write-Host "La pantalla si podra apagarse sola (eso no afecta al bot). Con bateria no cambia nada."
Write-Host ""
$r = Read-Host "Aplicar? (S/N)"
if ($r -notmatch '^[sS]') { Write-Host "No se ha cambiado nada."; Read-Host "Pulsa Enter para cerrar"; exit 0 }
$errores = 0
powercfg /change standby-timeout-ac 0;   if ($LASTEXITCODE -ne 0) { $errores++ }
powercfg /change hibernate-timeout-ac 0; if ($LASTEXITCODE -ne 0) { $errores++ }
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 2>$null; if ($LASTEXITCODE -ne 0) { $errores++ }
powercfg /setactive SCHEME_CURRENT
Write-Host ""
if ($errores -eq 0) {
    Write-Host "Hecho." -ForegroundColor Green
} else {
    Write-Host "Algun ajuste no se pudo aplicar (puede que haga falta permiso de administrador)." -ForegroundColor Yellow
    Write-Host "Hazlo a mano en: Configuracion > Sistema > Inicio/apagado y bateria (o Energia)."
}
Write-Host ""
Write-Host "Recuerda tambien (a mano, una sola vez):"
Write-Host "  - Windows Update > Opciones avanzadas > Horas activas: cubre las horas del bot."
Write-Host "  - Configuracion > Hora e idioma: 'Establecer la hora automaticamente' activado."
Read-Host "Pulsa Enter para cerrar"
