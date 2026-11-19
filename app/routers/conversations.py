"""
Endpoint que demuestra las 3 capas de defensa contra BOLA:
Capa 1 (JWT -> tenant_id) + Capa 2 (Repository Pattern) + Capa 3
(verificación de pertenencia, implícita en el filtro .eq de la Capa 2
para este caso simple; se vuelve explícita en endpoints que reciben
un ID específico, ver get_conversation()).

Los endpoints son `def` (no `async def`): el cliente de Supabase es
síncrono, así que FastAPI los ejecuta en su threadpool sin bloquear el loop.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.repository import ConversationsRepository
from app.security import get_current_tenant

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
def list_conversations(tenant_id: str = Depends(get_current_tenant)):
    repo = ConversationsRepository(tenant_id)
    conversations = repo.list_conversations()
    return {"tenant_id": tenant_id, "conversations": conversations}


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, tenant_id: str = Depends(get_current_tenant)):
    repo = ConversationsRepository(tenant_id)
    conversation = repo.get_conversation(conversation_id)
    if not conversation:
        # 404, no 403 — no revela si el objeto existe en otro tenant
        raise HTTPException(status_code=404, detail="No encontrado")
    return conversation
