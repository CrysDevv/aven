-- ============================================================================
-- AVEN — Script de Base de Datos (Supabase / PostgreSQL)
-- v1.0
--
-- Ejecutar en el SQL Editor de Supabase, o vía CLI: supabase db push
-- Orden de ejecución: extensiones → funciones auxiliares → tablas → índices
--                      → RLS → triggers → datos semilla (opcional)
-- ============================================================================


-- ============================================================================
-- 1. EXTENSIONES
-- ============================================================================

create extension if not exists "pgcrypto";   -- cifrado de columnas + gen_random_uuid()
create extension if not exists "vector";     -- pgvector, para embeddings de RAG


-- ============================================================================
-- 2. FUNCIONES AUXILIARES
-- ============================================================================

-- Trigger genérico para mantener updated_at al día en cualquier tabla
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;


-- ============================================================================
-- 3. TABLA: tenants
-- Cada fila = un negocio cliente de Aven.
-- ============================================================================

create table tenants (
  id                  uuid primary key default gen_random_uuid(),
  business_name       text not null,
  plan                text not null default 'starter'
                        check (plan in ('starter', 'growth', 'pro', 'agencia', 'enterprise')),
  ai_globally_paused  boolean not null default false,
  storage_limit_mb    integer not null default 200,   -- se ajusta según plan al momento de crear/upgradear
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create trigger trg_tenants_updated_at
  before update on tenants
  for each row execute function set_updated_at();

comment on table tenants is 'Negocios clientes de Aven. Un tenant = una empresa registrada.';


-- ============================================================================
-- 4. TABLA: tenant_members
-- Vincula usuarios de Supabase Auth (auth.users) a un tenant.
-- Es la fuente de verdad que se usa para inyectar tenant_id en el JWT
-- (vía custom access token hook) o para resolverlo en cada request desde FastAPI.
-- ============================================================================

create table tenant_members (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  role        text not null default 'owner' check (role in ('owner', 'staff')),
  created_at  timestamptz not null default now(),
  unique (tenant_id, user_id)
);

create index idx_tenant_members_user_id on tenant_members(user_id);


-- ============================================================================
-- 5. TABLA: meta_connections
-- Datos de la conexión de WhatsApp Business de cada tenant.
-- access_token va cifrado con pgcrypto — nunca en texto plano.
-- ============================================================================

create table meta_connections (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  waba_id           text not null,
  phone_number_id   text not null unique,
  n8n_workflow_id   text,
  access_token_enc  bytea,                  -- pgp_sym_encrypt(token, llave) — la llave vive en Secret Manager, no en la DB
  status            text not null default 'pending'
                      check (status in ('pending', 'active', 'disconnected')),
  connected_at      timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index idx_meta_connections_tenant_id on meta_connections(tenant_id);

create trigger trg_meta_connections_updated_at
  before update on meta_connections
  for each row execute function set_updated_at();

comment on column meta_connections.access_token_enc is
  'Cifrado con pgp_sym_encrypt(token, current_setting(''app.encryption_key'')). La llave NUNCA vive en esta base de datos.';


-- ============================================================================
-- 6. TABLA: knowledge_vectors
-- RAG: fragmentos de PDFs/catálogos + su embedding, por tenant.
-- ============================================================================

create table knowledge_vectors (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  content     text not null,
  embedding   vector(1536) not null,        -- dimensión de text-embedding-3-small de OpenAI
  metadata    jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

create index idx_knowledge_vectors_tenant_id on knowledge_vectors(tenant_id);

-- Índice HNSW para búsqueda de similitud coseno. A diferencia de ivfflat, no
-- necesita datos previos para entrenar centroides, así que es seguro crearlo
-- sobre una tabla vacía sin degradar la precisión de recall.
create index idx_knowledge_vectors_embedding on knowledge_vectors
  using hnsw (embedding vector_cosine_ops);

comment on table knowledge_vectors is
  'Toda consulta de similitud DEBE incluir WHERE tenant_id = X para evitar fuga de información entre clientes.';


-- ============================================================================
-- 7. TABLA: conversations
-- Estado de cada conversación (un cliente final por tenant).
-- Aquí vive el flag de "pausar IA" por chat individual.
-- ============================================================================

create table conversations (
  id                        uuid primary key default gen_random_uuid(),
  tenant_id                 uuid not null references tenants(id) on delete cascade,
  customer_phone            text not null,
  ai_paused                 boolean not null default false,
  last_customer_message_at  timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now(),
  unique (tenant_id, customer_phone)
);

create index idx_conversations_tenant_id on conversations(tenant_id);

create trigger trg_conversations_updated_at
  before update on conversations
  for each row execute function set_updated_at();


-- ============================================================================
-- 8. TABLA: messages
-- Log completo de mensajes (el "inbox" que ve el dueño del negocio).
-- SIEMPRE se escribe aquí, incluso fuera de la ventana de 24h o con IA pausada.
-- ============================================================================

create table messages (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,  -- denormalizado a propósito: permite filtrar/RLS sin JOIN
  conversation_id   uuid not null references conversations(id) on delete cascade,
  meta_message_id   text,                   -- wamid de Meta, para deduplicación aguas abajo
  direction         text not null check (direction in ('inbound', 'outbound')),
  content           text,
  media_url         text,
  media_type        text check (media_type in ('image', 'audio', 'video', 'document') or media_type is null),
  created_at        timestamptz not null default now()
);

create index idx_messages_tenant_id on messages(tenant_id);
create index idx_messages_conversation_id on messages(conversation_id);

-- Evita insertar dos veces el mismo mensaje entrante de Meta (defensa adicional a la de Redis)
create unique index uq_messages_meta_message_id on messages(tenant_id, meta_message_id)
  where meta_message_id is not null;


-- ============================================================================
-- 9. TABLA: appointments
-- Citas/reservas. Fuente de verdad propia; Google Calendar es un espejo opcional.
-- ============================================================================

create table appointments (
  id                       uuid primary key default gen_random_uuid(),
  tenant_id                uuid not null references tenants(id) on delete cascade,
  customer_phone           text not null,
  scheduled_at             timestamptz not null,
  status                   text not null default 'pending'
                             check (status in ('pending', 'confirmed', 'cancelled', 'completed')),
  notes                    text,
  google_calendar_event_id text,            -- null si el tenant no activó la sincronización
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create index idx_appointments_tenant_id on appointments(tenant_id);
create index idx_appointments_scheduled_at on appointments(tenant_id, scheduled_at);

create trigger trg_appointments_updated_at
  before update on appointments
  for each row execute function set_updated_at();


-- ============================================================================
-- 10. TABLA: subscriptions
-- Suscripción de plataforma (cobro recurrente).
-- ============================================================================

create table subscriptions (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null unique references tenants(id) on delete cascade,
  plan                text not null,
  status              text not null default 'active'
                        check (status in ('active', 'past_due', 'cancelled')),
  payment_provider    text check (payment_provider in ('webpay', 'flow')),
  payment_token_ref   text,                 -- referencia/id del token guardado en la pasarela, NO el número de tarjeta
  next_billing_date   date,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create trigger trg_subscriptions_updated_at
  before update on subscriptions
  for each row execute function set_updated_at();


-- ============================================================================
-- 11. TABLA: wallet_ledger
-- Libro contable append-only del saldo de mensajes. NUNCA usar una columna
-- de saldo mutable — el saldo actual es siempre la suma de este historial.
-- ============================================================================

create table wallet_ledger (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  entry_type        text not null check (entry_type in ('topup', 'consumption', 'refund', 'adjustment')),
  amount_messages   integer not null,       -- positivo en topup/refund, negativo en consumption
  amount_clp        integer,                -- monto pagado en CLP, solo aplica a 'topup'
  message_id        text,                   -- wamid asociado al descuento, para idempotencia
  created_at        timestamptz not null default now()
);

create index idx_wallet_ledger_tenant_id on wallet_ledger(tenant_id);

-- Evita descontar dos veces saldo por el mismo mensaje (defensa adicional al lock de Redis)
create unique index uq_wallet_ledger_consumption_message on wallet_ledger(tenant_id, message_id)
  where entry_type = 'consumption' and message_id is not null;

comment on table wallet_ledger is
  'Append-only. El saldo actual de un tenant = SUM(amount_messages) WHERE tenant_id = X. Ver función get_wallet_balance().';


-- ============================================================================
-- 12. TABLA: payment_events
-- Log de webhooks recibidos desde la pasarela de pago, para reconciliación.
-- ============================================================================

create table payment_events (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid references tenants(id) on delete set null,
  provider    text not null check (provider in ('webpay', 'flow')),
  event_type  text not null,
  payload     jsonb not null,
  created_at  timestamptz not null default now()
);

create index idx_payment_events_tenant_id on payment_events(tenant_id);


-- ============================================================================
-- 13. TABLA: audit_log
-- Trazabilidad de acceso a datos — requisito técnico y de Ley 21.719.
-- Append-only: se bloquean UPDATE/DELETE mediante triggers más abajo.
-- ============================================================================

create table audit_log (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid,
  user_id     uuid,
  resource    text not null,
  action      text not null,
  metadata    jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

create index idx_audit_log_tenant_id on audit_log(tenant_id);
create index idx_audit_log_created_at on audit_log(created_at);


-- ============================================================================
-- 14. FUNCIÓN: get_wallet_balance
-- Saldo actual de mensajes de un tenant, calculado desde el ledger.
-- ============================================================================

create or replace function get_wallet_balance(p_tenant_id uuid)
returns integer as $$
  select coalesce(sum(amount_messages), 0)::integer
  from wallet_ledger
  where tenant_id = p_tenant_id;
$$ language sql stable;


-- ============================================================================
-- 15. ROW LEVEL SECURITY
--
-- El backend (FastAPI) usa la service_role key, que bypasea RLS por diseño
-- — el aislamiento real para el backend lo da el Repository Pattern en Python.
-- RLS se activa igual aquí como defensa en profundidad: protege ante
-- exposición accidental vía PostgREST/Supabase client directo, y habilita
-- casos futuros como Supabase Realtime consumido directo desde la app móvil.
--
-- Asume un custom claim `tenant_id` en app_metadata del JWT, seteado vía
-- Custom Access Token Hook de Supabase al momento de emitir el token,
-- resuelto a partir de tenant_members.
-- ============================================================================

alter table tenants               enable row level security;
alter table tenant_members        enable row level security;
alter table meta_connections      enable row level security;
alter table knowledge_vectors     enable row level security;
alter table conversations         enable row level security;
alter table messages              enable row level security;
alter table appointments          enable row level security;
alter table subscriptions         enable row level security;
alter table wallet_ledger         enable row level security;
alter table payment_events        enable row level security;
alter table audit_log             enable row level security;

-- Helper: extrae el tenant_id del JWT actual
create or replace function auth_tenant_id()
returns uuid as $$
  select (auth.jwt() -> 'app_metadata' ->> 'tenant_id')::uuid;
$$ language sql stable;

-- Política estándar reutilizada en cada tabla con tenant_id directo
create policy tenant_isolation_select on tenants
  for select using (id = auth_tenant_id());

create policy tenant_isolation_all on tenant_members
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on meta_connections
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on knowledge_vectors
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on conversations
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on messages
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on appointments
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_all on subscriptions
  for all using (tenant_id = auth_tenant_id());

create policy tenant_isolation_select on wallet_ledger
  for select using (tenant_id = auth_tenant_id());
  -- Sin política de insert/update/delete para 'authenticated': el ledger solo lo escribe el backend con service_role.

create policy tenant_isolation_select on payment_events
  for select using (tenant_id = auth_tenant_id());

create policy tenant_isolation_select on audit_log
  for select using (tenant_id = auth_tenant_id());
  -- audit_log tampoco es escribible por 'authenticated' — solo el backend.


-- ============================================================================
-- 16. INMUTABILIDAD DE audit_log Y wallet_ledger
-- Ni siquiera el rol de aplicación (fuera de superuser/service_role a nivel
-- de Postgres) debería poder alterar el historial. Esto se refuerza también
-- a nivel de infraestructura (permisos de rol), esto es la capa de base de datos.
-- ============================================================================

create or replace function block_mutation()
returns trigger as $$
begin
  raise exception 'Esta tabla es append-only: % no está permitido', TG_OP;
end;
$$ language plpgsql;

create trigger trg_audit_log_no_update
  before update or delete on audit_log
  for each row execute function block_mutation();

create trigger trg_wallet_ledger_no_update
  before update or delete on wallet_ledger
  for each row execute function block_mutation();


-- ============================================================================
-- FIN DEL SCRIPT
--
-- Siguientes pasos (fuera de este script):
-- 1. Configurar el Custom Access Token Hook en Supabase Auth para inyectar
--    app_metadata.tenant_id a partir de tenant_members al emitir cada JWT.
-- 2. Guardar la llave de cifrado de pgcrypto en el Secret Manager y setearla
--    por sesión con: select set_config('app.encryption_key', '<llave>', false);
-- 3. Si el volumen de knowledge_vectors crece mucho, ajustar los parámetros
--    del índice HNSW (m, ef_construction) y hnsw.ef_search en las consultas.
-- ============================================================================
