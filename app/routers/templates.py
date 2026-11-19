"""
Flujo D del documento de arquitectura: Inyeccion de Plantillas (n8n Headless).

GET  /api/templates              -> catalogo de plantillas disponibles
GET  /api/templates/{id}         -> detalle + variables que expone
POST /api/templates/{id}/deploy  -> sustituye variables, sanitiza,
                                     crea y activa el flujo en n8n, y
                                     guarda el n8n_workflow_id en Supabase
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.config import settings
from app.limiter import limiter
from app.n8n_client import N8nAPIError, N8nClient
from app.repository import MetaConnectionsRepository
from app.security import get_current_tenant
from app.template_engine import TemplateError, TemplateSanitizationError, get_manifest, list_templates, render_workflow

router = APIRouter(prefix="/api/templates", tags=["templates"])


class DeployTemplateInput(BaseModel):
    variables: dict[str, str]


@router.get("")
async def get_templates():
    return {"templates": list_templates()}


@router.get("/{template_id}")
async def get_template_detail(template_id: str):
    try:
        return get_manifest(template_id)
    except TemplateError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{template_id}/deploy")
@limiter.limit("5/minute")
async def deploy_template(
    request: Request,
    template_id: str,
    input: DeployTemplateInput,
    tenant_id: str = Depends(get_current_tenant),
):
    # 0. Confirmar que el tenant tiene un numero de WhatsApp conectado
    #    ANTES de gastar tiempo en renderizar o llamar a n8n -- tambien
    #    necesitamos su phone_number_id para las variables de sistema.
    #    El repositorio es sincrono (supabase-py): se ejecuta en el
    #    threadpool para no bloquear el event loop.
    meta_repo = MetaConnectionsRepository(tenant_id)
    connection = await run_in_threadpool(meta_repo.get_connection)
    if not connection:
        raise HTTPException(
            status_code=409,
            detail="Este negocio todavia no tiene un numero de WhatsApp conectado. Conecta un numero antes de desplegar una plantilla.",
        )

    if not settings.n8n_buffer_workflow_id:
        raise HTTPException(
            status_code=500,
            detail="N8N_BUFFER_WORKFLOW_ID no esta configurado -- despliega primero el sub-flujo compartido de buffer (templates/00-shared-message-buffer) y guarda su ID en .env.",
        )

    system_variables = {
        "tenant_id": tenant_id,
        "phone_number_id": connection["phone_number_id"],
        "buffer_workflow_id": settings.n8n_buffer_workflow_id,
    }

    # 1. Renderizar (sustituir variables + sanitizar) -- puede fallar por
    #    variables faltantes/no declaradas, o por nodos peligrosos.
    try:
        workflow = render_workflow(template_id, input.variables, system_variables)
    except TemplateSanitizationError as e:
        raise HTTPException(status_code=400, detail=f"Plantilla rechazada por seguridad: {e}")
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 2. Nombrar el workflow de forma unica por tenant, Y forzar el path
    #    del nodo Webhook a ser unico por tenant. n8n exige paths de
    #    webhook unicos a nivel de INSTANCIA -- si dos tenants desplegaran
    #    la misma plantilla con el path que trae el JSON tal cual, el
    #    segundo despliegue chocaria con el primero. Se sobreescribe
    #    siempre a "wh-{tenant_id}", sin importar lo que traiga la
    #    plantilla, y de forma DETERMINISTICA -- asi el worker puede
    #    calcular la misma URL sin tener que consultarla en ningun lado.
    workflow["name"] = f"{workflow['name']} — {tenant_id[:8]}"
    webhook_path = f"wh-{tenant_id}"
    webhook_nodes_found = 0
    for node in workflow.get("nodes", []):
        if node.get("type") == "n8n-nodes-base.webhook":
            node.setdefault("parameters", {})["path"] = webhook_path
            webhook_nodes_found += 1

    if webhook_nodes_found == 0:
        raise HTTPException(
            status_code=400,
            detail="La plantilla no tiene ningun nodo Webhook -- no se puede disparar desde el sistema.",
        )

    # 3. Crear + activar en n8n (con mitigacion del bug de registro de
    #    webhooks -- ver app/n8n_client.py).
    client = N8nClient()
    try:
        deploy_result = await client.deploy_workflow(workflow)
    except N8nAPIError as e:
        raise HTTPException(status_code=502, detail=f"Error al desplegar en n8n: {e}")

    # 4. Persistir el n8n_workflow_id en Supabase.
    await run_in_threadpool(meta_repo.save_n8n_workflow_id, deploy_result["workflow_id"])

    return {
        "template_id": template_id,
        "tenant_id": tenant_id,
        **deploy_result,
    }
