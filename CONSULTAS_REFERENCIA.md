# Consultas de Referencia — Tablas Nuevas (catalog_items, tenant_files, tenant_integrations)

Todas las consultas de este documento fueron **ejecutadas de verdad** contra una base Postgres 16 + pgvector local, con datos de prueba insertados, no solo escritas. Los resultados de ejemplo están tomados de esa ejecución real.

---

## catalog_items (productos y servicios)

### 1. Listar productos activos (pantalla de inventario en la app)
```sql
SELECT sku, name, price, stock_quantity, image_url
FROM catalog_items
WHERE tenant_id = '<tenant_id>'
  AND item_type = 'product' AND is_active = true
ORDER BY name;
```

### 2. Listar servicios activos (con duración, para agenda)
```sql
SELECT name, price, duration_minutes
FROM catalog_items
WHERE tenant_id = '<tenant_id>'
  AND item_type = 'service' AND is_active = true
ORDER BY name;
```

### 3. Buscar en el catálogo por texto (usa el índice GIN ya creado en la migración)
```sql
SELECT item_type, name, price
FROM catalog_items
WHERE tenant_id = '<tenant_id>'
  AND to_tsvector('spanish', name) @@ plainto_tsquery('spanish', 'freno');
```

### 4. Productos con stock bajo (para alertar al dueño)
```sql
SELECT sku, name, stock_quantity
FROM catalog_items
WHERE tenant_id = '<tenant_id>'
  AND item_type = 'product' AND is_active = true AND stock_quantity <= 3
ORDER BY stock_quantity;
```

### 5. Descontar stock tras una venta confirmada
```sql
UPDATE catalog_items SET stock_quantity = stock_quantity - 1
WHERE tenant_id = '<tenant_id>'
  AND sku = '<sku>' AND stock_quantity > 0
RETURNING sku, stock_quantity;
```
> El `AND stock_quantity > 0` evita que quede en negativo si dos ventas simultáneas descuentan el último ítem — si la fila no se actualiza (0 filas afectadas), significa que ya no había stock.

### 6. Desactivar un ítem sin borrarlo (soft delete — preferible a DELETE)
```sql
UPDATE catalog_items SET is_active = false
WHERE tenant_id = '<tenant_id>' AND id = '<item_id>';
```

---

## tenant_files (uso de storage)

### 7. Uso real de storage vs el límite del plan
```sql
SELECT
  t.business_name,
  t.storage_limit_mb,
  round(get_tenant_storage_usage_mb(t.id), 2) AS usado_mb,
  round(100.0 * get_tenant_storage_usage_mb(t.id) / t.storage_limit_mb, 1) AS porcentaje_usado
FROM tenants t
WHERE t.id = '<tenant_id>';
```

### 8. Tenants que ya superaron el 90% de su storage (para alertar / cron de monitoreo)
```sql
SELECT t.business_name, t.storage_limit_mb, round(get_tenant_storage_usage_mb(t.id), 2) AS usado_mb
FROM tenants t
WHERE get_tenant_storage_usage_mb(t.id) > (t.storage_limit_mb * 0.9);
```

### 9. Registrar un archivo recién subido (llamar justo después de subir a Supabase Storage)
```sql
INSERT INTO tenant_files (tenant_id, file_name, file_url, file_size_bytes, file_type)
VALUES ('<tenant_id>', '<nombre>', '<url_publica_o_firmada>', <tamano_bytes>, 'pdf');
```

---

## tenant_integrations (Google Calendar)

### 10. ¿Este tenant tiene Google Calendar conectado y activo?
```sql
SELECT provider, status, external_account_email, token_expires_at
FROM tenant_integrations
WHERE tenant_id = '<tenant_id>' AND provider = 'google_calendar' AND status = 'active';
```
> En la práctica, **no consultes esto directo desde el backend** — usa siempre `TenantIntegrationsRepository.get_valid_access_token()`, que ya maneja el refresco automático del token vencido. Esta consulta es para inspección manual/debug.

### 11. Integraciones con token vencido (candidatas a monitoreo — no deberían acumularse si el refresco automático funciona)
```sql
SELECT t.business_name, ti.provider, ti.token_expires_at
FROM tenant_integrations ti
JOIN tenants t ON t.id = ti.tenant_id
WHERE ti.status = 'active' AND ti.token_expires_at < now();
```

---

## Notas importantes

- **Nunca ejecutes estas queries reemplazando `tenant_id` manualmente en código de aplicación** — en Python siempre pasan por el Repository Pattern (`CatalogRepository`, `TenantIntegrationsRepository`), que ya fuerza el filtro. Estas son para consulta directa en el SQL Editor de Supabase durante debug, o para construir reportes/cron jobs internos.
- Las tablas `access_token_enc`/`refresh_token_enc` de `tenant_integrations` están cifradas con Fernet **a nivel de aplicación** — si las miras directo en el SQL Editor de Supabase, vas a ver el texto cifrado, no el token real. Eso es intencional.
