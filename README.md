# Aven — Multi-Tenant Event-Driven AI Orchestration Backend

Backend SaaS multi-tenant orientado a eventos para el aprovisionamiento dinámico, aislamiento y ejecución de flujos de automatización e inteligencia artificial (**n8n headless**), con ingesta asíncrona de mensajería (**Meta / WhatsApp Cloud API** como adaptador inicial), búsqueda semántica (**pgvector**) e integraciones OAuth2 por cliente (**Google Calendar**).

Diseñado bajo principios de **defensa en profundidad (OWASP API Top 10)**, aislamiento estricto por `tenant_id` en todas las capas y procesamiento asíncrono tolerante a fallos.

---

## Arquitectura del Sistema

```text
[Meta Cloud API / Webhooks]
       │  POST /api/webhooks/meta (HMAC-SHA256 + Pydantic + Rate Limit)
       ▼
[FastAPI Gateway] ──► [Redis SET NX (Deduplicación 24h)] ──► [Cola RQ: incoming_messages]
                                                                    │
                                                                    ▼
[Supabase (PostgreSQL + RLS + pgvector)] ◄────────────── [RQ Worker (Síncrono)]
       │                                                            │
       │  (Catálogo, Ledger append-only, Tokens Fernet)             │ POST /webhook/wh-{tenant_id}
       ▼                                                            ▼
[n8n Headless + Task Runners Aislados] ◄─────────────── [Buffer Compartido (Redis INCR)]
       │
       ├──► AI Agent (LLM + Memoria aislada por tenant:cliente)
       ├──► Postgres Tool (Consultas parametrizadas con tenant_id fijo)
       ├──► RAG Vector Store (pgvector HNSW)
       └──► Google Calendar API (OAuth2 token inyectado por ejecución)
```

### Pilares técnicos implementados

* **Aislamiento Multi-Tenant en 3 Capas (Anti-BOLA / API1:2023):**
  1. **Capa 1 (Identidad):** Extracción de `tenant_id` exclusivamente desde `app_metadata` de JWTs firmados con **ES256**, validados contra el endpoint **JWKS** de Supabase (`app/security.py`). Ningún endpoint acepta `tenant_id` desde el cliente.
  2. **Capa 2 (Acceso a Datos):** *Repository Pattern* (`app/repository.py`) con filtro obligatorio por `tenant_id` en cada consulta y *threadpool* de FastAPI para evitar bloquear el *event loop*.
  3. **Capa 3 (Base de Datos):** *Row Level Security (RLS)* en PostgreSQL + políticas de inmutabilidad (`block_mutation()`) sobre tablas de auditoría y contabilidad (`wallet_ledger`, `audit_log`).
* **Ingesta Asíncrona y Desacoplada:** Verificación criptográfica HMAC sobre bytes crudos (`hmac.compare_digest`), deduplicación atómica en Redis (`SET NX`), encolamiento en **RQ** y procesamiento resiliente en worker dedicado (`app/worker.py`) con control de ventana de servicio, saldo en *ledger* y degradación elegante ante fallos de terceros.
* **Motor de Plantillas y Orquestación Headless (`n8n`):** Sustitución segura de variables con escapado JSON (`app/template_engine.py`), bloqueo de nodos peligrosos (`executeCommand`, `readWriteFile`, `ssh`), enrutamiento determinístico por tenant (`wh-{tenant_id}`) y mitigación del bug de registro de webhooks en n8n (`n8n-io/n8n#21614`).
* **Cifrado y Gestión de Secretos:** Credenciales OAuth2 de terceros cifradas en aplicación con **Fernet** antes de persistirse (`app/crypto.py`) y estados OAuth firmados con HMAC y TTL (`app/google_oauth.py`).

---

## Stack Tecnológico

| Capa | Tecnología |
|---|---|
| **API & Orquestación** | Python 3.12, FastAPI, Uvicorn, Pydantic v2, `slowapi` |
| **Colas & Caché** | Redis, RQ (Redis Queue) |
| **Base de Datos & Auth** | Supabase (PostgreSQL 16, `pgvector` HNSW, RLS, Auth JWKS ES256) |
| **Motor de Automatización** | n8n self-hosted (`2.34.1`) + Task Runners externos aislados |
| **Criptografía & Seguridad** | PyJWT (`ES256`), `cryptography` (Fernet), HMAC-SHA256, Security Headers |
| **Infraestructura** | Docker, Docker Compose (dev + prod), Caddy (HTTPS automático) |

---

## Inicio Rápido

### 1. Preparar la base de datos en Supabase
En el **SQL Editor** de tu proyecto en Supabase, ejecuta en orden:
1. `aven_schema.sql`
2. `migrations_002_rag_and_inventory.sql`
3. `migrations_003_catalog_and_integrations.sql`

### 2. Levantar el entorno con un comando

**macOS / Linux:**
```bash
chmod +x setup.sh
./setup.sh
```

**Windows (PowerShell):**
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```

En la primera ejecución, el script generará el archivo `.env` con secretos criptográficos aleatorios y solicitará completar `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` y `SUPABASE_SECRET_KEY`. Al volver a ejecutarlo, construirá los contenedores, verificará el esquema y correrá una prueba end-to-end contra Supabase.

### 3. Configurar n8n (única vez)
1. Abre `http://localhost:5678` y crea la cuenta local de administrador.
2. Ve a **Settings → n8n API → Create an API key** y guárdala en `.env` como `N8N_API_KEY`.
3. Despliega el sub-flujo compartido `templates/00-shared-message-buffer/workflow.json` y guarda su ID en `N8N_BUFFER_WORKFLOW_ID`.
4. Reinicia los servicios de aplicación:
   ```bash
   docker compose restart app rq-worker
   ```

Panel de pruebas interactivo (disponible cuando `AVEN_ENV=dev`): `http://localhost:8000`

---

## Estructura del Repositorio

```text
├── app/
│   ├── main.py                  # App FastAPI, middlewares de seguridad y montaje de routers
│   ├── config.py                # Configuración tipada vía pydantic-settings
│   ├── security.py              # Validación JWT (JWKS ES256) y firmas HMAC de webhooks
│   ├── repository.py            # Repository Pattern con aislamiento estricto por tenant_id
│   ├── worker.py                # Consumidor RQ: persistencia, reglas de negocio y disparo a n8n
│   ├── queue_client.py          # Cliente síncrono de encolamiento RQ
│   ├── redis_client.py          # Cliente asíncrono de Redis (deduplicación y readiness)
│   ├── template_engine.py       # Renderizado, validación y sanitización de workflows JSON
│   ├── n8n_client.py            # Cliente REST para n8n con mitigación de registro de webhooks
│   ├── google_oauth.py          # Flujo OAuth2 con state firmado HMAC y refresh automático
│   ├── crypto.py                # Cifrado simétrico Fernet (inicialización lazy)
│   ├── schemas.py               # Modelos Pydantic de validación de entrada
│   └── routers/                 # Endpoints REST (webhooks, templates, catalog, integrations, wallet, conversations, health, dev_tools)
├── templates/
│   ├── 00-shared-message-buffer/ # Sub-flujo multi-tenant de consolidación de ráfagas en Redis
│   ├── 01..05-*/                # Plantillas base de demostración
│   └── 06-motor-taller/         # Flujo completo: AI Agent + Memoria Redis + SQL Tool + RAG + Calendar
├── aven_schema.sql              # Esquema base, índices HNSW, RLS y triggers append-only
├── migrations_002_*.sql         # Función RPC match_documents aislada por tenant
├── migrations_003_*.sql         # Catálogo unificado (productos/servicios) e integraciones OAuth
├── docker-compose.yml           # Orquestación base (app, rq-worker, redis, n8n, n8n-runner)
├── docker-compose.prod.yml      # Configuración de producción con Caddy HTTPS
└── DOCUMENTACION.md             # Referencia técnica detallada de arquitectura, módulos y seguridad
```