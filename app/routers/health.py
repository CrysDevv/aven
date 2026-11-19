from fastapi import APIRouter, Response

from app.redis_client import redis_client

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health():
    """Liveness simple — el proceso está vivo. No valida dependencias."""
    return {"status": "ok", "service": "aven-api"}


@router.get("/api/health/ready")
async def readiness(response: Response):
    """
    Readiness real — valida que Redis esté alcanzable. Usado por el
    healthcheck de Docker y por el orquestador para saber si el
    contenedor puede recibir tráfico.
    """
    try:
        await redis_client.ping()
        return {"status": "ready", "redis": "ok"}
    except Exception as e:
        response.status_code = 503
        return {"status": "not_ready", "redis": "unreachable", "error": str(e)}
