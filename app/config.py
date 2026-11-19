"""
Configuración central de Aven API.
Todos los secretos se leen de variables de entorno — en producción,
inyectadas por el Secret Manager (nunca hardcodeadas ni en .env versionado).
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Meta / WhatsApp ---
    meta_app_secret: str = "dev-secret-CAMBIAR-EN-PRODUCCION"
    meta_verify_token: str = "dev-verify-token-CAMBIAR-EN-PRODUCCION"

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379

    # --- Supabase ---
    supabase_url: str
    supabase_publishable_key: str   # equivalente a la antigua "anon key" — respeta RLS
    supabase_secret_key: str        # equivalente a la antigua "service_role key" — bypasea RLS, SOLO backend
    supabase_jwks_url: str          # llaves públicas ES256 para validar JWTs emitidos por Supabase Auth

    # --- n8n ---
    n8n_url: str = "http://n8n:5678"      # hostname interno de Docker Compose
    n8n_api_key: str = ""                  # se genera manualmente la primera vez — ver README
    templates_dir: str = "templates"       # carpeta con manifest.json + workflow.json por plantilla
    n8n_buffer_workflow_id: str = ""       # ID del sub-flujo compartido de buffer, desplegado UNA vez

    # --- Cifrado a nivel de aplicación (Fernet) ---
    app_encryption_key: str = ""           # ver app/crypto.py — genera con Fernet.generate_key()

    # --- Google OAuth2 (Calendar) ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/api/integrations/google-calendar/callback"
    google_oauth_state_secret: str = ""    # firma el parámetro "state" — evita que alguien fuerce el callback a otro tenant

    # --- Ventana de servicio de WhatsApp ---
    service_window_hours: int = 24

    # --- TTL de deduplicación (debe cubrir al menos la ventana de servicio) ---
    dedup_ttl_seconds: int = 60 * 60 * 24  # 24h

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
