"""
Cifrado a nivel de aplicación para secretos que se guardan en Supabase vía
PostgREST (supabase-py) -- donde pgp_sym_encrypt de Postgres no es
invocable limpiamente desde una llamada REST tipo insert()/update().

Usa Fernet (cifrado simétrico autenticado, de la librería `cryptography`,
ya instalada como dependencia de pyjwt[crypto]). El texto cifrado es
seguro para guardar como columna `text` en Supabase.

Fernet se inicializa de forma perezosa: importar este módulo NO falla si
APP_ENCRYPTION_KEY está vacía (ej. en tests unitarios que no cifran nada).
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    return Fernet(settings.app_encryption_key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError(
            "No se pudo descifrar el secreto -- la APP_ENCRYPTION_KEY no coincide "
            "con la que se usó para cifrarlo (¿se rotó la llave sin migrar los datos existentes?)."
        )
