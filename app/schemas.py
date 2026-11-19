"""
Schemas de entrada/salida. Nunca se exponen modelos de base de datos
directamente — ver Anexo de Seguridad, API3:2023.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MetaMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")  # ignora campos no esperados de Meta, no falla por ellos

    id: str  # wamid — usado para deduplicación
    from_: str | None = None
    timestamp: str | None = None
    type: str | None = None


class MetaChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str
    value: dict


class MetaEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    changes: list[MetaChange]


class MetaWebhookPayload(BaseModel):
    """
    Valida la ESTRUCTURA del payload de Meta — la firma HMAC ya validó que
    viene de Meta, pero esto valida que el CONTENIDO tiene la forma esperada
    antes de que el resto del sistema confíe en él (API10:2023).
    """
    model_config = ConfigDict(extra="ignore")

    object: Literal["whatsapp_business_account"]
    entry: list[MetaEntry]


class WebhookAckResponse(BaseModel):
    status: Literal["received", "duplicate", "ignored"]
