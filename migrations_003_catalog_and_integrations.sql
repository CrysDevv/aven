-- ============================================================================
-- Migracion 003: catalog_items, tenant_files, tenant_integrations
--
-- catalog_items REEMPLAZA a parts_inventory (migracion 002) -- se generaliza
-- a productos Y servicios en una sola tabla, porque la mayoria de los
-- verticales de Aven (Agenda Saludable, Belleza Integral, Asesor
-- Inmobiliario) venden SERVICIOS con precio, no productos con SKU/stock.
-- Si ya aplicaste la migracion 002 en tu Supabase, corre esto despues --
-- el DROP de mas abajo es seguro porque parts_inventory nunca se conecto
-- a ningun endpoint real todavia.
-- ============================================================================

drop table if exists parts_inventory;

-- ----------------------------------------------------------------------
-- catalog_items: productos Y servicios en una sola tabla polimorfica
-- ----------------------------------------------------------------------
create table catalog_items (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  item_type         text not null check (item_type in ('product', 'service')),
  sku               text,
  name              text not null,
  description       text,
  price             integer not null,        -- unidad minima de la moneda (CLP sin decimales)
  stock_quantity    integer,                  -- NULL para servicios (no aplica)
  duration_minutes  integer,                  -- NULL para productos; usado por servicios con hora fija (Agenda Saludable, Belleza Integral)
  image_url         text,
  is_active         boolean not null default true,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  -- Un producto SIEMPRE debe tener SKU (para que la busqueda por codigo
  -- funcione); un servicio no lo necesita.
  constraint catalog_items_product_requires_sku
    check (item_type <> 'product' or sku is not null)
);

-- SKU unico por tenant, pero SOLO cuando existe (los servicios pueden
-- repetir NULL sin chocar entre si)
create unique index uq_catalog_items_tenant_sku on catalog_items(tenant_id, sku) where sku is not null;
create index idx_catalog_items_tenant_id on catalog_items(tenant_id);
create index idx_catalog_items_tenant_type on catalog_items(tenant_id, item_type);
create index idx_catalog_items_name_search on catalog_items using gin (to_tsvector('spanish', name));

create trigger trg_catalog_items_updated_at
  before update on catalog_items
  for each row execute function set_updated_at();

alter table catalog_items enable row level security;
create policy tenant_isolation_all on catalog_items
  for all using (tenant_id = auth_tenant_id());

-- ----------------------------------------------------------------------
-- tenant_files: registro de archivos subidos (PDFs, fotos) para poder
-- medir el uso real de storage contra tenants.storage_limit_mb
-- ----------------------------------------------------------------------
create table tenant_files (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  file_name        text not null,
  file_url         text not null,
  file_size_bytes  bigint not null,
  file_type        text not null check (file_type in ('image', 'pdf', 'document')),
  created_at       timestamptz not null default now()
);

create index idx_tenant_files_tenant_id on tenant_files(tenant_id);

alter table tenant_files enable row level security;
create policy tenant_isolation_all on tenant_files
  for all using (tenant_id = auth_tenant_id());

-- Funcion auxiliar: uso real de storage de un tenant en MB, para comparar
-- contra tenants.storage_limit_mb antes de aceptar una subida nueva.
create or replace function get_tenant_storage_usage_mb(p_tenant_id uuid)
returns numeric as $$
  select coalesce(sum(file_size_bytes), 0)::numeric / (1024 * 1024)
  from tenant_files
  where tenant_id = p_tenant_id;
$$ language sql stable;

-- ----------------------------------------------------------------------
-- tenant_integrations: credenciales OAuth2 de servicios externos
-- (Google Calendar por ahora, extensible a futuro). access_token y
-- refresh_token llegan YA CIFRADOS desde la aplicacion (Fernet, en
-- Python) -- Supabase nunca ve el valor en texto plano, ni siquiera de
-- paso. A diferencia de meta_connections.access_token_enc (que asumia
-- cifrado via pgp_sym_encrypt de Postgres), aqui el cifrado ocurre ANTES
-- de que el dato salga del backend, porque el acceso real a Supabase es
-- via PostgREST (supabase-py), no SQL directo -- pgp_sym_encrypt no es
-- invocable limpiamente desde ahi.
-- ----------------------------------------------------------------------
create table tenant_integrations (
  id                       uuid primary key default gen_random_uuid(),
  tenant_id                uuid not null references tenants(id) on delete cascade,
  provider                 text not null check (provider in ('google_calendar')),
  access_token_enc         text,             -- Fernet, base64 -- cifrado en Python antes de llegar aqui
  refresh_token_enc        text,             -- idem -- MAS sensible, vive mucho mas tiempo que el access_token
  token_expires_at         timestamptz,
  scope                    text,
  external_account_email   text,             -- para mostrarle al dueno "conectado como fulano@gmail.com"
  status                   text not null default 'pending'
                             check (status in ('pending', 'active', 'revoked', 'error')),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  unique (tenant_id, provider)
);

create index idx_tenant_integrations_tenant_id on tenant_integrations(tenant_id);

create trigger trg_tenant_integrations_updated_at
  before update on tenant_integrations
  for each row execute function set_updated_at();

alter table tenant_integrations enable row level security;

-- Solo SELECT para 'authenticated' (para que el dueno vea el estado de su
-- propia integracion, ej. "Conectado como fulano@gmail.com") -- el
-- INSERT/UPDATE de tokens SOLO lo hace el backend con service_role,
-- nunca directo desde el cliente.
create policy tenant_isolation_select on tenant_integrations
  for select using (tenant_id = auth_tenant_id());
