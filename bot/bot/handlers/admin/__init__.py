"""
Larizinha Store — Pacote Admin (agregador final)
Registra TODOS os 30+ módulos do painel administrativo.

Ordem de registro:
  1. Menu (menu.py) — principal + comandos /admin
  2. Dashboard
  3. Produtos, Categorias
  4. Vendas, Usuários, Admins
  5. Carteira, Pagamentos, Saques
  6. Gifts, Cupons, Promoções
  7. Canal, Afiliados
  8. Mensagens, Botões
  9. Credenciais, Estatísticas, Configurações
  10. Blacklist, Backup, Disparos, Automações
  11. Testes, Logs, Travas
  12. Tickets, Jogos
  13. Segurança, Alertas
  14. Ferramentas, API, Permissões

Todos os routers são incluídos em um único `admin_router`,
que recebe o `AdminGuardMiddleware` (permission engine) no `main.py`.

Regras invioláveis:
- Registro TOLERANTE à ausência (se um módulo falhar, apenas ignora).
- Nenhuma mensagem/botão hardcoded aqui — só bootstrap.
- Logs claros de quais módulos foram registrados.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from aiogram import Router

from bot.logger import logger


# ============================================================
# 📥 IMPORTAÇÃO TOLERANTE DE ROUTERS
# ============================================================
def _try_import(module: str, attr: str = "router") -> Optional[Router]:
    """
    Tenta importar `attr` (Router) de `bot.handlers.admin.<module>`.
    Retorna None se não existir ou falhar.
    """
    try:
        mod = __import__(f"bot.handlers.admin.{module}", fromlist=[attr])
        router = getattr(mod, attr, None)
        if isinstance(router, Router):
            return router
        logger.debug(
            f"[admin] módulo '{module}' não expõe Router em '{attr}'."
        )
        return None
    except ModuleNotFoundError:
        return None
    except Exception as exc:
        logger.warning(f"[admin] falha ao importar '{module}': {exc}")
        return None


# ============================================================
# 📋 ORDEM DE REGISTRO DOS MÓDULOS
# ============================================================
ADMIN_MODULES: List[str] = [
    # ----- Núcleo -----
    "menu",
    "dashboard",

    # ----- Catálogo -----
    "products",
    "categories",

    # ----- Operação -----
    "sales",
    "users",
    "admins",

    # ----- Financeiro -----
    "wallet",
    "payments",
    "withdraws",

    # ----- Marketing -----
    "gifts",
    "coupons",
    "promos",
    "channel",
    "affiliates",

    # ----- Editores -----
    "messages",
    "buttons",

    # ----- Sistema -----
    "credentials",
    "stats",
    "settings",
    "blacklist",
    "backup",
    "broadcast",
    "automations",
    "tests",
    "logs",
    "locks",

    # ----- Engajamento -----
    "tickets",
    "games",

    # ----- Segurança -----
    "security",
    "alerts",

    # ----- Avançado -----
    "tools",
    "api",
    "permissions",
]


# ============================================================
# 🧩 REGISTRADOR PRINCIPAL
# ============================================================
def register_admin_routers(admin_router: Router) -> Tuple[int, int]:
    """
    Registra TODOS os módulos admin no `admin_router` fornecido.

    Retorna (registrados, ignorados).

    Uso (no main.py):
        from aiogram import Router
        from bot.handlers.admin import register_admin_routers
        from bot.middlewares import register_admin_middlewares

        admin_router = Router(name="admin_root")
        registered, ignored = register_admin_routers(admin_router)
        register_admin_middlewares(admin_router)   # aplica AdminGuard
        dp.include_router(admin_router)
    """
    registered: List[str] = []
    ignored: List[str] = []

    for name in ADMIN_MODULES:
        router = _try_import(name)
        if router is None:
            ignored.append(name)
            logger.debug(f"[admin] módulo '{name}' indisponível — ignorado.")
            continue

        try:
            admin_router.include_router(router)
            registered.append(name)
        except Exception as exc:
            logger.warning(f"[admin] falha ao incluir '{name}': {exc}")
            ignored.append(name)

    logger.info(
        f"[admin] módulos registrados: {len(registered)} "
        f"({', '.join(registered)})"
    )
    if ignored:
        logger.info(
            f"[admin] módulos ignorados: {len(ignored)} "
            f"({', '.join(ignored)})"
        )

    return len(registered), len(ignored)


# ============================================================
# 🧩 CONTAGEM SEM REGISTRO (sanity check)
# ============================================================
def list_available_modules() -> List[str]:
    """Lista os módulos que estão disponíveis para importação."""
    available: List[str] = []
    for name in ADMIN_MODULES:
        if _try_import(name) is not None:
            available.append(name)
    return available


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "register_admin_routers",
    "list_available_modules",
    "ADMIN_MODULES",
]
