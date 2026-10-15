"""
Larizinha Store — Handlers (agregador)
Registra TODOS os routers do fluxo do membro + painel admin.

Ordem de registro (importa para prioridade de matching):
  1. commands      → /start /pix /historico /alertas /afiliados /id /saldo /ranking /termos
  2. start         → bloqueio de canal + boas-vindas
  3. shop          → WebApp (abrir loja)
  4. catalog       → catálogo + detalhe do produto + compra
  5. profile       → perfil + histórico + gift + alterar dados
  6. recharge      → recarga de saldo + PIX
  7. affiliates    → programa de afiliados
  8. withdraw      → saque (chave → confirmação → editar → senha → sucesso)
  9. ranking       → top compradores
 10. alerts        → /alertas (notificações de estoque)
 11. support       → atendimento + sobre o bot
 12. search        → pesquisar serviços
 13. tickets       → tickets e recompensas
 14. games         → jogos (funcionalidades interativas)
 15. admin         → painel administrativo (30 módulos)

Todos os módulos são TOLERANTES à ausência — se um arquivo ainda não
existir, o agregador apenas ignora e segue em frente.
"""
from __future__ import annotations

from typing import List, Optional

from aiogram import Dispatcher, Router

from bot.logger import logger


# ============================================================
# 🧩 IMPORTAÇÃO TOLERANTE DE ROUTERS
# ============================================================
def _try_import(module: str, attr: str = "router") -> Optional[Router]:
    """
    Tenta importar `attr` (Router) de `bot.handlers.<module>`.
    Retorna None se não existir ainda.
    """
    try:
        mod = __import__(f"bot.handlers.{module}", fromlist=[attr])
        router = getattr(mod, attr, None)
        if isinstance(router, Router):
            return router
        # Se o módulo não define `router`, mas define por padrão em `__all__`
        logger.debug(f"bot.handlers.{module} não expõe '{attr}' como Router.")
        return None
    except ModuleNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"Falha ao importar bot.handlers.{module}: {exc}")
        return None


# ============================================================
# 📋 ORDEM DE REGISTRO (fluxo do membro)
# ============================================================
MEMBER_MODULES: List[str] = [
    "commands",
    "start",
    "shop",
    "catalog",
    "profile",
    "recharge",
    "affiliates",
    "withdraw",
    "ranking",
    "alerts",
    "support",
    "search",
    "tickets",
    "games",
]


# ============================================================
# 📋 MÓDULOS ADMIN (registrados em router próprio)
# ============================================================
ADMIN_MODULES: List[str] = [
    "admin",
]


# ============================================================
# 🧩 REGISTRO DOS ROUTERS DO MEMBRO
# ============================================================
def register_member_routers(dp: Dispatcher) -> List[str]:
    """
    Registra todos os routers do fluxo do membro no Dispatcher.
    Retorna a lista de módulos efetivamente registrados.
    """
    registered: List[str] = []
    for name in MEMBER_MODULES:
        router = _try_import(name)
        if router is None:
            logger.debug(f"[handlers] módulo '{name}' ainda não disponível — ignorado.")
            continue
        dp.include_router(router)
        registered.append(name)

    logger.info(f"[handlers] routers do membro registrados: {registered}")
    return registered


# ============================================================
# 🧩 REGISTRO DOS ROUTERS ADMIN
# ============================================================
def register_admin_routers(admin_router: Router) -> List[str]:
    """
    Registra os routers admin DENTRO de um Router próprio.
    (O middleware AdminGuard é aplicado nesse router no main.py.)
    """
    registered: List[str] = []
    for name in ADMIN_MODULES:
        router = _try_import(name)
        if router is None:
            logger.debug(f"[handlers] módulo admin '{name}' ainda não disponível — ignorado.")
            continue
        admin_router.include_router(router)
        registered.append(name)

    logger.info(f"[handlers] routers admin registrados: {registered}")
    return registered


# ============================================================
# 🧩 REGISTRO COMPLETO (chamado pelo main.py)
# ============================================================
def register_all_routers(
    dp: Dispatcher,
    admin_router: Optional[Router] = None,
) -> None:
    """
    Registra tudo:
      - routers do membro no Dispatcher
      - routers admin no admin_router (se fornecido)

    Uso em `bot/main.py`:
        from aiogram import Router
        from bot.handlers import register_all_routers

        admin_router = Router(name="admin")
        register_all_routers(dp, admin_router=admin_router)
        dp.include_router(admin_router)  # após o admin_guard
    """
    register_member_routers(dp)
    if admin_router is not None:
        register_admin_routers(admin_router)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "MEMBER_MODULES",
    "ADMIN_MODULES",
    "register_member_routers",
    "register_admin_routers",
    "register_all_routers",
]
