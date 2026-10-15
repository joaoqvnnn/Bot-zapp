"""
Larizinha Store — Pacote externo do bot
Torna possível executar:
    python -m bot.main
    python -m bot.worker

Este arquivo fica em `bot/__init__.py` (raiz do diretório `bot/`),
NÃO confundir com `bot/bot/__init__.py` que é o pacote interno.
"""
from __future__ import annotations

# ============================================================
# ℹ️ METADADOS DO PACOTE EXTERNO
# ============================================================
__package_name__ = "larizinha-store-bot"
__package_version__ = "1.0.0"


# ============================================================
# 🛡️ PROTEÇÃO CONTRA EXECUÇÃO DIRETA
# ============================================================
# Nada pesado aqui. Toda inicialização fica em bot/bot/main.py
# e bot/bot/worker.py para evitar efeitos colaterais em import.

__all__ = [
    "__package_name__",
    "__package_version__",
]
