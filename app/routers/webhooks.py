"""
Flujo A del documento de arquitectura: Recepcion de mensajes de Meta.

Orden estricto (no alterar):
1. Validar firma HMAC sobre bytes crudos.
2. Validar estructura del payload (Pydantic).
3. Resolver tenant_id a partir de value.metadata.phone_number_id.
4. Deduplicar por message_id (Redis, TTL 24h).
5. Encolar (RQ) y responder 200 OK rapido -- el procesamiento real (IA,
   wallet, ventana de 24h) ocurre en el worker, no aqui.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.limiter import limiter
from app.queue_client import enqueue_incoming_message
from app.redis_client import redis_client
from app.repository import resolve_tenant_id_by_phone_number_id
from app.schemas import MetaWebhookPayload, WebhookAckResponse
from app.security import verify_webhook_signature

logger = logging.getLogger("aven.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.get("/meta")
async def verify_meta_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    """Endpoint de verificacion exigido por Meta al configurar el webhook."""
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Token de verificacion invalido")


@router.post("/meta", response_model=WebhookAckResponse)
@limiter.limit("60/second")
async def receive_meta_webhook(request: Request, raw_body: bytes = Depends(verify_webhook_signature)):
    # Firma ya validada por la dependency. Ahora se parsea y valida estructura.
    try:
        payload_dict = json.loads(raw_body)
        payload = MetaWebhookPayload.model_validate(payload_dict)
    except Exception:
        logger.warning("Payload de Meta con estructura inesperada, se ignora")
        return WebhookAckResponse(status="ignored")

    for entry in payload.entry:
        for change in entry.changes:
            value = change.value
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            messages = value.get("messages", [])

            if not messages:
                continue  # puede ser un evento de "status" (entregado/leido), no un mensaje

            if not phone_number_id:
                logger.warning("Payload sin metadata.phone_number_id -- se ignora")
                continue

            try:
                # La resolucion usa el cliente sincrono de Supabase: se ejecuta
                # en el threadpool para no bloquear el event loop del webhook.
                tenant_id = await run_in_threadpool(resolve_tenant_id_by_phone_number_id, phone_number_id)
            except Exception as e:
                # Un fallo aqui (ej. Supabase caido transitoriamente) NUNCA
                # debe tumbar el webhook completo -- Meta exige una respuesta
                # rapida y confiable. Se loguea como error (para alertar),
                # se salta este batch de mensajes, y se sigue con el resto.
                logger.error(f"Error resolviendo tenant para phone_number_id {phone_number_id}: {e}")
                continue

            if not tenant_id:
                # No es un error del cliente (Meta) -- es un numero que nos
                # llega pero no tenemos registrado. Se loguea para revisar,
                # nunca se lanza una excepcion que le devuelva 500 a Meta.
                logger.warning(f"phone_number_id sin tenant asociado: {phone_number_id}")
                continue

            for msg in messages:
                message_id = msg.get("id")
                if not message_id:
                    continue

                dedup_key = f"meta:msg:{message_id}"
                # SET ... NX: solo escribe si la llave NO existe -- atomico,
                # evita condicion de carrera entre workers concurrentes.
                is_new = await redis_client.set(
                    dedup_key, "received", nx=True, ex=settings.dedup_ttl_seconds
                )

                if not is_new:
                    logger.info(f"Mensaje duplicado ignorado: {message_id}")
                    continue

                enqueue_incoming_message(tenant_id=tenant_id, message=msg)
                logger.info(f"Mensaje encolado para tenant {tenant_id}: {message_id}")

    return WebhookAckResponse(status="received")
