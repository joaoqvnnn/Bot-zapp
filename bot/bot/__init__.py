"""
Larizinha Store — Pacote raiz do bot
Expõe metadados do pacote e garante inicialização única do logging.
Não importa submódulos pesados aqui para evitar ciclos.
"""
from __future__ import annotations

from bot.config import settings as _settings


# ============================================================
# ℹ️ METADADOS
# ============================================================
__app_name__ = _settings.APP_NAME
__version__ = _settings.APP_VERSION
__env__ = _settings.ENV
__author__ = "Larizinha Store"


# ============================================================
# 📦 API PÚBLICA (leve)
# ============================================================
__all__ = [
    "__app_name__",
    "__version__",
    "__env__",
    "__author__",
]
