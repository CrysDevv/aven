# AVEN — Documentación Técnica Exhaustiva (v3.0)
### Arquitectura, Base de Datos, Módulos Backend, Flujos y Seguridad

**Versión:** 3.0  
**Alcance:** Describe la implementación exacta del repositorio `aven-api` (esquema SQL, backend FastAPI, worker RQ, plantillas n8n y controles de seguridad).

---

## Índice

1. Visión General y Principios de Diseño
2. Base de Datos — Tablas, Índices, Funciones SQL y RLS
3. Backend Python — Módulo por Módulo
4. API REST — Endpoints en Detalle
5. Sistema de Plantillas y Flujos n8n
6. Flujos End-to-End (con referencias cruzadas)
7. Modelo de Seguridad y Mitigaciones OWASP API Top 10
8. Infraestructura y Despliegue
9. Estrategia de Verificación y Pruebas
10. Alcance Actual y Trabajo Futuro

---

## 1. Visión General y Principios de Diseño

### 1.1 Qué es Aven

Aven es una **plataforma backend SaaS multi-tenant orientada a eventos** diseñada para orquestar flujos de automatización e inteligencia artificial (**n8n headless**), gestionar catálogos de productos/servicios, administrar saldos de consumo mediante un libro contable inmutable (*append-only ledger*) y conectar integraciones externas por cliente (como Google Calendar).

Para la ingesta y entrega de mensajes conversacionales, el sistema implementa un **adaptador desacoplado de webhooks** (actualmente integrado con **Meta / WhatsApp Cloud API**), separado del motor de procesamiento mediante colas **Redis / RQ**.

### 1.2 Principios de diseño

1. **El `tenant_id` nunca proviene del cliente.** Siempre se deriva de: (a) el JWT validado (`get_current_tenant`), o (b) el contexto interno del sistema (resolución por `phone_number_id` en webhooks entrantes, worker o variables de sistema del motor de plantillas). Ningún endpoint acepta `tenant_id` como entrada directa.
2. **Defensa en profundidad, nunca una sola capa.** Cada operación sensible combina múltiples barreras independientes: RLS en PostgreSQL + filtro obligatorio en el *Repository Pattern* + verificación de pertenencia en el endpoint.
3. **Degradación elegante sobre fallo total.** Si una dependencia auxiliar falla (por ejemplo, Google Calendar no conectado o fallo al refrescar un token OAuth), el worker continúa ejecutando el flujo sin esa pieza en lugar de descartar la interacción.
4. **Fallo ruidoso ante plantillas malformadas.** `template_engine.py` valida variables requeridas, variables no declaradas, sintaxis JSON resultante y tipos de nodo permitidos antes de tocar la API de n8n.
5. **No bloqueo del *event loop* ASGI.** Las operaciones contra el cliente síncrono de Supabase (`supabase-py`) se ejecutan en rutas síncronas (`def`) o mediante `run_in_threadpool` para preservar la concurrencia de Uvicorn.

### 1.3 Stack tecnológico

| Capa | Tecnología | Versión / Detalle |
|---|---|---|
| API / Orquestador | FastAPI (Python 3.12) | `0.141.1` |
| Servidor ASGI | Uvicorn | `0.52.4` |
| Base de datos | Supabase (PostgreSQL + pgvector) | Modelo Single-DB Multi-Tenant |
| Cliente Supabase | `supabase-py` | `2.31.0` |
| Autenticación | Supabase Auth (JWT ES256, JWKS) | Verificación asimétrica |
| Verificación JWT | PyJWT con soporte JWKS | `2.13.0` |
| Cola de trabajos | RQ (Redis Queue) | `2.11.0` |
| Buffer / Deduplicación | Redis (`asyncio` + `sync`) | Cliente `8.1.0` |
| Motor de automatización | n8n self-hosted | `2.34.1` (versión fijada) |
| Aislamiento de código | n8n Task Runners externos | `n8nio/runners:2.34.1` |
| Rate limiting | `slowapi` | `0.1.10` |
| Cifrado a nivel de app | `cryptography` (Fernet) | Inicialización lazy |
| Cliente HTTP | `httpx` | `0.28.1` |
| Proxy HTTPS (producción) | Caddy | `2-alpine` |

---

## 2. Base de Datos — Tablas, Índices, Funciones SQL y RLS

Modelo: **Single Database, Multi-Tenant**. Todas las tablas de negocio incluyen `tenant_id uuid references tenants(id) on delete cascade`, *Row Level Security (RLS)* habilitado y políticas basadas en `auth_tenant_id()`.

Archivos SQL aplicados secuencialmente:
1. `aven_schema.sql` (esquema base, extensiones, ledger, RLS y triggers de inmutabilidad)
2. `migrations_002_rag_and_inventory.sql` (función RPC `match_documents` aislada por tenant)
3. `migrations_003_catalog_and_integrations.sql` (catálogo unificado de productos/servicios e integraciones OAuth2)

### 2.1 Extensiones requeridas
* `pgcrypto`: generación de UUIDs (`gen_random_uuid()`) y funciones criptográficas.
* `vector` (`pgvector`): almacenamiento e indexación HNSW de embeddings de 1536 dimensiones.

### 2.2 Funciones SQL principales

* **`set_updated_at()`**: Trigger genérico `BEFORE UPDATE` que actualiza `updated_at = now()`.
* **`auth_tenant_id()`**: Extrae `(auth.jwt() -> 'app_metadata' ->> 'tenant_id')::uuid` para evaluar políticas RLS.
* **`get_wallet_balance(p_tenant_id uuid)`**: Calcula `COALESCE(SUM(amount_messages), 0)` sobre `wallet_ledger`. El saldo nunca se almacena como columna mutable.
* **`get_tenant_storage_usage_mb(p_tenant_id uuid)`**: Calcula el almacenamiento consumido en MB sumando `file_size_bytes` de `tenant_files`.
* **`match_documents(query_embedding vector(1536), p_tenant_id uuid, match_count int default 5, filter jsonb default '{}')`**: Búsqueda de similitud coseno (`<=>`) sobre `knowledge_vectors`. Exige `p_tenant_id` como parámetro obligatorio (sin valor por defecto) y filtra `WHERE knowledge_vectors.tenant_id = p_tenant_id AND knowledge_vectors.metadata @> filter`, eliminando cualquier posibilidad de fuga entre tenants si `filter` se omite.
* **`block_mutation()`**: Trigger `BEFORE UPDATE OR DELETE` que lanza excepción en tablas *append-only* (`audit_log` y `wallet_ledger`).

### 2.3 Tablas del esquema

#### `tenants`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `uuid PK` | Identificador único del negocio |
| `business_name` | `text not null` | Nombre comercial |
| `plan` | `text not null` | `starter`, `growth`, `pro`, `agencia`, `enterprise` |
| `ai_globally_paused` | `boolean` | Pausa global de automatización consultada por `worker.py` |
| `storage_limit_mb` | `integer` | Cuota de almacenamiento (default `200`) |
| `created_at`, `updated_at` | `timestamptz` | Auditoría temporal |

#### `tenant_members`
Vincula usuarios de `auth.users` con un `tenant_id` y un rol (`owner` / `staff`), con restricción `unique(tenant_id, user_id)`.

#### `meta_connections`
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `uuid PK` | |
| `tenant_id` | `uuid FK` | |
| `waba_id` | `text not null` | ID de cuenta de mensajería |
| `phone_number_id` | `text not null unique` | Único a nivel global; clave de enrutamiento para webhooks entrantes |
| `n8n_workflow_id` | `text` | ID del flujo activo desplegado en n8n |
| `access_token_enc` | `bytea` | Reservado para cifrado de token del canal |
| `status` | `text` | `pending`, `active`, `disconnected` |
| `connected_at`, `created_at`, `updated_at` | `timestamptz` | |

#### `knowledge_vectors` (RAG)
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `uuid PK` | |
| `tenant_id` | `uuid FK` | Índice B-Tree dedicado |
| `content` | `text not null` | Fragmento textual indexado |
| `embedding` | `vector(1536) not null` | Vector compatible con `text-embedding-3-small` |
| `metadata` | `jsonb` | Metadatos filtrables |
| `created_at` | `timestamptz` | |

Utiliza un índice **`HNSW` (`using hnsw (embedding vector_cosine_ops)`)** en lugar de `ivfflat`, garantizando precisión de *recall* incluso cuando el índice se crea sobre una tabla inicialmente vacía.

#### `conversations` y `messages`
* **`conversations`**: Agrupa el estado conversacional por `(tenant_id, customer_phone)` único, incluyendo el flag `ai_paused` por conversación individual y `last_customer_message_at`.
* **`messages`**: Registro completo de mensajes (`inbound` / `outbound`), con `tenant_id` denormalizado para filtros directos sin `JOIN` y un índice único parcial `(tenant_id, meta_message_id) WHERE meta_message_id IS NOT NULL` como segunda barrera de deduplicación respaldando a Redis.

#### `wallet_ledger` (Inmutable / Append-Only)
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `uuid PK` | |
| `tenant_id` | `uuid FK` | |
| `entry_type` | `text not null` | `topup`, `consumption`, `refund`, `adjustment` |
| `amount_messages` | `integer not null` | Positivo en recargas/reembolsos, negativo en consumos |
| `amount_clp` | `integer` | Monto asociado a recargas |
| `message_id` | `text` | ID del mensaje consumido (garantiza idempotencia) |
| `created_at` | `timestamptz` | |

Protegida con índice único parcial `uq_wallet_ledger_consumption_message` sobre `(tenant_id, message_id)` para evitar cobros duplicados, más trigger `block_mutation` contra `UPDATE`/`DELETE`.

#### `catalog_items` (Migración 003)
| Columna | Tipo | Notas |
|---|---|---|
| `id` | `uuid PK` | |
| `tenant_id` | `uuid FK` | |
| `item_type` | `text not null` | `product` o `service` |
| `sku` | `text` | Obligatorio para productos (`catalog_items_product_requires_sku`), nullable en servicios |
| `name`, `description` | `text` | Indexado con GIN `to_tsvector('spanish', name)` |
| `price` | `integer not null` | Precio en unidad mínima (`>= 0`) |
| `stock_quantity` | `integer` | Inventario disponible (productos) |
| `duration_minutes` | `integer` | Duración estimada (servicios) |
| `image_url` | `text` | |
| `is_active` | `boolean` | Estado lógico del ítem |

#### `tenant_integrations` (Migración 003)
Almacena credenciales OAuth2 por `(tenant_id, provider)` único (`google_calendar`). Los campos `access_token_enc` y `refresh_token_enc` se guardan cifrados con **Fernet** desde Python antes de enviarse a Supabase.

#### Otras tablas de soporte
* **`appointments`**: Estructura relacional para reservas internas vinculables a `google_calendar_event_id`.
* **`subscriptions` y `payment_events`**: Esquema preparado para gestión de suscripciones y trazabilidad de webhooks de pago.
* **`tenant_files`**: Registro de archivos subidos por tenant para control de cuota.
* **`audit_log`**: Bitácora inmutable (`block_mutation`) para trazabilidad de acceso a datos.

---

## 3. Backend Python — Módulo por Módulo

### 3.1 `app/config.py`
Centraliza la configuración mediante `pydantic-settings` (`Settings`). Ningún otro módulo lee `os.environ` directamente. Exige `supabase_url`, `supabase_publishable_key`, `supabase_secret_key` y `supabase_jwks_url` sin valores por defecto para fallar rápido al arranque si falta configuración crítica.

### 3.2 `app/security.py`
* **`verify_meta_signature` / `verify_webhook_signature`**: Dependencia de FastAPI que lee `await request.body()` en bytes crudos antes de cualquier parseo JSON y valida `X-Hub-Signature-256` mediante `hmac.compare_digest()`.
* **`get_current_tenant`**: Extrae el `Bearer` token, obtiene la clave pública desde el cliente JWKS cacheado (`PyJWKClient`), verifica firma **ES256** y audiencia `"authenticated"`, y extrae `app_metadata.tenant_id`.

### 3.3 `app/crypto.py`
Provee `encrypt_secret()` y `decrypt_secret()` usando `cryptography.fernet.Fernet`. La instancia `Fernet` se crea de forma perezosa (*lazy*) mediante `@lru_cache(maxsize=1)` en `_get_fernet()`, permitiendo importar el módulo en pruebas unitarias sin requerir `APP_ENCRYPTION_KEY` cargada de antemano.

### 3.4 `app/schemas.py`
Modelos Pydantic v2 de entrada (`MetaWebhookPayload`, `MetaEntry`, `MetaChange`, `MetaMessage`, `WebhookAckResponse`) con `extra="ignore"` para tolerar campos nuevos del proveedor externo sin romper la validación estructural (API10:2023).

### 3.5 `app/redis_client.py` y `app/queue_client.py`
* `redis_client`: Conexión asíncrona (`redis.asyncio`) para deduplicación (`SET NX`) y healthcheck.
* `incoming_messages_queue`: Cola `rq.Queue` sobre conexión síncrona de `redis-py`. `enqueue_incoming_message()` encola `process_incoming_message` con `job_timeout=90` (suficiente para cubrir la espera del buffer de n8n más la inferencia del LLM) y `retry=None`.

### 3.6 `app/repository.py`
Implementa el *Repository Pattern* sobre `supabase_admin`:
* **`ConversationsRepository`**, **`WalletRepository`**, **`MetaConnectionsRepository`**, **`CatalogRepository`**: Métodos síncronos (`def`) con filtro obligatorio `.eq("tenant_id", self.tenant_id)`.
* **`TenantIntegrationsRepository`**: Métodos asíncronos (`save_google_calendar_tokens`, `get_valid_access_token`) que gestionan el cifrado Fernet y el refresco automático de tokens de Google cuando faltan menos de 60 segundos para su expiración.
* **`resolve_tenant_id_by_phone_number_id`**: Función de módulo usada exclusivamente por el webhook receiver para descubrir el `tenant_id` propietario de un `phone_number_id`.

### 3.7 `app/n8n_client.py` y `app/template_engine.py`
* **`template_engine.py`**: Carga `manifest.json` y `workflow.json`, valida variables requeridas, rechaza claves no declaradas en el manifest (evitando sobrescritura de variables de sistema), escapa valores para inserción segura en JSON (`_escape_for_json_string`), verifica que no queden placeholders `{{...}}` sin resolver y bloquea tipos de nodo peligrosos (`executeCommand`, `readWriteFile`, `ssh`).
* **`n8n_client.py`**: Crea y activa workflows vía REST (`/api/v1/workflows`), ejecutando la secuencia `create → activate → deactivate → activate` para mitigar el bug `n8n-io/n8n#21614` de registro de webhooks.

### 3.8 `app/google_oauth.py`
Construye URLs de autorización (`access_type=offline`, `prompt=consent`), firma el parámetro `state` con HMAC-SHA256 + timestamp (`STATE_TTL_SECONDS = 600`) para prevenir ataques CSRF/falsificación de tenant, e intercambia o refresca tokens contra `oauth2.googleapis.com`.

### 3.9 `app/worker.py`
Procesa cada mensaje entrante en orden estricto:
1. Persiste conversación y mensaje en Supabase (`_get_or_create_conversation`, `_save_message`, `_update_last_customer_message_at`).
2. Filtra mensajes no textuales (`if msg_type != "text" or not text_body:`), retornando `skipped/unsupported_message_type` sin consumir saldo ni invocar n8n.
3. Verifica pausa individual o global (`_is_ai_paused`).
4. Verifica la ventana de servicio (`settings.service_window_hours`, default `24h`).
5. Verifica saldo positivo en `wallet_ledger` (`_get_wallet_balance`).
6. Recupera el token vigente de Google Calendar (con degradación elegante si falla).
7. Envía el payload vía `httpx.Client(timeout=60.0)` al webhook determinístico `wh-{tenant_id}` en n8n.
8. Registra el consumo idempotente (`_record_wallet_consumption`) únicamente tras un disparo exitoso.

### 3.10 `app/main.py` y `app/limiter.py`
Instancia única de `Limiter` (`slowapi`), middleware de cabeceras de seguridad (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`), CORS restringido fuera de `AVEN_ENV=dev`, montaje condicional de `dev_tools` solo en desarrollo y servicio de archivos estáticos (`static/`).

---

## 4. API REST — Endpoints en Detalle

| Grupo | Método y Ruta | Auth | Rate Limit | Función principal |
|---|---|---|---|---|
| **Health** | `GET /api/health` | No | — | Liveness check del proceso |
| **Health** | `GET /api/health/ready` | No | — | Readiness check con `PING` real a Redis (`503` si falla) |
| **Webhooks** | `GET /api/webhooks/meta` | No | — | Verificación de suscripción de webhook (`hub.challenge`) |
| **Webhooks** | `POST /api/webhooks/meta` | Firma HMAC | `60/s` | Validación HMAC + Pydantic, dedup Redis `SET NX`, encolado RQ |
| **Conversations** | `GET /api/conversations` | JWT | — | Lista conversaciones del tenant autenticado |
| **Conversations** | `GET /api/conversations/{id}` | JWT | — | Detalle de conversación (`404` si no existe o es de otro tenant) |
| **Wallet** | `GET /api/wallet/balance` | JWT | — | Saldo calculado desde `wallet_ledger` |
| **Templates** | `GET /api/templates` | No | — | Catálogo público de plantillas con `manifest.json` |
| **Templates** | `GET /api/templates/{id}` | No | — | Esquema de variables configurables de una plantilla |
| **Templates** | `POST /api/templates/{id}/deploy` | JWT | `5/min` | Renderiza, sanitiza, fuerza path `wh-{tenant_id}` y despliega en n8n |
| **Integrations** | `GET /api/integrations/google-calendar/connect` | JWT | — | Genera URL OAuth2 con `state` firmado |
| **Integrations** | `GET /api/integrations/google-calendar/callback` | `state` HMAC | `20/min` | Intercambia `code` por tokens y los guarda cifrados con Fernet |
| **Integrations** | `GET /api/integrations/google-calendar/status` | JWT | — | Verifica vigencia del token de Google Calendar |
| **Catalog** | `GET / POST /api/catalog` | JWT | — | Listado filtrado y creación de productos/servicios |
| **Catalog** | `GET / PATCH / DELETE /api/catalog/{id}` | JWT | — | Consulta, actualización parcial y eliminación por tenant |
| **Dev Tools** | `POST /api/dev/simulate-webhook` | Solo `dev` | `20/min` | Envía un webhook firmado con HMAC real contra la API local |
| **Dev Tools** | `POST /api/dev/generate-test-jwt` | Solo `dev` | `10/min` | Provisiona tenant + conexión + usuario en Supabase y emite JWT real |

---

## 5. Sistema de Plantillas y Flujos n8n

### 5.1 `00-shared-message-buffer` (Sub-flujo compartido multi-tenant)
Consolida ráfagas de mensajes consecutivos de un mismo usuario antes de invocar al agente de IA:
* Utiliza claves aisladas por tenant: `buffer_in:{tenant_id}:{customer_phone}`, `last_seen:{context_id}`, `buffer_count:{context_id}` y `waiting_reply:{context_id}`.
* Emplea `INCR` atómico sobre `waiting_reply:{context_id}` para garantizar que solo una ejecución espere y consolide la ráfaga, evitando condiciones de carrera y respuestas duplicadas.
* Lee la lista con operación `get` y `keyType: "list"`, ordena cronológicamente por timestamp, deduplica y retorna `status: "consolidated"`.

### 5.2 Plantillas base (`01` a `05`)
Plantillas estructurales (`Webhook → Set → Respond to Webhook`) para validar el ciclo de sustitución de variables y despliegue multi-tenant (`01-atencion-catalogo`, `02-agendamiento-citas`, `03-ventas-catalogo`, `04-faq-simple`, `05-toma-pedidos`).

### 5.3 `06-motor-taller` (Flujo completo con Agente IA y Herramientas)
Integra `AI Agent` (`gpt-4o-mini`), memoria conversacional en Redis aislada por `context_id` y cuatro herramientas:
1. **Consultar Inventario (`postgresTool`)**: Ejecuta `SELECT ... FROM catalog_items WHERE tenant_id = '{{tenant_id}}' AND item_type = 'product' AND is_active = true AND (name ILIKE '%' || $1::text || '%' OR sku ILIKE $1::text) LIMIT 5`, pasando el término de búsqueda de la IA como parámetro posicional (`queryReplacement`) para prevenir inyección SQL y manteniendo el `tenant_id` fijado por el motor de plantillas.
2. **Buscar en Manuales Técnicos (`vectorStoreSupabase`)**: Recupera contexto técnico desde `knowledge_vectors`. *(Nota técnica: la función SQL `match_documents` exige `p_tenant_id` explícito; para uso en producción con el nodo estándar de n8n se recomienda exponer un wrapper RPC por tenant o usar `httpRequestTool` contra `/rest/v1/rpc/match_documents`).*
3. **Consultar Disponibilidad Taller (`httpRequestTool`)**: Consulta eventos en Google Calendar API v3 usando el `Bearer` token inyectado dinámicamente por el worker.
4. **Agendar Instalación (`httpRequestTool`)**: Crea el evento confirmado en Google Calendar API v3.

---

## 6. Flujos End-to-End

1. **Ingesta y Procesamiento de Eventos:**  
   `Proveedor Externo → POST /api/webhooks/meta → verify_webhook_signature → resolve_tenant_id_by_phone_number_id → Redis SET NX → RQ enqueue → process_incoming_message → POST /webhook/wh-{tenant_id} → n8n Buffer + AI Agent → Respuesta y descuento en wallet_ledger`
2. **Aprovisionamiento de Workflows:**  
   `POST /api/templates/{id}/deploy → MetaConnectionsRepository.get_connection → render_workflow (validación + sanitización) → inyección de path wh-{tenant_id} → N8nClient.deploy_workflow → persistencia de n8n_workflow_id`
3. **Vinculación OAuth2 (Google Calendar):**  
   `GET /connect → build_authorization_url (state HMAC) → Consentimiento Google → GET /callback → verify_and_extract_tenant_from_state → exchange_code_for_tokens → cifrado Fernet en tenant_integrations`

---

## 7. Modelo de Seguridad y Mitigaciones OWASP API Top 10

* **API1:2023 (BOLA):** Triple barrera: `tenant_id` derivado de JWT ES256 + filtro obligatorio en `BaseRepository` + políticas RLS en PostgreSQL.
* **API3:2023 (Broken Object Property Level Authorization):** Esquemas Pydantic de entrada estrictos (`CatalogItemInput`, `CatalogItemUpdate`) separados del modelo de persistencia; `tenant_id` siempre inyectado desde el contexto autenticado.
* **API4:2023 (Unrestricted Resource Consumption):** Rate limiting por IP con `slowapi` (`60/s` en webhooks, `5/min` en despliegues de workflows, `20/min` en callbacks OAuth) y control de saldo por tenant en `wallet_ledger`.
* **API7:2023 (SSRF):** `N8N_SSRF_PROTECTION_ENABLED=true` y construcción interna de URLs destino sin parámetros controlados por el usuario.
* **API10:2023 (Unsafe Consumption of APIs):** Validación estructural estricta de payloads entrantes, consultas SQL parametrizadas en herramientas de IA y sanitización de nodos en plantillas JSON.
* **Aislamiento de Ejecución y Red:**
  * Redis nunca expone puertos al host (mitigando deserialización `pickle` de RQ).
  * Los nodos de código de n8n se ejecutan en contenedores `n8n-runner` separados sin acceso a `N8N_ENCRYPTION_KEY` ni credenciales internas.
  * Cabeceras HTTP de seguridad aplicadas globalmente por middleware.

---

## 8. Infraestructura y Despliegue

* **Servicios (`docker-compose.yml`):** `redis`, `n8n`, `n8n-runner`, `app` (FastAPI) y `rq-worker`.
* **Producción (`docker-compose.prod.yml`):** Añade `caddy` como proxy inverso con terminación TLS automática y deshabilita herramientas de desarrollo (`AVEN_ENV=prod`).
* **Automatización de arranque (`setup.sh` / `setup.ps1`):** Verifica dependencias de Docker, genera llaves criptográficas locales (`N8N_ENCRYPTION_KEY`, `N8N_RUNNERS_AUTH_TOKEN`, `APP_ENCRYPTION_KEY`, `GOOGLE_OAUTH_STATE_SECRET`) sin hardcodear secretos, valida conectividad con Supabase y ejecuta un smoke test de emisión de JWT.

---

## 9. Estrategia de Verificación y Pruebas

* **Integración con PostgreSQL + `pgvector`:** Validación encadenada de las tres migraciones SQL, triggers de inmutabilidad (`block_mutation`), restricciones de unicidad para idempotencia de consumo y aislamiento de consultas entre tenants distintos.
* **Criptografía y Autenticación:** Pruebas de verificación JWT ES256 contra JWKS local, rechazo de tokens expirados o manipulados, validación de firmas HMAC en webhooks y rechazo de estados OAuth adulterados.
* **Concurrencia y Colas:** Deduplicación atómica en Redis, consumo de jobs en proceso `rq worker` independiente y validación de respuestas HTTP `429` ante exceso de tasa.
* **Motor de Plantillas:** Pruebas unitarias de escapado JSON, detección de variables no declaradas y bloqueo de nodos prohibidos.

---

## 10. Alcance Actual y Trabajo Futuro

### Implementado en esta versión
* Esquema relacional y vectorial completo con RLS e inmutabilidad de *ledger* y auditoría.
* Autenticación JWT ES256 (JWKS), *Repository Pattern* y aislamiento multi-tenant.
* Pipeline asíncrono completo (`webhooks.py` → Redis → RQ → `worker.py` → n8n).
* Motor de plantillas con sanitización, buffer compartido en Redis y flujo completo `06-motor-taller`.
* Gestión OAuth2 con cifrado Fernet para Google Calendar y CRUD de catálogo unificado.

### Trabajo futuro (extensión arquitectónica)
* **Adaptadores multicanal adicionales:** Extender el módulo de ingesta para soportar Telegram, Slack o WebChat reutilizando la misma cola `incoming_messages` y el contrato de `worker.py`.
* **Pipeline de ingesta documental (RAG):** Endpoint asíncrono para fragmentación de PDFs, generación de embeddings y escritura en `knowledge_vectors` con wrapper RPC dedicado para herramientas de n8n.
* **Observabilidad y trazabilidad:** Conectar `audit_log` a las operaciones de lectura/escritura sensibles y exportar métricas OpenTelemetry / Prometheus desde FastAPI y RQ.