# Conecta este PC a Cloudflare Tunnel para ver el panel desde cualquier sitio.
# Antes, en el panel de Cloudflare, crea el túnel y copia el comando de instalación (ver LEEME).
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Buscar-Cloudflared {
    $c = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($r in @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe")) {
        if (Test-Path $r) { return $r }
    }
    return $null
}

$exe = Buscar-Cloudflared
if (-not $exe) {
    Write-Host ">> Instalando cloudflared (el conector oficial de Cloudflare)..." -ForegroundColor Cyan
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "No tienes winget. Descarga cloudflared para Windows desde la web de Cloudflare y vuelve a ejecutar esto." -ForegroundColor Red
        Read-Host "Pulsa Enter para cerrar"; exit 1
    }
    winget install -e --id Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements
    $exe = Buscar-Cloudflared
    if (-not $exe) { Write-Host "No se pudo instalar cloudflared." -ForegroundColor Red; Read-Host "Pulsa Enter para cerrar"; exit 1 }
}
Write-Host "cloudflared OK: $exe"
Write-Host ""
Write-Host "Pega el comando o el token que te da Cloudflare al crear el tunel (el texto largo que empieza por 'eyJ'):"
$entrada = Read-Host "Token"
$token = ($entrada -split '\s+' | Where-Object { $_ -match '^eyJ' } | Select-Object -First 1)
if (-not $token) { Write-Host "No encuentro el token (debe empezar por eyJ)." -ForegroundColor Red; Read-Host "Pulsa Enter para cerrar"; exit 1 }

Write-Host ""
Write-Host ">> Instalando el tunel como servicio de Windows (arranca solo con el PC)." -ForegroundColor Cyan
Write-Host "   Windows te pedira permiso de administrador: pulsa Si."
Start-Process -FilePath $exe -ArgumentList "service install $token" -Verb RunAs -Wait
Start-Sleep -Seconds 5
$serv = Get-Service -Name cloudflared -ErrorAction SilentlyContinue
if ($serv -and $serv.Status -eq "Running") {
    Write-Host "Tunel conectado y en marcha." -ForegroundColor Green
    Write-Host "Vuelve al panel de Cloudflare: deberia salir el tunel como 'Healthy'. Sigue con el paso de la ruta (LEEME)."
} else {
    Write-Host "El servicio no aparece en marcha todavia. Espera un minuto y revisa en Cloudflare si el tunel sale 'Healthy'." -ForegroundColor Yellow
}
Read-Host "Pulsa Enter para cerrar"
