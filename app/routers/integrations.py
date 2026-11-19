"""
Flujo de conexion de Google Calendar (OAuth2 manejado por FastAPI, ver
app/google_oauth.py). El dueño del negocio hace clic en "Conectar
Google Calendar" en la app -> GET /connect le devuelve la URL de Google
-> el navegador redirige -> Google redirige de vuelta a /callback con
el codigo -> se intercambia por tokens y se guardan cifrados.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.google_oauth import (
    GoogleTokenError,
    OAuthStateError,
    build_authorization_url,
    exchange_code_for_tokens,
    verify_and_extract_tenant_from_state,
)
from app.limiter import limiter
from app.repository import TenantIntegrationsRepository
from app.security import get_current_tenant

logger = logging.getLogger("aven.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/google-calendar/connect")
async def connect_google_calendar(tenant_id: str = Depends(get_current_tenant)):
    """Requiere JWT -- el dueño ya está autenticado en la app cuando pide esto."""
    url = build_authorization_url(tenant_id)
    return {"authorization_url": url}


@router.get("/google-calendar/callback")
@limiter.limit("20/minute")
async def google_calendar_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """
    Este endpoint lo llama GOOGLE (redirigiendo el navegador del dueño),
    no la app directamente -- por eso NO tiene get_current_tenant como
    dependency (no hay JWT de Aven en esta request). El tenant se
    recupera exclusivamente del `state` firmado.
    """
    if error:
        logger.warning(f"Google OAuth callback con error: {error}")
        return RedirectResponse(url="/onboarding/calendar-error?reason=denied")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Faltan parametros code/state en el callback")

    try:
        tenant_id = verify_and_extract_tenant_from_state(state)
    except OAuthStateError as e:
        logger.warning(f"State invalido en callback de Google: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    try:
        tokens = await exchange_code_for_tokens(code)
    except GoogleTokenError as e:
        logger.error(f"Fallo el intercambio de codigo por tokens: {e}")
        raise HTTPException(status_code=502, detail="No se pudo completar la conexion con Google")

    repo = TenantIntegrationsRepository(tenant_id)
    await repo.save_google_calendar_tokens(tokens)

    # En producción esto redirige a una pantalla de éxito de la app/panel,
    # no a un JSON — placeholder hasta que exista esa pantalla.
    return RedirectResponse(url="/onboarding/calendar-connected")


@router.get("/google-calendar/status")
async def google_calendar_status(tenant_id: str = Depends(get_current_tenant)):
    repo = TenantIntegrationsRepository(tenant_id)
    token = await repo.get_valid_access_token()
    return {"connected": token is not None}
