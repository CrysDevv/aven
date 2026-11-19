"""
Flujo OAuth2 de Google Calendar, manejado enteramente por FastAPI (no por
n8n). Decision explicita: n8n Community no maneja bien credenciales OAuth
dinamicas para decenas de tenants distintos -- FastAPI posee el flujo
completo, guarda los tokens cifrados, y le pasa un access_token vigente
al worker en el momento de disparar el flujo del tenant en n8n.

Endpoints oficiales de Google (estables, documentados):
- Autorizacion: https://accounts.google.com/o/oauth2/v2/auth
- Intercambio/refresh de tokens: https://oauth2.googleapis.com/token
- access_type=offline + prompt=consent son OBLIGATORIOS para garantizar
  que Google devuelva un refresh_token la primera vez (sin esto, en
  reconexiones Google a veces omite el refresh_token).
"""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from app.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

STATE_TTL_SECONDS = 600  # 10 minutos -- una autorizacion de Google es rapida


class OAuthStateError(Exception):
    pass


class GoogleTokenError(Exception):
    pass


def _sign_state(tenant_id: str, timestamp: int) -> str:
    message = f"{tenant_id}.{timestamp}".encode()
    signature = hmac.new(
        settings.google_oauth_state_secret.encode(), message, hashlib.sha256
    ).hexdigest()
    return f"{tenant_id}.{timestamp}.{signature}"


def build_authorization_url(tenant_id: str) -> str:
    """
    El parametro `state` viaja hasta Google y vuelve intacto en el
    callback -- es la UNICA forma de saber, cuando Google redirige de
    vuelta, a que tenant pertenece esta autorizacion (el callback de
    Google no trae el JWT de sesion de Aven). Por eso el state va
    firmado con HMAC: sin la firma, cualquiera podria forjar un state
    con un tenant_id ajeno y vincular su propia cuenta de Google a un
    negocio que no es el suyo.
    """
    state = _sign_state(tenant_id, int(time.time()))
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def verify_and_extract_tenant_from_state(state: str) -> str:
    try:
        tenant_id, timestamp_str, signature = state.split(".", 2)
        timestamp = int(timestamp_str)
    except (ValueError, AttributeError):
        raise OAuthStateError("state con formato invalido")

    expected = _sign_state(tenant_id, timestamp)
    if not hmac.compare_digest(expected, state):
        raise OAuthStateError("Firma del state invalida -- posible intento de forjar el callback")

    if time.time() - timestamp > STATE_TTL_SECONDS:
        raise OAuthStateError("El state expiro -- la autorizacion tardo demasiado, intenta de nuevo")

    return tenant_id


async def exchange_code_for_tokens(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        raise GoogleTokenError(f"Google rechazo el intercambio de codigo: {response.text}")
    return response.json()


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        raise GoogleTokenError(f"Google rechazo el refresh del token: {response.text}")
    return response.json()
