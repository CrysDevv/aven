"""
Cliente de la cola RQ (Redis Queue). RQ necesita una conexion SINCRONA
de redis-py (no redis.asyncio) para encolar jobs -- es una libreria
separada de la conexion async que usa el resto de la app para
deduplicacion (app/redis_client.py).
"""
import redis
from rq import Queue

from app.config import settings

_sync_redis = redis.Redis(host=settings.redis_host, port=settings.redis_port)
incoming_messages_queue = Queue("incoming_messages", connection=_sync_redis)


def enqueue_incoming_message(tenant_id: str, message: dict) -> None:
    # Import diferido: evita import circular (worker.py importa cosas que
    # a su vez podrian importar este modulo).
    from app.worker import process_incoming_message

    incoming_messages_queue.enqueue(
        process_incoming_message,
        tenant_id=tenant_id,
        raw_message=message,
        # Debe cubrir la espera del buffer de n8n (12s+) mas la inferencia del LLM.
        job_timeout=90,
        retry=None,  # los reintentos por fallo de red a n8n se manejan explicitamente dentro del worker
    )
