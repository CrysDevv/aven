-- Funcion RPC requerida por el nodo Supabase Vector Store de n8n (convencion
-- estandar de LangChain: nombre "match_documents"). No existia en el
-- esquema original -- se agrega aqui porque el sistema de RAG la necesita
-- para que las plantillas con busqueda semantica funcionen.
--
-- AISLAMIENTO MULTI-TENANT: p_tenant_id es OBLIGATORIO (sin default) y se
-- filtra contra la columna tenant_id, no solo contra el JSONB `metadata`.
-- Asi, omitir `filter` (o dejarlo en '{}') nunca expone filas de otros tenants.
-- La ingesta de documentos debe escribir tambien {"tenant_id": "..."} en
-- metadata si se quiere seguir filtrando por ese campo via `filter`.

-- Elimina la firma anterior (sin p_tenant_id): si no, CREATE OR REPLACE solo
-- agregaria una sobrecarga y la version insegura seguiria siendo invocable.
drop function if exists match_documents(vector, int, jsonb);

create or replace function match_documents (
  query_embedding vector(1536),
  p_tenant_id uuid,
  match_count int default 5,
  filter jsonb default '{}'
) returns table (
  id uuid,
  content text,
  metadata jsonb,
  similarity float
)
language plpgsql
stable
as $$
begin
  return query
  select
    knowledge_vectors.id,
    knowledge_vectors.content,
    knowledge_vectors.metadata,
    1 - (knowledge_vectors.embedding <=> query_embedding) as similarity
  from knowledge_vectors
  where knowledge_vectors.tenant_id = p_tenant_id
    and knowledge_vectors.metadata @> filter
  order by knowledge_vectors.embedding <=> query_embedding
  limit match_count;
end;
$$;

-- ============================================================================
-- Tabla parts_inventory -- requerida por la plantilla "Motor & Taller"
-- (y reutilizable por cualquier plantilla que venda productos con SKU/stock).
-- Sigue las mismas convenciones del esquema original: tenant_id obligatorio,
-- RLS activo, indices por tenant.
-- ============================================================================

create table parts_inventory (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  sku          text not null,
  name         text not null,
  description  text,
  price        integer not null,          -- en la unidad minima de la moneda (ej. CLP sin decimales)
  stock        integer not null default 0,
  image_url    text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  unique (tenant_id, sku)
);

create index idx_parts_inventory_tenant_id on parts_inventory(tenant_id);
create index idx_parts_inventory_name_search on parts_inventory using gin (to_tsvector('spanish', name));

create trigger trg_parts_inventory_updated_at
  before update on parts_inventory
  for each row execute function set_updated_at();

alter table parts_inventory enable row level security;

create policy tenant_isolation_all on parts_inventory
  for all using (tenant_id = auth_tenant_id());
