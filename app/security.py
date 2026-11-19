"""
Seguridad: validación de firma de Meta (webhook) y resolución de tenant_id
desde el JWT de Supabase (nunca desde el body de la request — ver Anexo de
Seguridad, API1:2023 Broken Object Level Authorization).

El JWT se valida contra las llaves públicas ES256 publicadas en el JWKS
de Supabase (SUPABASE_JWKS_URL) — NO contra un secreto compartido. Esto
es obligatorio: los proyectos nuevos de Supabase firman con llaves
asimétricas, y aunque existiera un secreto legado, validar con la llave
pública es la práctica correcta (el backend nunca necesita conocer un
secreto de firma que no es suyo).
"""
import hashlib
import hmac

import jwt
from fastapi import Depends, Header, HTTPException, Request
from jwt import PyJWKClient

from app.config import settings

# PyJWKClient cachea las llaves públicas en memoria (evita golpear el
# endpoint de JWKS en cada request) y las refresca automáticamente si
# aparece un 'kid' que no conoce (rotación de llaves sin downtime).
_jwks_client = PyJWKClient(settings.supabase_jwks_url, cache_keys=True)


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> None:
    """
    Valida X-Hub-Signature-256 sobre los BYTES CRUDOS del body.
    Debe llamarse ANTES de cualquier parseo a JSON.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Firma ausente o con formato inválido")

    expected_signature = hmac.new(
        key=settings.meta_app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    received_signature = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected_signature, received_signature):
        raise HTTPException(status_code=401, detail="Firma inválida")


async def verify_webhook_signature(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> bytes:
    raw_body = await request.body()
    verify_meta_signature(raw_body, x_hub_signature_256)
    return raw_body


def get_current_tenant(authorization: str | None = Header(default=None)) -> str:
    """
    Deriva el tenant_id EXCLUSIVAMENTE del JWT de Supabase, validado
    contra el JWKS real (firma ES256) — nunca desde el body ni un
    query param.

    Requiere que el usuario tenga app_metadata.tenant_id seteado al
    crearlo (ver app/routers/dev_tools.py) — Supabase incluye
    app_metadata en el JWT automáticamente, sin necesitar un Custom
    Access Token Hook para este caso simple.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de autorización requerido")

    token = authorization.removeprefix("Bearer ")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",  # audiencia estándar de Supabase Auth
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido o expirado: {e}")

    app_metadata = payload.get("app_metadata", {})
    tenant_id = app_metadata.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Token sin tenant_id asociado")

    return tenant_id
