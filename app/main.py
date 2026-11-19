import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from app.limiter import limiter
from app.routers import catalog, conversations, health, integrations, templates, wallet, webhooks

app = FastAPI(title="Aven API", version="0.1.0")

# Rate limiting -- ver Anexo de Seguridad, API4:2023 (Unrestricted
# Resource Consumption). La instancia de Limiter vive en app/limiter.py
# y se importa aqui Y en cada router que la necesite -- SIEMPRE la misma
# instancia, nunca una nueva por archivo.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Ver Anexo de Seguridad, sección 12."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS abierto SOLO en desarrollo, para que la página HTML de debug (servida
# desde el mismo origen aquí, pero podría no estarlo) pueda llamar a la API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if os.getenv("AVEN_ENV", "dev") == "dev" else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(conversations.router)
app.include_router(wallet.router)
app.include_router(templates.router)
app.include_router(integrations.router)
app.include_router(catalog.router)

# Router de herramientas de desarrollo — SOLO se monta si AVEN_ENV=dev
if os.getenv("AVEN_ENV", "dev") == "dev":
    from app.routers import dev_tools

    app.include_router(dev_tools.router)

# Entorno de pruebas HTML/CSS plano (Fase 1)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
