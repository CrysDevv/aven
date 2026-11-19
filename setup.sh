#!/usr/bin/env bash
# ============================================================================
# Aven API — Setup de un solo comando
#
# Qué hace:
#   1. Verifica que Docker esté instalado
#   2. Escribe .env (reutiliza los valores de un .env existente; si no hay,
#      deja placeholders claros para las credenciales de Supabase)
#   3. Construye y levanta los contenedores (app + Redis)
#   4. Espera a que la API esté lista
#   5. Se autoprueba contra tu Supabase (conectividad + esquema)
#
# Uso:
#   chmod +x setup.sh
#   ./setup.sh
# ============================================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[ATENCIÓN]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; }

echo "=== Aven API — Setup ==="
echo

# ----------------------------------------------------------------------
# 1. Verificar Docker
# ----------------------------------------------------------------------
if ! command -v docker &> /dev/null; then
    fail "Docker no está instalado. Instálalo desde https://www.docker.com/products/docker-desktop/ y vuelve a correr este script."
    exit 1
fi

if ! docker compose version &> /dev/null; then
    fail "Docker Compose (plugin v2) no está disponible. Actualiza Docker Desktop, o en Linux instala 'docker-compose-plugin'."
    exit 1
fi
info "Docker y Docker Compose están disponibles."

# ----------------------------------------------------------------------
# 2. Escribir .env
# ----------------------------------------------------------------------
env_get() {
    if [ -f .env ]; then
        grep -E "^$1=" .env | head -n1 | cut -d= -f2- || true
    fi
}
val() {
    local v
    v="$(env_get "$1")"
    echo "${v:-$2}"
}

SUPABASE_URL_V="$(val SUPABASE_URL 'https://TU-PROYECTO.supabase.co')"
SUPABASE_PUBLISHABLE_KEY_V="$(val SUPABASE_PUBLISHABLE_KEY 'REEMPLAZAR_CON_PUBLISHABLE_KEY')"
SUPABASE_SECRET_KEY_V="$(val SUPABASE_SECRET_KEY 'REEMPLAZAR_CON_SECRET_KEY')"
SUPABASE_JWKS_URL_V="${SUPABASE_URL_V}/auth/v1/.well-known/jwks.json"

# Las llaves generadas se reutilizan si ya existían: regenerarlas rompería
# las credenciales guardadas en n8n y los tokens cifrados en Supabase.
N8N_ENCRYPTION_KEY_V="$(val N8N_ENCRYPTION_KEY "$(python3 -c 'import secrets; print(secrets.token_hex(32))')")"
N8N_RUNNERS_AUTH_TOKEN_V="$(val N8N_RUNNERS_AUTH_TOKEN "$(python3 -c 'import secrets; print(secrets.token_hex(32))')")"
APP_ENCRYPTION_KEY_V="$(val APP_ENCRYPTION_KEY "$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')")"
GOOGLE_OAUTH_STATE_SECRET_V="$(val GOOGLE_OAUTH_STATE_SECRET "$(python3 -c 'import secrets; print(secrets.token_hex(32))')")"

META_APP_SECRET_V="$(val META_APP_SECRET 'dev-secret-CAMBIAR-EN-PRODUCCION')"
META_VERIFY_TOKEN_V="$(val META_VERIFY_TOKEN 'dev-verify-token-CAMBIAR-EN-PRODUCCION')"
N8N_API_KEY_V="$(val N8N_API_KEY '')"
N8N_BUFFER_WORKFLOW_ID_V="$(val N8N_BUFFER_WORKFLOW_ID '')"
GOOGLE_CLIENT_ID_V="$(val GOOGLE_CLIENT_ID '')"
GOOGLE_CLIENT_SECRET_V="$(val GOOGLE_CLIENT_SECRET '')"

if [ -f .env ]; then
    warn "Ya existe un .env — se respalda como .env.bak antes de sobreescribir."
    cp .env .env.bak
fi

cat > .env << ENVEOF
AVEN_ENV=dev

META_APP_SECRET=${META_APP_SECRET_V}
META_VERIFY_TOKEN=${META_VERIFY_TOKEN_V}

SUPABASE_URL=${SUPABASE_URL_V}
SUPABASE_PUBLISHABLE_KEY=${SUPABASE_PUBLISHABLE_KEY_V}
SUPABASE_SECRET_KEY=${SUPABASE_SECRET_KEY_V}
SUPABASE_JWKS_URL=${SUPABASE_JWKS_URL_V}

N8N_URL=http://n8n:5678
N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY_V}
N8N_RUNNERS_AUTH_TOKEN=${N8N_RUNNERS_AUTH_TOKEN_V}
N8N_API_KEY=${N8N_API_KEY_V}
N8N_BUFFER_WORKFLOW_ID=${N8N_BUFFER_WORKFLOW_ID_V}

APP_ENCRYPTION_KEY=${APP_ENCRYPTION_KEY_V}

GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID_V}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET_V}
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/integrations/google-calendar/callback
GOOGLE_OAUTH_STATE_SECRET=${GOOGLE_OAUTH_STATE_SECRET_V}
ENVEOF
chmod 600 .env
info ".env escrito (permisos restringidos a tu usuario)."

if [[ "$SUPABASE_URL_V" == *TU-PROYECTO* || "$SUPABASE_SECRET_KEY_V" == REEMPLAZAR_* || "$SUPABASE_PUBLISHABLE_KEY_V" == REEMPLAZAR_* ]]; then
    warn "Faltan credenciales de Supabase. Edita .env (SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY) y vuelve a correr ./setup.sh"
    exit 1
fi

# ----------------------------------------------------------------------
# 3. Verificar que el esquema SQL ya esté aplicado en Supabase
# ----------------------------------------------------------------------
PROJECT_REF="${SUPABASE_URL_V#https://}"
PROJECT_REF="${PROJECT_REF%%.*}"

echo
echo "Verificando si el esquema de base de datos ya está aplicado..."
SCHEMA_CHECK=$(curl -s -o /dev/null -w "%{http_code}" \
    "${SUPABASE_URL_V}/rest/v1/tenants?limit=1" \
    -H "apikey: ${SUPABASE_SECRET_KEY_V}" \
    -H "Authorization: Bearer ${SUPABASE_SECRET_KEY_V}" 2>/dev/null || echo "000")

if [ "$SCHEMA_CHECK" == "200" ]; then
    info "El esquema ya está aplicado en Supabase (tabla 'tenants' responde)."
elif [ "$SCHEMA_CHECK" == "000" ]; then
    warn "No se pudo verificar (¿sin conexión a internet ahora mismo?). Continuando de todas formas."
else
    warn "La tabla 'tenants' no respondió como se esperaba (HTTP $SCHEMA_CHECK)."
    echo "    Antes de seguir probando, corre el esquema una sola vez:"
    echo "    1. Entra a https://supabase.com/dashboard/project/${PROJECT_REF}/sql/new"
    echo "    2. Pega el contenido completo de aven_schema.sql (y luego las migraciones)"
    echo "    3. Click en 'Run'"
    echo
    read -p "    ¿Ya lo hiciste, o quieres continuar de todas formas? [Enter para continuar] "
fi

# ----------------------------------------------------------------------
# 4. Construir y levantar
# ----------------------------------------------------------------------
echo
echo "Construyendo y levantando contenedores..."
docker compose up --build -d

# ----------------------------------------------------------------------
# 5. Esperar a que la API esté lista (hasta 60s)
# ----------------------------------------------------------------------
echo
echo -n "Esperando a que la API esté lista"
READY=false
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/health/ready > /dev/null 2>&1; then
        READY=true
        break
    fi
    echo -n "."
    sleep 2
done
echo

if [ "$READY" = false ]; then
    fail "La API no respondió a tiempo. Revisa los logs con: docker compose logs app"
    exit 1
fi
info "API arriba y respondiendo (app + Redis healthy)."

# ----------------------------------------------------------------------
# 6. Autoprueba end-to-end contra tu Supabase
# ----------------------------------------------------------------------
echo
echo "Probando el flujo completo (crea un tenant + usuario de prueba en tu Supabase)..."
JWT_TEST=$(curl -s -X POST http://localhost:8000/api/dev/generate-test-jwt)

if echo "$JWT_TEST" | grep -q "access_token"; then
    info "¡Conexión a Supabase funcionando de punta a punta! Se creó un tenant y usuario de prueba."
    TENANT_ID=$(echo "$JWT_TEST" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tenant_id','?'))" 2>/dev/null || echo "?")
    echo "    tenant_id de prueba creado: $TENANT_ID"
else
    warn "La prueba end-to-end con Supabase falló. Respuesta del servidor:"
    echo "    $JWT_TEST"
    echo
    echo "    Causa más probable: el esquema SQL aún no está aplicado (ver paso 3 arriba)."
fi

# ----------------------------------------------------------------------
# 7. Resumen final
# ----------------------------------------------------------------------
echo
echo "======================================================================"
echo -e "${GREEN}Todo listo.${NC} Abre http://localhost:8000 para el panel de pruebas."
echo
if [ -z "$(grep '^N8N_API_KEY=.' .env 2>/dev/null)" ]; then
    warn "Paso manual pendiente: genera tu API key de n8n."
    echo "    1. Entra a http://localhost:5678"
    echo "    2. Crea tu cuenta de owner (queda solo en tu instancia local)"
    echo "    3. Ve a Settings > n8n API > Create an API key"
    echo "    4. Pega el valor en .env como N8N_API_KEY=..."
    echo "    5. Reinicia: docker compose restart app rq-worker"
    echo
fi
echo "Comandos útiles:"
echo "  docker compose logs -f app     # ver logs en vivo"
echo "  docker compose down            # apagar todo"
echo "  docker compose restart app     # reiniciar solo la API"
echo "======================================================================"
