"""
Worker que procesa la cola `incoming_messages` (RQ). Es el resto del
Flujo A que el webhook receiver no hace: guardar el mensaje SIEMPRE,
revisar pausas/ventana/saldo, y disparar el flujo de n8n del tenant.

Se ejecuta como proceso aparte: `rq worker incoming_messages` (ver
docker-compose.yml, servicio "rq-worker"). Las funciones aqui son
SINCRONAS a proposito -- RQ ejecuta jobs sincronos, y el cliente de
Supabase (create_client, no create_async_client) tambien es sincrono.
UNICA excepcion: obtener el token de Google Calendar usa un metodo
async del Repository Pattern (TenantIntegrationsRepository), envuelto
en asyncio.run() en el unico punto donde hace falta -- ver mas abajo.
"""
import asyncio
import logging
import time

import httpx

from app.config import settings
from app.repository import TenantIntegrationsRepository
from app.supabase_client import supabase_admin

logger = logging.getLogger("aven.worker")


def _get_or_create_conversation(tenant_id: str, customer_phone: str) -> dict:
    existing = (
        supabase_admin.table("conversations")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("customer_phone", customer_phone)
        .maybe_single()
        .execute()
    )
    if existing and existing.data:
        return existing.data

    created = (
        supabase_admin.table("conversations")
        .insert({"tenant_id": tenant_id, "customer_phone": customer_phone})
        .execute()
    )
    return created.data[0]


def _save_message(tenant_id: str, conversation_id: str, meta_message_id: str, content: str | None) -> None:
    supabase_admin.table("messages").insert(
        {
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "meta_message_id": meta_message_id,
            "direction": "inbound",
            "content": content,
        }
    ).execute()


def _update_last_customer_message_at(conversation_id: str, timestamp_unix: int) -> None:
    from datetime import datetime, timezone

    iso_ts = datetime.fromtimestamp(timestamp_unix, tz=timezone.utc).isoformat()
    supabase_admin.table("conversations").update(
        {"last_customer_message_at": iso_ts}
    ).eq("id", conversation_id).execute()


def _is_ai_paused(tenant_id: str, conversation: dict) -> tuple[bool, str]:
    if conversation.get("ai_paused"):
        return True, "ai_paused_conversation"

    tenant = (
        supabase_admin.table("tenants")
        .select("ai_globally_paused")
        .eq("id", tenant_id)
        .maybe_single()
        .execute()
    )
    if tenant and tenant.data and tenant.data.get("ai_globally_paused"):
        return True, "ai_paused_tenant"

    return False, ""


def _get_wallet_balance(tenant_id: str) -> int:
    response = supabase_admin.rpc("get_wallet_balance", {"p_tenant_id": tenant_id}).execute()
    return response.data or 0


def _record_wallet_consumption(tenant_id: str, message_id: str) -> bool:
    """Devuelve False si ya se habia descontado antes (idempotencia real, no solo del lock de Redis)."""
    try:
        supabase_admin.table("wallet_ledger").insert(
            {
                "tenant_id": tenant_id,
                "entry_type": "consumption",
                "amount_messages": -1,
                "message_id": message_id,
            }
        ).execute()
        return True
    except Exception as e:
        if "uq_wallet_ledger_consumption_message" in str(e):
            return False
        raise


def process_incoming_message(tenant_id: str, raw_message: dict) -> dict:
    """
    Punto de entrada del job de RQ. Devuelve un dict de resultado (RQ lo
    guarda como resultado del job) -- nunca lanza excepcion para casos
    de negocio normales (pausado, fuera de ventana, sin saldo); solo
    lanza para errores reales de infraestructura, que RQ debe poder
    reintentar/alertar.
    """
    message_id = raw_message.get("id")
    customer_phone = raw_message.get("from")
    msg_type = raw_message.get("type", "text")
    text_body = raw_message.get("text", {}).get("body") if msg_type == "text" else None
    timestamp = int(raw_message.get("timestamp", time.time()))

    logger.info(f"Procesando mensaje {message_id} del tenant {tenant_id}")

    # 1. SIEMPRE se guarda -- pase lo que pase despues, el dueno del
    #    negocio debe poder verlo en su inbox.
    conversation = _get_or_create_conversation(tenant_id, customer_phone)
    _save_message(tenant_id, conversation["id"], message_id, text_body)
    _update_last_customer_message_at(conversation["id"], timestamp)

    # 1b. Solo se procesan mensajes de texto con contenido. Audio, imagenes,
    #     stickers, etc. ya quedaron guardados en el inbox, pero no se llama a
    #     n8n ni se descuenta saldo.
    if msg_type != "text" or not text_body:
        logger.info(f"Mensaje {message_id}: tipo '{msg_type}' no soportado, no se dispara respuesta")
        return {"status": "skipped", "reason": "unsupported_message_type"}

    # 2. Pausas (tenant completo o esta conversacion especifica)
    paused, reason = _is_ai_paused(tenant_id, conversation)
    if paused:
        logger.info(f"Mensaje {message_id}: IA pausada ({reason}), no se dispara respuesta")
        return {"status": "skipped", "reason": reason}

    # 3. Ventana de servicio de 24h -- si el mensaje ya viene "viejo"
    #    (ej. el worker estuvo caido y hay backlog), no se dispara
    #    respuesta automatica; Meta la rechazaria de todas formas.
    now = int(time.time())
    window_seconds = settings.service_window_hours * 3600
    if now - timestamp > window_seconds:
        logger.info(f"Mensaje {message_id}: fuera de la ventana de {settings.service_window_hours}h")
        return {"status": "skipped", "reason": "outside_service_window"}

    # 4. Saldo del wallet
    balance = _get_wallet_balance(tenant_id)
    if balance <= 0:
        logger.warning(f"Mensaje {message_id}: tenant {tenant_id} sin saldo (balance={balance})")
        return {"status": "skipped", "reason": "insufficient_wallet_balance"}

    # 5. Disparar el flujo de n8n del tenant. El path es DETERMINISTICO
    #    (wh-{tenant_id}) -- ver app/routers/templates.py, se fuerza al
    #    desplegar, asi que no hace falta consultarlo en ningun lado.
    webhook_path = f"wh-{tenant_id}"
    n8n_webhook_url = f"{settings.n8n_url}/webhook/{webhook_path}"

    # Token de Google Calendar, ya vigente (refrescado si hacia falta).
    # get_valid_access_token() es async (mismo patron que el resto del
    # Repository Pattern) pero este worker es deliberadamente sincrono
    # (RQ ejecuta jobs sincronos) -- asyncio.run() es seguro aqui porque
    # no hay ya un event loop corriendo en este proceso. Si falla por
    # cualquier motivo, se sigue sin calendario en vez de tumbar el
    # mensaje completo -- el prompt del flujo ya sabe manejar ese caso.
    google_calendar_access_token = ""
    try:
        google_calendar_access_token = asyncio.run(
            TenantIntegrationsRepository(tenant_id).get_valid_access_token()
        ) or ""
    except Exception as e:
        logger.warning(f"Mensaje {message_id}: no se pudo obtener token de Google Calendar ({e}) -- se continua sin calendario")

    try:
        # El webhook de n8n responde al terminar el flujo: incluye la espera
        # del buffer (12s+) mas la inferencia del LLM, de ahi el timeout de 60s.
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                n8n_webhook_url,
                json={
                    "tenant_id": tenant_id,
                    "conversation_id": conversation["id"],
                    "customer_phone": customer_phone,
                    "message": text_body,
                    "message_type": msg_type,
                    "message_id": message_id,
                    "google_calendar_access_token": google_calendar_access_token,
                },
            )
    except httpx.RequestError as e:
        logger.error(f"Mensaje {message_id}: n8n inalcanzable ({e})")
        return {"status": "error", "reason": f"n8n_unreachable: {e}"}

    if response.status_code == 404:
        # El flujo del tenant no esta desplegado o no esta activo --
        # esto NO es un error transitorio, es un problema de config
        # del tenant. Se loguea distinto para poder alertar aparte.
        logger.warning(f"Mensaje {message_id}: flujo de n8n no encontrado para tenant {tenant_id}")
        return {"status": "error", "reason": "tenant_workflow_not_found"}

    # 6. Descontar saldo -- solo si el disparo a n8n fue exitoso.
    debited = _record_wallet_consumption(tenant_id, message_id)

    logger.info(f"Mensaje {message_id}: disparado a n8n (HTTP {response.status_code}), saldo descontado={debited}")
    return {
        "status": "triggered",
        "n8n_status_code": response.status_code,
        "wallet_debited": debited,
    }
