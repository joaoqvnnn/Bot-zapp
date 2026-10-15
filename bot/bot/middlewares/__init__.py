"""
Larizinha Store — Middlewares (agregador)
Inicializa e registra TODOS os middlewares na ORDEM CORRETA.

Ordem de execução (outer → inner):
1. ThrottleMiddleware         → bloqueia flood (mais externo, roda antes de tudo)
2. UserLoaderMiddleware       → cria/carrega usuário + heartbeat
3. ChannelGuardMiddleware     → verifica canal/grupo obrigatório
4. (handlers executam)

Para o router administrativo:
- AdminGuardMiddleware        → registrado no próprio router admin

ChatMemberUpdated:
- ChannelWatcherMiddleware    → registrado em dp.chat_member
"""
from __future__ import annotations

from typing import Optional

from aiogram import Dispatcher, Router

from bot.logger import logger

# --- Importa todos os middlewares ---
from bot.middlewares.throttle import (
    ThrottleMiddleware,
    throttle_callback,
    throttle_message,
)
from bot.middlewares.user_loader import (
    UserLoaderMiddleware,
    user_loader,
)
from bot.middlewares.channel_guard import (
    ChannelGuardMiddleware,
    ChannelWatcherMiddleware,
    channel_router,
)
from bot.middlewares.admin_guard import (
    AdminGuardMiddleware,
    PermissionEngine,
    register_admin_guard,
    require_admin,
    require_permission,
)


# ============================================================
# 🧩 REGISTRO GLOBAL (message + callback_query)
# ============================================================
def register_global_middlewares(dp: Dispatcher) -> None:
    """
    Registra os middlewares GLOBAIS no Dispatcher.

    Ordem (outer → inner):
      1. Throttle
      2. UserLoader
      3. ChannelGuard

    Todos são OUTER para garantir que rodem ANTES dos handlers.
    """
    # ------------------------------
    # 1) THROTTLE (anti-flood)
    # ------------------------------
    dp.message.outer_middleware(throttle_message)
    dp.callback_query.outer_middleware(throttle_callback)

    # ------------------------------
    # 2) USER LOADER (cria/carrega user + heartbeat)
    # ------------------------------
    dp.message.outer_middleware(user_loader)
    dp.callback_query.outer_middleware(user_loader)

    # ------------------------------
    # 3) CHANNEL GUARD (canal/grupo obrigatório)
    # ------------------------------
    guard = ChannelGuardMiddleware()
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)

    # ------------------------------
    # 4) CHAT MEMBER WATCHER (auto desbloqueio)
    # ------------------------------
    watcher = ChannelWatcherMiddleware()
    dp.chat_member.outer_middleware(watcher)

    logger.info("Middlewares globais registrados (throttle → user_loader → channel_guard).")


# ============================================================
# 🧩 REGISTRO DE ROUTERS DE MIDDLEWARE
# ============================================================
def register_middleware_routers(dp: Dispatcher) -> None:
    """
    Registra routers internos de middleware (ex.: botão ✅ Verificar
    do channel_guard). Devem ser registrados DEPOIS dos routers comuns
    para não roubar prioridade indevida, mas ANTES do fallback.
    """
    dp.include_router(channel_router)
    logger.info("Router interno do channel_guard registrado.")


# ============================================================
# 🧩 REGISTRO ADMIN (aplicado no router admin)
# ============================================================
def register_admin_middlewares(admin_router: Router) -> None:
    """
    Registra o AdminGuardMiddleware no router administrativo.
    Deve ser chamado com o Router do painel admin (não o Dispatcher).
    """
    register_admin_guard(admin_router)


# ============================================================
# 🧩 REGISTRO COMPLETO (chamado pelo main.py)
# ============================================================
def setup_middlewares(
    dp: Dispatcher,
    admin_router: Optional[Router] = None,
) -> None:
    """
    Faz o setup completo:
      1. Middlewares globais no Dispatcher.
      2. Routers internos de middleware.
      3. (Opcional) Middleware de admin no router admin.

    Uso em `bot/main.py`:
        from bot.middlewares import setup_middlewares
        setup_middlewares(dp, admin_router=admin_router)
    """
    register_global_middlewares(dp)
    register_middleware_routers(dp)

    if admin_router is not None:
        register_admin_middlewares(admin_router)

    logger.info("Setup de middlewares concluído.")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    # Instâncias
    "throttle_message",
    "throttle_callback",
    "user_loader",
    # Classes
    "ThrottleMiddleware",
    "UserLoaderMiddleware",
    "ChannelGuardMiddleware",
    "ChannelWatcherMiddleware",
    "AdminGuardMiddleware",
    "PermissionEngine",
    # Routers
    "channel_router",
    # Helpers
    "require_admin",
    "require_permission",
    # Funções de registro
    "register_global_middlewares",
    "register_middleware_routers",
    "register_admin_middlewares",
    "setup_middlewares",
]
