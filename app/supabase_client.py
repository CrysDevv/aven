"""
Cliente de Supabase para el backend. Usa la SECRET key (equivalente a la
antigua service_role) — esto bypasea RLS intencionalmente, ya que el
aislamiento real para el backend lo da el Repository Pattern en Python
(ver app/repository.py), no RLS. RLS sigue activo en la base como defensa
en profundidad para accesos que NO pasen por este backend.

Este cliente NUNCA debe usarse para operaciones iniciadas directamente
por un cliente externo sin pasar por get_current_tenant() primero.
"""
from supabase import Client, create_client

from app.config import settings

supabase_admin: Client = create_client(
    settings.supabase_url,
    settings.supabase_secret_key,
)

# Cliente con la publishable key — respeta RLS, se usa SOLO para operaciones
# de Auth que deben ejecutarse "como el usuario" (ej. sign_in_with_password
# en las herramientas de desarrollo), nunca para leer datos de negocio.
supabase_public: Client = create_client(
    settings.supabase_url,
    settings.supabase_publishable_key,
)
