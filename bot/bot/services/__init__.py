"""
Larizinha Store — Serviços (agregador)
Centraliza TODOS os serviços internos do bot e oferece um ciclo de vida
controlado (init / shutdown / health) para uso em `bot/main.py`.

Serviços:
    - MessageEngine      → motor de mensagens editáveis
    - ButtonEngine       → motor de botões editáveis
    - LockManager        → travas de concorrência (Redis)
    - FlowState          → estados de fluxo (PIX, saque, gift)
    - Presence           → heartbeat / online
    - Throttle           → rate-limit por usuário
    - PixService         → geração/consulta de PIX (Mercado Pago)
    - ReceiptGenerator   → geração de comprovantes (spoiler)
    - CartTracker        → carrinho abandonado
    - StockAlerts        → alertas de estoque baixo/esgotado
    - ChannelPost        → auto-post no canal (compra, estoque, promoção)
    - SecurityMonitor    → antifraude
    - PermissionEngine   → permissões de admin
"""
from __future__ import annotations

from typing import Any, Dict

from bot.logger import logger


# ============================================================
# 📥 IMPORTS DE SERVIÇOS EXISTENTES
# ============================================================
# Motores centrais (já implementados)
from bot.services.message_engine import (
    MessageEngine,
    RenderedMessage,
    message_engine,
    render as render_message,
    send as send_message,
    edit as edit_message,
    invalidate_message_cache,
)
from bot.keyboards.button_engine import (
    ButtonEngine,
    ButtonDef,
    button_engine,
    render_inline,
    render_reply,
    invalidate_button_cache,
)

# Travas e estados (implementados em redis_client)
from bot.redis_client import (
    LockManager,
    LockError,
    FlowState,
    Presence,
    Throttle,
    Queue,
    Counters,
    clear_user_state,
)


# ============================================================
# 📥 IMPORTS DE SERVIÇOS ADICIONAIS (tolerantes a ausência)
# ============================================================
# Estes módulos serão criados nos próximos códigos. O import é tolerante
# para que o pacote continue funcional durante a construção incremental.
_pix_service = None
_receipt_generator = None
_cart_tracker = None
_stock_alerts = None
_channel_post = None
_security_monitor = None
_permission_engine = None

try:
    from bot.services.pix_service import (  # type: ignore
        PixService,
        pix_service,
    )

    _pix_service = pix_service
except Exception:  # pragma: no cover
    PixService = None  # type: ignore

try:
    from bot.services.receipt_generator import (  # type: ignore
        ReceiptGenerator,
        receipt_generator,
    )

    _receipt_generator = receipt_generator
except Exception:  # pragma: no cover
    ReceiptGenerator = None  # type: ignore

try:
    from bot.services.cart_tracker import (  # type: ignore
        CartTracker,
        cart_tracker,
    )

    _cart_tracker = cart_tracker
except Exception:  # pragma: no cover
    CartTracker = None  # type: ignore

try:
    from bot.services.stock_alerts import (  # type: ignore
        StockAlerts,
        stock_alerts,
    )

    _stock_alerts = stock_alerts
except Exception:  # pragma: no cover
    StockAlerts = None  # type: ignore

try:
    from bot.services.channel_post import (  # type: ignore
        ChannelPostService,
        channel_post,
    )

    _channel_post = channel_post
except Exception:  # pragma: no cover
    ChannelPostService = None  # type: ignore

try:
    from bot.services.security_monitor import (  # type: ignore
        SecurityMonitor,
        security_monitor,
    )

    _security_monitor = security_monitor
except Exception:  # pragma: no cover
    SecurityMonitor = None  # type: ignore

try:
    from bot.middlewares.admin_guard import (  # type: ignore
        PermissionEngine,
    )

    _permission_engine = PermissionEngine
except Exception:  # pragma: no cover
    PermissionEngine = None  # type: ignore


# ============================================================
# 🧠 REGISTRO CENTRAL
# ============================================================
class ServiceRegistry:
    """
    Guarda referências a todos os serviços ativos.
    Permite inicialização e shutdown centralizados.
    """

    def __init__(self) -> None:
        self._services: Dict[str, Any] = {}
        self._initialized = False

    # --------------------------------------------------------
    def register(self, name: str, instance: Any) -> None:
        if instance is None:
            return
        self._services[name] = instance

    def get(self, name: str) -> Any:
        return self._services.get(name)

    def all(self) -> Dict[str, Any]:
        return dict(self._services)

    # --------------------------------------------------------
    @property
    def initialized(self) -> bool:
        return self._initialized

    # --------------------------------------------------------
    def bind_core(self) -> None:
        """Registra os serviços que já existem (motores + Redis helpers)."""
        # Motores
        self.register("message_engine", message_engine)
        self.register("button_engine", button_engine)

        # Travas / estados / presença
        self.register("lock_manager", LockManager)
        self.register("flow_state", FlowState)
        self.register("presence", Presence)
        self.register("throttle", Throttle)
        self.register("queue", Queue)
        self.register("counters", Counters)

        # Serviços condicionais
        if _pix_service is not None:
            self.register("pix_service", _pix_service)
        if _receipt_generator is not None:
            self.register("receipt_generator", _receipt_generator)
        if _cart_tracker is not None:
            self.register("cart_tracker", _cart_tracker)
        if _stock_alerts is not None:
            self.register("stock_alerts", _stock_alerts)
        if _channel_post is not None:
            self.register("channel_post", _channel_post)
        if _security_monitor is not None:
            self.register("security_monitor", _security_monitor)
        if _permission_engine is not None:
            self.register("permission_engine", _permission_engine)

    # --------------------------------------------------------
    def bind_telegram(self, bot: Any) -> None:
        """
        Liga serviços que precisam do bot (channel_post, pix_service...).
        Chamado no boot, depois de criar o Bot.
        """
        for name in ("channel_post", "stock_alerts", "receipt_generator"):
            svc = self._services.get(name)
            if svc is None:
                continue
            attach = getattr(svc, "attach_bot", None) or getattr(
                svc, "bind_bot", None
            )
            if callable(attach):
                try:
                    attach(bot)
                except Exception as exc:
                    logger.warning(f"Falha ao ligar {name} ao bot: {exc}")


# ============================================================
# 🌐 INSTÂNCIA GLOBAL
# ============================================================
registry = ServiceRegistry()


# ============================================================
# 🚀 CICLO DE VIDA
# ============================================================
async def init_services(bot: Any = None) -> None:
    """
    Inicializa todos os serviços.
    - Vincula motores e helpers.
    - Liga serviços ao bot (se fornecido).
    """
    if registry.initialized:
        logger.debug("Serviços já inicializados.")
        return

    registry.bind_core()

    if bot is not None:
        registry.bind_telegram(bot)

    # Inicializa serviços que exigem setup assíncrono
    for name, svc in registry.all().items():
        setup = getattr(svc, "startup", None)
        if callable(setup):
            try:
                result = setup()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning(f"Startup de {name} falhou: {exc}")

    registry._initialized = True  # noqa: SLF001
    logger.info(f"Serviços inicializados: {list(registry.all().keys())}")


async def shutdown_services() -> None:
    """Encerra todos os serviços educadamente."""
    for name, svc in registry.all().items():
        shutdown = getattr(svc, "shutdown", None)
        if callable(shutdown):
            try:
                result = shutdown()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning(f"Shutdown de {name} falhou: {exc}")

    registry._services.clear()
    registry._initialized = False
    logger.info("Serviços encerrados.")


# ============================================================
# 💚 HEALTHCHECK
# ============================================================
async def services_health() -> Dict[str, bool]:
    """
    Verifica a saúde dos serviços.
    Retorna dicionário {nome: ok}.
    """
    health: Dict[str, bool] = {}
    for name, svc in registry.all().items():
        check = getattr(svc, "healthcheck", None) or getattr(svc, "ping", None)
        if not callable(check):
            health[name] = True
            continue
        try:
            result = check()
            if hasattr(result, "__await__"):
                result = await result
            health[name] = bool(result)
        except Exception:
            health[name] = False
    return health


# ============================================================
# 🧹 LIMPEZA
# ============================================================
async def cleanup_user(user_id: int) -> None:
    """Limpa estados de fluxo, views e throttles de um usuário."""
    try:
        await clear_user_state(user_id)
    except Exception as exc:
        logger.debug(f"cleanup_user falhou ({user_id}): {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    # Motores
    "MessageEngine",
    "RenderedMessage",
    "message_engine",
    "render_message",
    "send_message",
    "edit_message",
    "invalidate_message_cache",
    "ButtonEngine",
    "ButtonDef",
    "button_engine",
    "render_inline",
    "render_reply",
    "invalidate_button_cache",
    # Travas / estados / presença
    "LockManager",
    "LockError",
    "FlowState",
    "Presence",
    "Throttle",
    "Queue",
    "Counters",
    "clear_user_state",
    # Serviços condicionais
    "PixService",
    "ReceiptGenerator",
    "CartTracker",
    "StockAlerts",
    "ChannelPostService",
    "SecurityMonitor",
    "PermissionEngine",
    # Registro
    "ServiceRegistry",
    "registry",
    # Ciclo de vida
    "init_services",
    "shutdown_services",
    "services_health",
    "cleanup_user",
]
