"""
⚠️ SOLO PARA DESARROLLO. Nunca se monta en producción (ver app/main.py).

Estas herramientas necesitan conectividad real a Supabase — a diferencia
de la versión anterior (que firmaba JWTs falsos con HS256), ahora crean
un tenant y un usuario de PRUEBA de verdad en tu proyecto, e inician
sesión real vía Supabase Auth para obtener un token auténtico firmado
con la llave privada real de tu proyecto (ES256). Esto prueba el flujo
completo, no una simulación.
"""
import hashlib
import hmac
import json
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.limiter import limiter

from app.config import settings
from app.supabase_client import supabase_admin, supabase_public

router = APIRouter(prefix="/api/dev", tags=["dev-tools (solo pruebas)"])


class SimulateWebhookInput(BaseModel):
    customer_phone: str = "+56912345678"
    message_text: str = "Hola, ¿tienen disponible el producto X?"
    phone_number_id: str = "dev-phone-id"  # debe coincidir con el de un tenant real para que se resuelva


@router.post("/simulate-webhook")
@limiter.limit("20/minute")
async def simulate_meta_webhook(request: Request, input: SimulateWebhookInput):
    fake_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "dev-waba-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "56900000000",
                                "phone_number_id": input.phone_number_id,
                            },
                            "messages": [
                                {
                                    "id": f"wamid.dev.{uuid.uuid4()}",
                                    "from": input.customer_phone,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": input.message_text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    raw_body = json.dumps(fake_payload).encode("utf-8")
    signature = hmac.new(
        settings.meta_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/api/webhooks/meta",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
        )

    try:
        respuesta_webhook = response.json()
    except Exception:
        respuesta_webhook = {"raw_text": response.text}

    return {
        "payload_enviado": fake_payload,
        "respuesta_webhook": respuesta_webhook,
        "status_code": response.status_code,
    }


@router.post("/generate-test-jwt")
@limiter.limit("10/minute")
async def generate_test_jwt(request: Request):
    """
    Crea un tenant + un usuario de prueba REALES en tu Supabase, y
    devuelve un access_token REAL (firmado con la llave privada de tu
    proyecto). Requiere que ya hayas corrido el script SQL del esquema
    (aven_schema.sql) contra tu base — si no, fallará al insertar en
    `tenants` con un error claro.
    """
    suffix = uuid.uuid4().hex[:8]
    email = f"dev-test-{suffix}@aven-testing.local"
    password = f"Test-{uuid.uuid4().hex}!A1"

    # 1. Crear el tenant de prueba
    try:
        tenant_response = (
            supabase_admin.table("tenants")
            .insert({"business_name": f"Tenant de prueba {suffix}"})
            .execute()
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "No se pudo crear el tenant de prueba. ¿Ya ejecutaste "
                f"aven_schema.sql contra tu proyecto de Supabase? Error: {e}"
            ),
        )
    tenant_id = tenant_response.data[0]["id"]

    # 1b. Crear una conexión de Meta de PRUEBA (simula que el dueño ya
    #     conectó su número de WhatsApp vía Embedded Signup — ese flujo
    #     real aún no está construido, esto es un placeholder de dev
    #     para poder probar el despliegue de plantillas de punta a punta).
    try:
        supabase_admin.table("meta_connections").insert(
            {
                "tenant_id": tenant_id,
                "waba_id": f"dev-waba-{suffix}",
                "phone_number_id": f"dev-phone-{suffix}",
                "status": "active",
            }
        ).execute()
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"No se pudo crear la conexión de Meta de prueba: {e}"
        )

    # 2. Crear el usuario con app_metadata.tenant_id — Supabase incluye
    #    app_metadata en el JWT automáticamente, sin necesitar un Custom
    #    Access Token Hook para este caso.
    try:
        supabase_admin.auth.admin.create_user(
            {
                "email": email,
                "password": password,
                "email_confirm": True,
                "app_metadata": {"tenant_id": tenant_id},
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo crear el usuario de prueba: {e}")

    # 3. Iniciar sesión real para obtener un access_token auténtico
    try:
        session_response = supabase_public.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo iniciar sesión de prueba: {e}")

    return {
        "tenant_id": tenant_id,
        "test_user_email": email,
        "phone_number_id": f"dev-phone-{suffix}",
        "access_token": session_response.session.access_token,
        "nota": "Este token es REAL, firmado por tu proyecto de Supabase — no una simulación. Usa phone_number_id para probar /api/dev/simulate-webhook contra este mismo tenant.",
    }
