# Arranque de un solo clic del bot de ballenas en Windows.
# Lo lanza INICIAR.bat. Hace todo lo necesario y solo lo que falta (y reinicia el bot si se cae por un fallo):
#   1. Busca Python 3.10+; si no lo hay, lo instala (winget o instalador oficial, sin permisos de administrador).
#   2. Crea el entorno del bot e instala las dependencias (solo si han cambiado).
#   3. Crea .env si no existe y un acceso directo en el escritorio.
#   4. Si no existe estrategia_elegida.json, ofrece pasar el laboratorio (elige y valida las estrategias).
#   5. Abre el panel (en segundo plano) y arranca el bot en esta ventana.

param([switch]$SoloInstalar)

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location $Raiz
$Host.UI.RawUI.WindowTitle = "Bot de ballenas - NO CERRAR ESTA VENTANA"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Paso($texto) { Write-Host ""; Write-Host ">> $texto" -ForegroundColor Cyan }
function Fallo($texto) {
    Write-Host ""; Write-Host "ERROR: $texto" -ForegroundColor Red
    Write-Host "Copia este mensaje y pasaselo a Claude."
    Read-Host "Pulsa Enter para cerrar"; exit 1
}

function Probar-Python($exe, $extra) {
    try {
        $v = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 10) { return $true }
    } catch {}
    return $false
}

function Buscar-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) { if (Probar-Python "py" @("-3")) { return ,@("py", "-3") } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    # Ignora el "python" falso de la Microsoft Store (carpeta WindowsApps)
    if ($python -and $python.Source -notlike "*WindowsApps*") { if (Probar-Python $python.Source @()) { return ,@($python.Source) } }
    $rutas = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue) +
             @(Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue)
    foreach ($r in ($rutas | Sort-Object FullName -Descending)) { if (Probar-Python $r.FullName @()) { return ,@($r.FullName) } }
    return $null
}

# ---------------------------------------------------------------- 1. Python
Paso "Comprobando Python..."
$Py = Buscar-Python
if (-not $Py) {
    Paso "No hay Python 3.10 o superior. Instalandolo (2-3 minutos, sin permisos de administrador)..."
    $instalado = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        $instalado = ($LASTEXITCODE -eq 0)
    }
    if (-not $instalado) {
        $url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
        $tmp = Join-Path $env:TEMP "python-instalador.exe"
        Write-Host "Descargando Python desde python.org..."
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        Start-Process -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
    }
    $Py = Buscar-Python
    if (-not $Py) { Fallo "No se pudo instalar Python. Instalalo a mano desde python.org marcando 'Add python.exe to PATH'." }
}
Write-Host "Python OK."

# ------------------------------------------------------------ 2. entorno
$VenvPy = Join-Path $Raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Paso "Creando el entorno del bot..."
    $exe = $Py[0]; $extra = @($Py | Select-Object -Skip 1)
    & $exe @extra -m venv .venv
    if (-not (Test-Path $VenvPy)) { Fallo "No se pudo crear el entorno de Python (.venv)." }
}
$HashReq = (Get-FileHash requirements.txt -Algorithm SHA256).Hash
$MarcaReq = Join-Path $Raiz ".venv\requisitos.hash"
if (-not (Test-Path $MarcaReq) -or (Get-Content $MarcaReq -Raw).Trim() -ne $HashReq) {
    Paso "Instalando lo que necesita el bot (solo la primera vez o tras actualizar)..."
    & $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $VenvPy -m pip install -r requirements.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Fallo "Fallo la instalacion de dependencias (revisa tu conexion a internet)." }
    Set-Content -Path $MarcaReq -Value $HashReq
}
Write-Host "Dependencias OK."

# --------------------------------------------------- 3. .env y acceso directo
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Creado .env (en modo simulado no hace falta tocarlo)." }
try {
    $Atajo = Join-Path ([Environment]::GetFolderPath("Desktop")) "Bot de ballenas.lnk"
    if (-not (Test-Path $Atajo)) {
        $sh = New-Object -ComObject WScript.Shell
        $lnk = $sh.CreateShortcut($Atajo)
        $lnk.TargetPath = Join-Path $Raiz "INICIAR.bat"
        $lnk.WorkingDirectory = $Raiz
        $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,43"
        $lnk.Save()
        Write-Host "Creado el acceso directo 'Bot de ballenas' en el escritorio."
    }
} catch {}

if ($SoloInstalar) { Write-Host ""; Write-Host "Instalacion completa." -ForegroundColor Green; Read-Host "Pulsa Enter para cerrar"; exit 0 }

# Pregunta S/N con tiempo límite: si nadie responde (p. ej. arranque automático o desatendido), se toma "N"
# y el bot arranca igualmente. Así una pregunta nunca deja el bot parado esperando.
function PreguntarSN([string]$texto, [int]$segundos) {
    Write-Host -NoNewline "$texto (S/N; si no respondes en $segundos s: N): "
    $respuesta = ""
    try {
        $fin = (Get-Date).AddSeconds($segundos)
        while ((Get-Date) -lt $fin) {
            if ([Console]::KeyAvailable) {
                $k = [Console]::ReadKey($true)
                if ($k.Key -eq "Enter") { break }
                $respuesta += $k.KeyChar
                Write-Host -NoNewline $k.KeyChar
            } else { Start-Sleep -Milliseconds 100 }
        }
    } catch { $respuesta = "" }
    Write-Host ""
    return $respuesta
}

# ----------------------------------------------------- 4. laboratorio inicial
if (-not (Test-Path "estrategia_elegida.json")) {
    Write-Host ""
    Write-Host "Todavia no has pasado el LABORATORIO: prueba las estrategias con anos de datos reales de Hyperliquid,"
    Write-Host "y deja EVIDENCIA de cuales funcionan. No activa el dinero real: eso solo lo aprueba una persona (promocion.py)."
    $r = PreguntarSN "Quieres hacerlo ahora? La primera vez tarda 20-40 min" 30
    if ($r -match '^[sS]') {
        & $VenvPy laboratorio.py --guardar-memoria
        Write-Host ""
        Read-Host "Revisa los resultados de arriba (pasaselos a Claude). Pulsa Enter para arrancar el bot"
    }
}

# ------------------------------------------------------ 5. panel + bot
Paso "Abriendo el panel..."
$panel = $null
$panelVivo = $false
try { Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/datos" -TimeoutSec 2 -UseBasicParsing | Out-Null; $panelVivo = $true }
catch { if ($_.Exception.Response) { $panelVivo = $true } }   # responde (aunque pida contraseña): ya estaba abierto
if ($panelVivo) {
    # Hay un panel abierto de antes: se cierra para arrancar la version actual (por si el bot se ha actualizado).
    $reiniciado = $false
    try {
        $pids = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($id in $pids) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -match "panel\.py") {
                Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
                $reiniciado = $true
            }
        }
    } catch {}
    if ($reiniciado) { Start-Sleep -Seconds 2; $panelVivo = $false }
    else { Start-Process "http://localhost:8080" }
}
if (-not $panelVivo) {
    $VenvPyw = Join-Path $Raiz ".venv\Scripts\pythonw.exe"
    $panelExe = if (Test-Path $VenvPyw) { $VenvPyw } else { $VenvPy }
    $panel = Start-Process -FilePath $panelExe -ArgumentList "panel.py --abrir" -WorkingDirectory $Raiz -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    if ($panel.HasExited) {
        Write-Host "AVISO: el panel no ha arrancado. Si lo has abierto a la red (host 0.0.0.0), pon PANEL_PASSWORD en .env." -ForegroundColor Yellow
        Write-Host "El bot funciona igual; para ver el error, abre herramientas\abrir-panel.bat." -ForegroundColor Yellow
        $panel = $null
    }
}

Paso "Comprobando la configuracion..."
# Pasa config.json al formato actual (guarda antes una copia config.json.v1-....bak) y lo valida SIN conectar ni operar.
& $VenvPy bot.py --validar-config
if ($LASTEXITCODE -ne 0) { Fallo "La configuracion no es valida (mira el motivo arriba). El bot NO se ha arrancado." }

Paso "Arrancando el bot. Para pararlo: Ctrl + C o cierra esta ventana."
try {
    while ($true) {
        & $VenvPy bot.py
        $codigo = $LASTEXITCODE
        # 0 = parado a mano; 3 = parada definitiva (pérdida máxima total o config incorrecta): no reiniciar.
        if ($codigo -eq 0 -or $codigo -eq 3) { break }
        if ($codigo -eq 4) {
            # Reinicio ordenado pedido por el propio bot (p. ej. para aplicar una configuracion guardada desde el panel)
            Write-Host ""
            Write-Host "Reiniciando el bot para aplicar la configuracion nueva..." -ForegroundColor Cyan
            Start-Sleep -Seconds 2
            continue
        }
        Write-Host ""
        Write-Host "El bot se ha cerrado por un fallo (codigo $codigo). Lo reinicio en 30 segundos... (Ctrl + C para no hacerlo)" -ForegroundColor Yellow
        Start-Sleep -Seconds 30
    }
} finally {
    if ($panel -and -not $panel.HasExited) { Stop-Process -Id $panel.Id -ErrorAction SilentlyContinue }
}
Write-Host ""
Write-Host "El bot se ha detenido." -ForegroundColor Yellow
Read-Host "Pulsa Enter para cerrar"
