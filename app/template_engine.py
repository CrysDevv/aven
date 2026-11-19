"""
Motor de plantillas: carga manifest.json + workflow.json de una carpeta,
sustituye variables {{clave}} de forma segura (escapadas para JSON), y
sanitiza nodos peligrosos antes de permitir el despliegue.

Convencion de carpeta (una por plantilla, dentro de settings.templates_dir):
    templates/
        01-atencion-catalogo/
            manifest.json   # metadata + variables que expone al usuario
            workflow.json   # el flujo real exportado de n8n, con
                             # placeholders {{variable}} en los valores
                             # de texto que deben ser configurables.
"""
import json
import re
from pathlib import Path

from app.config import settings

# Nodos bloqueados por riesgo de RCE / acceso a filesystem del host.
# Complementa (no reemplaza) la variable de entorno NODES_EXCLUDE de la
# instancia de n8n -- esta es una segunda capa, a nivel de aplicacion,
# que corre ANTES de que el JSON llegue siquiera a n8n.
DANGEROUS_NODE_TYPES = {
    "n8n-nodes-base.executeCommand",
    "n8n-nodes-base.readWriteFile",
    "n8n-nodes-base.ssh",
}

PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")


class TemplateError(Exception):
    pass


class TemplateSanitizationError(TemplateError):
    pass


def _templates_root() -> Path:
    return Path(settings.templates_dir)


def list_templates() -> list[dict]:
    """Lista los manifests de todas las plantillas disponibles (sin el workflow completo)."""
    root = _templates_root()
    if not root.exists():
        return []

    manifests = []
    for folder in sorted(root.iterdir()):
        manifest_path = folder / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path, encoding="utf-8") as f:
                manifests.append(json.load(f))
    return manifests


def get_manifest(template_id: str) -> dict:
    manifest_path = _templates_root() / template_id / "manifest.json"
    if not manifest_path.exists():
        raise TemplateError(f"Plantilla '{template_id}' no encontrada")
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def _escape_for_json_string(value: str) -> str:
    """
    Escapa un valor para insertarlo de forma segura DENTRO de un valor de
    string de JSON ya existente (el placeholder vive dentro de comillas
    en workflow.json). Sin esto, un nombre de negocio con una comilla o
    un salto de linea rompe la estructura del JSON o -- peor -- permite
    inyectar contenido fuera del string original.
    """
    # json.dumps ya hace el escapado correcto; se le quitan las comillas
    # externas porque el placeholder ya esta dentro de un string existente.
    return json.dumps(value)[1:-1]


def render_workflow(
    template_id: str,
    variables: dict[str, str],
    system_variables: dict[str, str] | None = None,
) -> dict:
    """
    Sustituye los placeholders {{clave}} en workflow.json con los valores
    provistos, validando que todas las variables requeridas del manifest
    esten presentes, y sanitiza nodos peligrosos antes de devolver el
    dict final listo para enviar a la API de n8n.

    `system_variables` son valores que el propio backend inyecta desde el
    contexto de despliegue (tenant_id, phone_number_id, buffer_workflow_id)
    -- NUNCA declarados en el manifest, y NUNCA provistos por el usuario.
    Se fusionan DESPUES de validar las variables de usuario, así que un
    valor de sistema no puede ser sobreescrito por el body de la request.
    """
    manifest = get_manifest(template_id)
    workflow_path = _templates_root() / template_id / "workflow.json"
    if not workflow_path.exists():
        raise TemplateError(f"workflow.json no encontrado para '{template_id}'")

    # Validar variables requeridas ANTES de tocar el JSON
    declared_keys = {v["key"] for v in manifest.get("variables", [])}
    required_keys = {v["key"] for v in manifest.get("variables", []) if v.get("required")}
    missing = required_keys - set(variables.keys())
    if missing:
        raise TemplateError(f"Faltan variables requeridas: {', '.join(sorted(missing))}")

    unknown = set(variables.keys()) - declared_keys
    if unknown:
        raise TemplateError(f"Variables no declaradas en el manifest: {', '.join(sorted(unknown))}")

    # Las variables de sistema se fusionan AL FINAL -- si por error alguna
    # plantilla declarara "tenant_id" como variable de usuario, el valor
    # real del sistema prevalece siempre.
    all_variables = {**variables, **(system_variables or {})}

    with open(workflow_path, encoding="utf-8") as f:
        raw_text = f.read()

    # Defensa adicional: cualquier placeholder presente en workflow.json que
    # no este declarado NI en el manifest NI como variable de sistema es un
    # ERROR explicito de la plantilla (bug del desarrollador de la plantilla,
    # no del usuario que la despliega) -- por ejemplo, usar {{calendar_id}}
    # en el JSON sin declararlo en manifest.json. Distinto de una variable
    # declarada pero opcional que el usuario simplemente no completo esta
    # vez -- eso SI se sustituye por cadena vacia, sin error.
    all_declared_keys = declared_keys | set((system_variables or {}).keys())
    found_placeholders = set(PLACEHOLDER_PATTERN.findall(raw_text))
    uncovered = found_placeholders - all_declared_keys
    if uncovered:
        raise TemplateError(
            f"La plantilla '{template_id}' referencia variables sin declarar en su manifest "
            f"ni provistas como variables de sistema: {', '.join(sorted(uncovered))}. "
            "Esto es un error de la plantilla, no del usuario que la despliega."
        )

    def replace(match: re.Match) -> str:
        key = match.group(1)
        # Declarada (o de sistema) pero no provista esta vez -> vacio, valido
        return _escape_for_json_string(str(all_variables.get(key, "")))

    substituted_text = PLACEHOLDER_PATTERN.sub(replace, raw_text)

    try:
        workflow = json.loads(substituted_text)
    except json.JSONDecodeError as e:
        # Si esto ocurre, es un bug en la plantilla (placeholder fuera de
        # un string JSON valido), no un problema del usuario -- se
        # reporta como error de servidor, no se le pasa la culpa al tenant.
        raise TemplateError(f"La plantilla '{template_id}' quedo con JSON invalido tras sustituir variables: {e}")

    _sanitize_workflow(workflow)
    return workflow


def _sanitize_workflow(workflow: dict) -> None:
    nodes = workflow.get("nodes", [])
    for node in nodes:
        node_type = node.get("type", "")
        if node_type in DANGEROUS_NODE_TYPES:
            raise TemplateSanitizationError(
                f"El nodo '{node.get('name', '?')}' usa el tipo bloqueado '{node_type}'. "
                "No se permite desplegar flujos con nodos de ejecucion de comandos o "
                "acceso a archivos del sistema."
            )

    # settings por defecto si la plantilla no los trae -- evita depender
    # del comportamiento por defecto (potencialmente distinto) de n8n
    workflow.setdefault("settings", {})
    workflow["settings"].setdefault("executionOrder", "v1")
