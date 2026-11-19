# ============================================================================
# Aven API - Setup de un solo comando (Windows / PowerShell)
#
# Que hace:
#   1. Verifica que Docker Desktop este instalado Y corriendo
#   2. Escribe .env (reutiliza los valores de un .env existente; si no hay,
#      deja placeholders claros para las credenciales de Supabase)
#   3. Construye y levanta los contenedores (app + Redis)
#   4. Espera a que la API este lista
#   5. Se autoprueba contra tu Supabase (conectividad + esquema)
#
# Uso (en PowerShell, no en cmd.exe):
#   .\setup.ps1
#
# Si Windows bloquea la ejecucion de scripts la primera vez:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# Nota: este archivo es ASCII puro a proposito, para que Windows PowerShell 5.1
# lo lea igual con cualquier codificacion.
# ============================================================================

$ErrorActionPreference = "Stop"

function Write-Info    { param($msg) Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-WarnMsg { param($msg) Write-Host "[ATENCION] $msg" -ForegroundColor Yellow }
function Write-Fail    { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

function Get-EnvValue {
    param([string]$Key, [string]$Default)
    if (Test-Path ".env") {
        $line = Select-String -Path ".env" -Pattern "^$Key=(.*)$" | Select-Object -First 1
        if ($line) {
            $v = $line.Matches[0].Groups[1].Value.Trim()
            if ($v) { return $v }
        }
    }
    return $Default
}

function New-RandomBytes {
    param([int]$Count = 32)
    $bytes = New-Object byte[] $Count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return ,$bytes
}

function New-HexKey {
    return ((New-RandomBytes 32) | ForEach-Object { $_.ToString("x2") }) -join ""
}

Write-Host "=== Aven API - Setup (Windows) ===" -ForegroundColor Cyan
Write-Host ""

# ----------------------------------------------------------------------
# 1. Verificar Docker instalado y corriendo
# ----------------------------------------------------------------------
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Fail "Docker no esta instalado o no esta en el PATH. Instala Docker Desktop desde https://www.docker.com/products/docker-desktop/ y vuelve a correr este script."
    exit 1
}

# docker.exe es un ejecutable externo: no lanza excepciones capturables con
# try/catch, solo deja un $LASTEXITCODE distinto de 0. Se revisa explicitamente.
$null = docker compose version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker Compose (plugin v2) no esta disponible. Actualiza Docker Desktop a una version reciente."
    exit 1
}

$null = docker ps 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker esta instalado pero el motor no esta corriendo. Abre Docker Desktop, espera a que el icono de la ballena quede quieto, y vuelve a correr este script."
    exit 1
}
Write-Info "Docker Desktop esta instalado y corriendo."

# ----------------------------------------------------------------------
# 2. Escribir .env
# ----------------------------------------------------------------------
$supabaseUrl            = Get-EnvValue "SUPABASE_URL" "https://TU-PROYECTO.supabase.co"
$supabasePublishableKey = Get-EnvValue "SUPABASE_PUBLISHABLE_KEY" "REEMPLAZAR_CON_PUBLISHABLE_KEY"
$supabaseSecretKey      = Get-EnvValue "SUPABASE_SECRET_KEY" "REEMPLAZAR_CON_SECRET_KEY"
$supabaseJwksUrl        = "$supabaseUrl/auth/v1/.well-known/jwks.json"

# Las llaves generadas se reutilizan si ya existian: regenerarlas romperia las
# credenciales guardadas en n8n y los tokens cifrados en Supabase.
$fernetDefault = [Convert]::ToBase64String((New-RandomBytes 32)).Replace('+','-').Replace('/','_')
$n8nEncryptionKey       = Get-EnvValue "N8N_ENCRYPTION_KEY" (New-HexKey)
$n8nRunnersAuthToken    = Get-EnvValue "N8N_RUNNERS_AUTH_TOKEN" (New-HexKey)
$appEncryptionKey       = Get-EnvValue "APP_ENCRYPTION_KEY" $fernetDefault
$googleOauthStateSecret = Get-EnvValue "GOOGLE_OAUTH_STATE_SECRET" (New-HexKey)

$metaAppSecret       = Get-EnvValue "META_APP_SECRET" "dev-secret-CAMBIAR-EN-PRODUCCION"
$metaVerifyToken     = Get-EnvValue "META_VERIFY_TOKEN" "dev-verify-token-CAMBIAR-EN-PRODUCCION"
$n8nApiKey           = Get-EnvValue "N8N_API_KEY" ""
$n8nBufferWorkflowId = Get-EnvValue "N8N_BUFFER_WORKFLOW_ID" ""
$googleClientId      = Get-EnvValue "GOOGLE_CLIENT_ID" ""
$googleClientSecret  = Get-EnvValue "GOOGLE_CLIENT_SECRET" ""

if (Test-Path ".env") {
    Write-WarnMsg "Ya existe un .env - se respalda como .env.bak antes de sobreescribir."
    Copy-Item ".env" ".env.bak" -Force
}

$envContent = @"
AVEN_ENV=dev

META_APP_SECRET=$metaAppSecret
META_VERIFY_TOKEN=$metaVerifyToken

SUPABASE_URL=$supabaseUrl
SUPABASE_PUBLISHABLE_KEY=$supabasePublishableKey
SUPABASE_SECRET_KEY=$supabaseSecretKey
SUPABASE_JWKS_URL=$supabaseJwksUrl

N8N_URL=http://n8n:5678
N8N_ENCRYPTION_KEY=$n8nEncryptionKey
N8N_RUNNERS_AUTH_TOKEN=$n8nRunnersAuthToken
N8N_API_KEY=$n8nApiKey
N8N_BUFFER_WORKFLOW_ID=$n8nBufferWorkflowId

APP_ENCRYPTION_KEY=$appEncryptionKey

GOOGLE_CLIENT_ID=$googleClientId
GOOGLE_CLIENT_SECRET=$googleClientSecret
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/integrations/google-calendar/callback
GOOGLE_OAUTH_STATE_SECRET=$googleOauthStateSecret
"@

# UTF8 sin BOM - evita que Docker/Python se quejen de un byte de mas al leer el .env
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), $envContent, $utf8NoBom)
Write-Info ".env escrito."

if ($supabaseUrl -like "*TU-PROYECTO*" -or $supabaseSecretKey -like "REEMPLAZAR_*" -or $supabasePublishableKey -like "REEMPLAZAR_*") {
    Write-WarnMsg "Faltan credenciales de Supabase. Edita .env (SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY) y vuelve a correr .\setup.ps1"
    exit 1
}

# ----------------------------------------------------------------------
# 3. Verificar que el esquema SQL ya este aplicado en Supabase
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Verificando si el esquema de base de datos ya esta aplicado..."

$projectRef = ($supabaseUrl -replace "^https://", "") -replace "\..*$", ""
$schemaOk = $false
try {
    $headers = @{
        "apikey"        = $supabaseSecretKey
        "Authorization" = "Bearer $supabaseSecretKey"
    }
    $resp = Invoke-WebRequest -Uri "$supabaseUrl/rest/v1/tenants?limit=1" `
                               -Headers $headers -UseBasicParsing -TimeoutSec 10
    if ($resp.StatusCode -eq 200) { $schemaOk = $true }
}
catch {
    $statusCode = $null
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
        $statusCode = [int]$_.Exception.Response.StatusCode
    }
    if ($statusCode -eq 200) { $schemaOk = $true }
}

if ($schemaOk) {
    Write-Info "El esquema ya esta aplicado en Supabase (tabla 'tenants' responde)."
}
else {
    Write-WarnMsg "La tabla 'tenants' no respondio como se esperaba."
    Write-Host "    Antes de seguir probando, corre el esquema una sola vez:"
    Write-Host "    1. Entra a https://supabase.com/dashboard/project/$projectRef/sql/new"
    Write-Host "    2. Pega el contenido completo de aven_schema.sql (y luego las migraciones)"
    Write-Host "    3. Click en 'Run'"
    Write-Host ""
    Read-Host "    Presiona Enter para continuar de todas formas"
}

# ----------------------------------------------------------------------
# 4. Construir y levantar
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Construyendo y levantando contenedores..."
docker compose up --build -d
if ($LASTEXITCODE -ne 0) {
    Write-Fail "docker compose fallo al levantar los contenedores. Revisa el mensaje de error arriba."
    exit 1
}

# ----------------------------------------------------------------------
# 5. Esperar a que la API este lista (hasta 60s)
# ----------------------------------------------------------------------
Write-Host ""
Write-Host -NoNewline "Esperando a que la API este lista"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/health/ready" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # Aun no responde - normal mientras el contenedor termina de arrancar
    }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 2
}
Write-Host ""

if (-not $ready) {
    Write-Fail "La API no respondio a tiempo. Revisa los logs con: docker compose logs app"
    exit 1
}
Write-Info "API arriba y respondiendo (app + Redis healthy)."

# ----------------------------------------------------------------------
# 6. Autoprueba end-to-end contra tu Supabase
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "Probando el flujo completo (crea un tenant + usuario de prueba en tu Supabase)..."
try {
    $jwtTest = Invoke-RestMethod -Uri "http://localhost:8000/api/dev/generate-test-jwt" -Method Post -TimeoutSec 15
    if ($jwtTest.access_token) {
        Write-Info "Conexion a Supabase funcionando de punta a punta! Se creo un tenant y usuario de prueba."
        Write-Host "    tenant_id de prueba creado: $($jwtTest.tenant_id)"
    } else {
        Write-WarnMsg "Respuesta inesperada del servidor: $($jwtTest | ConvertTo-Json -Compress)"
    }
}
catch {
    Write-WarnMsg "La prueba end-to-end con Supabase fallo."
    Write-Host "    $($_.Exception.Message)"
    Write-Host ""
    Write-Host "    Causa mas probable: el esquema SQL aun no esta aplicado (ver paso 3 arriba)."
}

# ----------------------------------------------------------------------
# 7. Resumen final
# ----------------------------------------------------------------------
Write-Host ""
Write-Host "======================================================================"
Write-Host "Todo listo. Abre http://localhost:8000 para el panel de pruebas." -ForegroundColor Green
Write-Host ""

$envRaw = Get-Content ".env" -Raw
if ($envRaw -notmatch "(?m)^N8N_API_KEY=.+") {
    Write-WarnMsg "Paso manual pendiente: genera tu API key de n8n."
    Write-Host "    1. Entra a http://localhost:5678"
    Write-Host "    2. Crea tu cuenta de owner (queda solo en tu instancia local)"
    Write-Host "    3. Ve a Settings > n8n API > Create an API key"
    Write-Host "    4. Pega el valor en .env como N8N_API_KEY=..."
    Write-Host "    5. Reinicia: docker compose restart app rq-worker"
    Write-Host ""
}

Write-Host "Comandos utiles:"
Write-Host "  docker compose logs -f app     # ver logs en vivo"
Write-Host "  docker compose down            # apagar todo"
Write-Host "  docker compose restart app     # reiniciar solo la API"
Write-Host "======================================================================"
