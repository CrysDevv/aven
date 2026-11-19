"""
Repository Pattern — Capa 2 de la defensa contra BOLA (ver Anexo de
Seguridad, API1:2023). Usa supabase_admin (secret key, bypasea RLS),
así que el filtro `tenant_id` aquí NO es opcional — es la única barrera
real para este cliente. RLS en Supabase sigue activo como Capa adicional
para cualquier acceso que no pase por este backend.

supabase_admin es el cliente SÍNCRONO de supabase-py: los métodos que solo
lo usan son `def` normales, para que FastAPI los ejecute en su threadpool
sin bloquear el event loop. Solo es `async def` lo que realmente hace `await`.
"""
from app.supabase_client import supabase_admin


class BaseRepository:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id


class ConversationsRepository(BaseRepository):
    def list_conversations(self, limit: int = 50):
        response = (
            supabase_admin.table("conversations")
            .select("*")
            .eq("tenant_id", self.tenant_id)  # obligatorio — nunca omitir
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return response.data

    def get_conversation(self, conversation_id: str):
        response = (
            supabase_admin.table("conversations")
            .select("*")
            .eq("tenant_id", self.tenant_id)
            .eq("id", conversation_id)
            .maybe_single()
            .execute()
        )
        # Capa 3: si no vino nada, o pertenece a otro tenant, es 404 —
        # el filtro .eq("tenant_id", ...) ya lo garantiza, pero devolver
        # None explícito aquí deja claro el contrato para el endpoint.
        return response.data if response else None


class WalletRepository(BaseRepository):
    def get_balance(self) -> int:
        response = (
            supabase_admin.rpc(
                "get_wallet_balance", {"p_tenant_id": self.tenant_id}
            ).execute()
        )
        return response.data or 0

    def record_consumption(self, message_id: str, amount: int = -1) -> bool:
        """
        Descuenta saldo por un mensaje enviado. Devuelve False si ya se
        había descontado antes por este message_id (protección de
        idempotencia a nivel de base de datos — ver constraint única
        en wallet_ledger del script SQL).
        """
        try:
            supabase_admin.table("wallet_ledger").insert(
                {
                    "tenant_id": self.tenant_id,
                    "entry_type": "consumption",
                    "amount_messages": amount,
                    "message_id": message_id,
                }
            ).execute()
            return True
        except Exception as e:
            if "uq_wallet_ledger_consumption_message" in str(e):
                return False  # ya se había descontado — no es un error, es idempotencia funcionando
            raise


class MetaConnectionsRepository(BaseRepository):
    def get_connection(self):
        response = (
            supabase_admin.table("meta_connections")
            .select("*")
            .eq("tenant_id", self.tenant_id)
            .maybe_single()
            .execute()
        )
        return response.data if response else None

    def save_n8n_workflow_id(self, n8n_workflow_id: str) -> None:
        """
        Guarda el n8n_workflow_id en la conexión de Meta del tenant.
        Requiere que ya exista una fila en meta_connections (se crea al
        conectar el número de WhatsApp vía Embedded Signup — ver
        Documentación Maestra, Flujo E). Si no existe, falla con un
        mensaje claro en vez de crear una fila con datos falsos.
        """
        connection = self.get_connection()
        if not connection:
            raise ValueError(
                "Este tenant no tiene un número de WhatsApp conectado todavía "
                "(no existe fila en meta_connections). Conecta un número antes "
                "de desplegar una plantilla."
            )
        supabase_admin.table("meta_connections").update(
            {"n8n_workflow_id": n8n_workflow_id}
        ).eq("tenant_id", self.tenant_id).execute()


class TenantIntegrationsRepository(BaseRepository):
    async def save_google_calendar_tokens(self, tokens: dict, external_account_email: str | None = None) -> None:
        """
        Guarda (upsert) las credenciales de Google Calendar, cifradas
        antes de que el dato salga del proceso Python. `tokens` es la
        respuesta cruda del endpoint de token de Google.
        """
        from datetime import datetime, timedelta, timezone

        from app.crypto import encrypt_secret

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))

        payload = {
            "tenant_id": self.tenant_id,
            "provider": "google_calendar",
            "access_token_enc": encrypt_secret(tokens["access_token"]),
            "token_expires_at": expires_at.isoformat(),
            "scope": tokens.get("scope", ""),
            "status": "active",
        }
        # El refresh_token solo llega la PRIMERA vez que el usuario autoriza
        # (con prompt=consent) -- en refrescos posteriores Google no lo
        # reenvia, asi que solo se sobreescribe si vino en la respuesta.
        if "refresh_token" in tokens:
            payload["refresh_token_enc"] = encrypt_secret(tokens["refresh_token"])
        if external_account_email:
            payload["external_account_email"] = external_account_email

        supabase_admin.table("tenant_integrations").upsert(
            payload, on_conflict="tenant_id,provider"
        ).execute()

    async def get_valid_access_token(self) -> str | None:
        """
        Devuelve un access_token vigente para Google Calendar, refrescandolo
        automaticamente si esta vencido o a punto de vencer. Devuelve None
        si el tenant nunca conecto Google Calendar.
        """
        from datetime import datetime, timedelta, timezone

        from app.crypto import decrypt_secret
        from app.google_oauth import refresh_access_token

        response = (
            supabase_admin.table("tenant_integrations")
            .select("*")
            .eq("tenant_id", self.tenant_id)
            .eq("provider", "google_calendar")
            .maybe_single()
            .execute()
        )
        if not response or not response.data:
            return None

        row = response.data
        if row["status"] != "active":
            return None

        expires_at = datetime.fromisoformat(row["token_expires_at"])
        # Margen de 60s para evitar que el token expire a mitad de la
        # llamada a n8n/Google por una diferencia de reloj minima.
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return decrypt_secret(row["access_token_enc"])

        # Vencido -> refrescar usando el refresh_token guardado
        if not row.get("refresh_token_enc"):
            return None  # no hay como refrescar -- el tenant debe reconectar
        refresh_token = decrypt_secret(row["refresh_token_enc"])
        new_tokens = await refresh_access_token(refresh_token)
        await self.save_google_calendar_tokens(new_tokens)
        return new_tokens["access_token"]


class CatalogRepository(BaseRepository):
    def list_items(self, item_type: str | None = None, only_active: bool = True):
        query = supabase_admin.table("catalog_items").select("*").eq("tenant_id", self.tenant_id)
        if item_type:
            query = query.eq("item_type", item_type)
        if only_active:
            query = query.eq("is_active", True)
        response = query.order("name").execute()
        return response.data

    def get_item(self, item_id: str):
        response = (
            supabase_admin.table("catalog_items")
            .select("*")
            .eq("tenant_id", self.tenant_id)
            .eq("id", item_id)
            .maybe_single()
            .execute()
        )
        return response.data if response else None

    def create_item(self, item: dict) -> dict:
        payload = {**item, "tenant_id": self.tenant_id}  # tenant_id SIEMPRE del contexto, nunca del body
        response = supabase_admin.table("catalog_items").insert(payload).execute()
        return response.data[0]

    def update_item(self, item_id: str, updates: dict) -> dict | None:
        # Capa 3: el .eq("tenant_id", ...) garantiza que no se pueda editar
        # un item de otro tenant aunque alguien adivine su UUID.
        response = (
            supabase_admin.table("catalog_items")
            .update(updates)
            .eq("tenant_id", self.tenant_id)
            .eq("id", item_id)
            .execute()
        )
        return response.data[0] if response.data else None

    def delete_item(self, item_id: str) -> bool:
        response = (
            supabase_admin.table("catalog_items")
            .delete()
            .eq("tenant_id", self.tenant_id)
            .eq("id", item_id)
            .execute()
        )
        return len(response.data) > 0


def resolve_tenant_id_by_phone_number_id(phone_number_id: str) -> str | None:
    """
    Resuelve qué tenant es dueño de un phone_number_id de Meta.

    Deliberadamente NO usa BaseRepository (no está acotado por tenant_id)
    porque este es justamente el paso que DESCUBRE el tenant a partir de
    un dato que llega de un tercero (Meta) — se usa exclusivamente desde
    el webhook receiver, nunca desde un endpoint que reciba input directo
    de un cliente. Devuelve None si el número no está registrado (no
    debería pasar en producción, pero no se asume).

    Es síncrona: los llamadores async deben invocarla con run_in_threadpool.
    """
    response = (
        supabase_admin.table("meta_connections")
        .select("tenant_id")
        .eq("phone_number_id", phone_number_id)
        .maybe_single()
        .execute()
    )
    if not response or not response.data:
        return None
    return response.data["tenant_id"]
