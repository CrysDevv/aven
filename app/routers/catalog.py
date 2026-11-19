"""
Gestión de catálogo (productos y servicios) desde el panel del dueño del
negocio. Misma pieza de datos que consulta la IA vía el nodo Postgres
Tool en n8n (tabla catalog_items) -- lo que el dueño agrega/edita aquí
la IA lo ve automáticamente, sin duplicación de datos.

Los endpoints son `def` (no `async def`): el cliente de Supabase es
síncrono, así que FastAPI los ejecuta en su threadpool sin bloquear el loop.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.repository import CatalogRepository
from app.security import get_current_tenant

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


class CatalogItemInput(BaseModel):
    item_type: str = Field(pattern="^(product|service)$")
    sku: str | None = None
    name: str
    description: str | None = None
    price: int = Field(ge=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=0)
    image_url: str | None = None


class CatalogItemUpdate(BaseModel):
    # Todos opcionales -- PATCH parcial. tenant_id e id se ignoran si
    # vinieran en el body (nunca se leen de aquí, ver CatalogRepository).
    name: str | None = None
    description: str | None = None
    price: int | None = Field(default=None, ge=0)
    stock_quantity: int | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    is_active: bool | None = None


@router.get("")
def list_catalog(item_type: str | None = None, tenant_id: str = Depends(get_current_tenant)):
    if item_type and item_type not in ("product", "service"):
        raise HTTPException(status_code=400, detail="item_type debe ser 'product' o 'service'")
    repo = CatalogRepository(tenant_id)
    items = repo.list_items(item_type=item_type)
    return {"items": items}


@router.get("/{item_id}")
def get_catalog_item(item_id: str, tenant_id: str = Depends(get_current_tenant)):
    repo = CatalogRepository(tenant_id)
    item = repo.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="No encontrado")
    return item


@router.post("", status_code=201)
def create_catalog_item(input: CatalogItemInput, tenant_id: str = Depends(get_current_tenant)):
    if input.item_type == "product" and not input.sku:
        raise HTTPException(status_code=400, detail="Los productos requieren SKU")
    repo = CatalogRepository(tenant_id)
    try:
        item = repo.create_item(input.model_dump(exclude_none=True))
    except Exception as e:
        if "uq_catalog_items_tenant_sku" in str(e):
            raise HTTPException(status_code=409, detail=f"Ya existe un producto con el SKU '{input.sku}'")
        raise
    return item


@router.patch("/{item_id}")
def update_catalog_item(item_id: str, input: CatalogItemUpdate, tenant_id: str = Depends(get_current_tenant)):
    repo = CatalogRepository(tenant_id)
    updated = repo.update_item(item_id, input.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="No encontrado")
    return updated


@router.delete("/{item_id}", status_code=204)
def delete_catalog_item(item_id: str, tenant_id: str = Depends(get_current_tenant)):
    repo = CatalogRepository(tenant_id)
    deleted = repo.delete_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No encontrado")
