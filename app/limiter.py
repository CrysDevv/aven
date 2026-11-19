"""
Instancia UNICA y compartida de Limiter. Si cada router creara su propia
instancia de Limiter, cada una llevaria su propio contador -- los limites
no se aplicarian de forma coherente. Este modulo existe solo para que
main.py y los routers importen SIEMPRE la misma instancia.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
