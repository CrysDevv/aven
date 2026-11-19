"""
Cliente de la API pública de n8n (docs.n8n.io/api/).

Hechos verificados contra documentación oficial (no asumidos):
- POST /api/v1/workflows crea el flujo SIEMPRE inactivo. Requiere:
  name, nodes, connections, settings.
- Activar es un paso APARTE: POST /api/v1/workflows/{id}/activate.
- Autenticación: header X-N8N-API-KEY (no Bearer).
- NO existe /api/v1/workflows/{id}/execute — para disparar un flujo desde
  fuera hay que llamar a la URL del Webhook trigger directamente, nunca
  a un endpoint de "ejecutar" de la API de gestión.
- La CLI (`n8n publish:workflow`) requiere reiniciar la instancia para
  que el cambio tome efecto — por eso este cliente usa exclusivamente
  la API REST en caliente, nunca la CLI.

Bug conocido de n8n a mitigar (n8n-io/n8n#21614, abierto): al crear y
activar un flujo por API en la misma operación, a veces el path del
Webhook NO queda registrado, y las llamadas de producción devuelven 404
aunque el flujo figure como activo. La única mitigación reportada por la
comunidad es forzar un ciclo desactivar→activar después de la creación,
que vuelve a registrar el webhook. Este cliente lo hace automáticamente
y deja constancia en la respuesta de si se aplicó.
"""
import httpx

from app.config import settings


class N8nAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class N8nClient:
    def __init__(self):
        self.base_url = settings.n8n_url.rstrip("/") + "/api/v1"
        self.headers = {
            "X-N8N-API-KEY": settings.n8n_api_key,
            "Content-Type": "application/json",
        }

    async def create_workflow(self, workflow: dict) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/workflows", json=workflow, headers=self.headers
            )
        if response.status_code not in (200, 201):
            raise N8nAPIError(
                f"n8n rechazo la creacion del flujo: {response.text}",
                status_code=response.status_code,
            )
        return response.json()

    async def activate_workflow(self, workflow_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/workflows/{workflow_id}/activate", headers=self.headers
            )
        if response.status_code != 200:
            raise N8nAPIError(
                f"n8n rechazo la activacion del flujo {workflow_id}: {response.text}",
                status_code=response.status_code,
            )
        return response.json()

    async def deactivate_workflow(self, workflow_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{self.base_url}/workflows/{workflow_id}/deactivate", headers=self.headers
            )
        if response.status_code != 200:
            raise N8nAPIError(
                f"n8n rechazo la desactivacion del flujo {workflow_id}: {response.text}",
                status_code=response.status_code,
            )
        return response.json()

    async def deploy_workflow(self, workflow: dict) -> dict:
        """
        Crea, activa, y aplica la mitigacion del bug de registro de
        webhooks (desactivar -> reactivar). Devuelve el workflow final
        mas un flag indicando que se aplico la mitigacion.
        """
        created = await self.create_workflow(workflow)
        workflow_id = created["id"]

        await self.activate_workflow(workflow_id)

        # Mitigacion del bug n8n-io/n8n#21614: se fuerza un ciclo
        # desactivar/reactivar para forzar el re-registro del path del
        # webhook. No hay endpoint publico para *confirmar* que el
        # webhook quedo registrado, asi que esto es una mitigacion
        # razonable, no una garantia -- documentado explicitamente.
        await self.deactivate_workflow(workflow_id)
        final = await self.activate_workflow(workflow_id)

        return {
            "workflow_id": workflow_id,
            "active": final.get("active", False),
            "webhook_registration_mitigation_applied": True,
        }
