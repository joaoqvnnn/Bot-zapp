"""
Larizinha Store — Bootstrap FINAL do Bot
Inicializa TUDO na ordem correta:

  1. Configuração e logging
  2. Bot (Telegram) + Dispatcher (Aiogram 3.x) + Storage (Redis)
  3. Middlewares globais (throttle → user_loader → channel_guard)
  4. Routers do membro (14 handlers)
  5. Router do painel admin (com AdminGuardMiddleware / Permission Engine)
  6. Ciclo de vida:
       - Startup: init_db, init_services, seed (opcional), set_commands, webhook
       - Shutdown: close_redis, close_db, shutdown_services, session.close
  7. Modo de execução:
       - RENDER=true / TELEGRAM_WEBHOOK_URL  → WEBHOOK (aiohttp)
       - Caso contrário                     → POLLING (dev/local)
  8. Healthcheck exposto em /health e /

Regras invioláveis:
- NADA de mensagem/botão hardcoded aqui — só bootstrap.
- Registro automático e tolerante à ausência de módulos.
- Webhook com secret_token validado.
- Seed automático controlado por RUN_SEED_ON_BOOT (.env).
"""
from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
from aiohttp import web

from bot.config import settings
from bot.database import (
    close_db,
    healthcheck as db_healthcheck,
    init_db,
)
from bot.logger import logger, setup_logger
from bot.redis_client import (
    close_redis,
    ping as redis_ping,
)
from bot.services import (
    init_services,
    services_health,
    shutdown_services,
)

# ---------- Middlewares ----------
from bot.middlewares import setup_middlewares
from bot.middlewares.admin_guard import AdminGuardMiddleware

# ---------- Routers ----------
from bot.handlers import register_all_routers
from bot.handlers.admin import register_admin_routers


# ============================================================
# 🌐 SINGLETONS
# ============================================================
_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_webhook_runner: Optional[web.AppRunner] = None
_shutdown_event = asyncio.Event()


# ============================================================
# 🤖 BOT / DISPATCHER
# ============================================================
def build_bot() -> Bot:
    """Constrói o Bot com defaults seguros."""
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
            allow_sending_without_reply=True,
        ),
    )


def build_storage() -> RedisStorage:
    """Storage Redis para FSM."""
    return RedisStorage.from_url(
        settings.REDIS_URL,
        key_builder=None,  # fsm:{chat_id}:{user_id}:{bot_id}
        connection_kwargs={
            "decode_responses": False,
        },
    )


def build_dispatcher() -> Dispatcher:
    """Dispatcher com storage Redis + dependências no contexto."""
    storage = build_storage()
    dp = Dispatcher(storage=storage)

    # Injeta dependências no contexto de todos os handlers
    dp["settings"] = settings
    dp["app_name"] = settings.APP_NAME
    dp["app_version"] = settings.APP_VERSION

    return dp


# ============================================================
# 📌 COMANDOS DEFAULT
# ============================================================
async def set_default_commands(bot: Bot) -> None:
    """Registra a lista de comandos exibida no menu do Telegram."""
    commands = [
        BotCommand(command="start", description="🚀 Iniciar o bot"),
        BotCommand(command="pix", description="💠 Gerar PIX de recarga"),
        BotCommand(command="saldo", description="💰 Ver meu saldo"),
        BotCommand(command="historico", description="📜 Meu histórico de compras"),
        BotCommand(command="alertas", description="⚠️ Notificações de estoque"),
        BotCommand(command="afiliados", description="👥 Programa de afiliados"),
        BotCommand(command="ranking", description="🏆 Ranking do bot"),
        BotCommand(command="id", description="🆔 Meu ID no Telegram"),
        BotCommand(command="termos", description="📜 Termos de uso"),
        BotCommand(command="suporte", description="✉️ Atendimento"),
        BotCommand(command="sobre", description="🤖 Sobre o bot"),
        BotCommand(command="cancelar", description="❌ Cancelar operação"),
    ]
    try:
        await bot.set_my_commands(
            commands=commands,
            scope=BotCommandScopeDefault(),
        )
        logger.info(f"Comandos default registrados ({len(commands)}).")
    except Exception as exc:
        logger.warning(f"Falha ao registrar comandos default: {exc}")


# ============================================================
# 🌱 SEED AUTOMÁTICO
# ============================================================
async def run_seed_if_enabled() -> None:
    """
    Roda o seed se RUN_SEED_ON_BOOT=true e o banco ainda não foi populado.
    """
    enabled = str(getattr(settings, "RUN_SEED_ON_BOOT", "true")).lower() == "true"
    if not enabled:
        logger.info("Seed automático desativado (RUN_SEED_ON_BOOT=false).")
        return

    try:
        from bot.seed import (
            _seed_admins,
            _seed_roles,
            _seed_categories,
            _seed_messages,
            _seed_buttons,
            _seed_settings,
            _seed_automations,
        )

        logger.info("🌱 Verificando seed inicial do banco...")

        a = await _seed_admins()
        r = await _seed_roles()
        c = await _seed_categories()
        m = await _seed_messages()
        b = await _seed_buttons()
        s = await _seed_settings()
        au = await _seed_automations()

        total = a + r + c + m + b + s + au
        if total == 0:
            logger.info("🌱 Banco já populado — nada novo.")
        else:
            logger.info(
                f"🌱 Seed aplicado: admins={a} roles={r} categorias={c} "
                f"mensagens={m} botões={b} settings={s} automações={au}"
            )
    except Exception as exc:
        logger.exception(f"⚠️  Falha no seed automático: {exc}")
        # Não bloqueia o boot


# ============================================================
# 🚀 STARTUP / SHUTDOWN
# ============================================================
async def on_startup(bot: Bot) -> None:
    """Executado antes de começar a receber updates."""
    logger.info(
        f"🚀 Iniciando {settings.APP_NAME} v{settings.APP_VERSION} "
        f"(env={settings.ENV})"
    )

    # 1) Database
    try:
        await init_db()
        ok = await db_healthcheck()
        logger.info(f"🗄️  Banco: {'OK' if ok else 'FALHOU'}")
    except Exception as exc:
        logger.exception(f"Erro no init_db: {exc}")

    # 2) Redis
    try:
        ok = await redis_ping()
        logger.info(f"⚡ Redis: {'OK' if ok else 'FALHOU'}")
    except Exception as exc:
        logger.exception(f"Erro no ping do Redis: {exc}")

    # 3) Seed inicial (aditivo)
    await run_seed_if_enabled()

    # 4) Serviços internos
    try:
        await init_services(bot)
        health = await services_health()
        logger.info(f"🧩 Serviços: {health}")
    except Exception as exc:
        logger.exception(f"Erro no init_services: {exc}")

    # 5) Comandos default
    await set_default_commands(bot)

    # 6) Webhook (produção)
    if settings.RENDER or settings.TELEGRAM_WEBHOOK_URL:
        await _setup_webhook(bot)
    else:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("Webhook removido — rodando em modo polling.")
        except Exception as exc:
            logger.debug(f"delete_webhook falhou: {exc}")

    # 7) Info do bot
    try:
        me = await bot.get_me()
        logger.info(
            f"🤖 Bot: @{me.username} (id={me.id}) — "
            f"{'webhook' if (settings.RENDER or settings.TELEGRAM_WEBHOOK_URL) else 'polling'}"
        )
    except Exception as exc:
        logger.warning(f"get_me falhou: {exc}")


async def on_shutdown(bot: Bot) -> None:
    """Executado ao desligar (SIGTERM/SIGINT)."""
    logger.info("🛑 Encerrando bot...")

    try:
        await shutdown_services()
    except Exception as exc:
        logger.debug(f"shutdown_services falhou: {exc}")

    try:
        await close_redis()
    except Exception as exc:
        logger.debug(f"close_redis falhou: {exc}")

    try:
        await close_db()
    except Exception as exc:
        logger.debug(f"close_db falhou: {exc}")

    try:
        await bot.session.close()
    except Exception as exc:
        logger.debug(f"session.close falhou: {exc}")

    logger.info("✅ Bot encerrado com sucesso.")


# ============================================================
# 🌐 WEBHOOK
# ============================================================
async def _setup_webhook(bot: Bot) -> None:
    """Configura o webhook no Telegram."""
    webhook_url = settings.TELEGRAM_WEBHOOK_URL
    if not webhook_url:
        if settings.RENDER_EXTERNAL_URL:
            webhook_url = (
                f"{settings.RENDER_EXTERNAL_URL.rstrip('/')}/webhook/telegram"
            )
        else:
            logger.warning("WEBHOOK URL não configurada — webhook ignorado.")
            return

    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.WEBHOOK_SECRET_TOKEN or None,
            drop_pending_updates=False,
            allowed_updates=[
                "message",
                "callback_query",
                "chat_member",
                "my_chat_member",
                "inline_query",
                "pre_checkout_query",
            ],
        )
        logger.info(f"🪝 Webhook registrado: {webhook_url}")
    except Exception as exc:
        logger.exception(f"Falha ao registrar webhook: {exc}")


async def _webhook_health(_: web.Request) -> web.Response:
    """Endpoint /health para o Render."""
    try:
        db_ok = await db_healthcheck()
    except Exception:
        db_ok = False

    try:
        rd_ok = await redis_ping()
    except Exception:
        rd_ok = False

    try:
        svc = await services_health()
    except Exception:
        svc = {}

    status = "ok" if (db_ok and rd_ok) else "degraded"
    http_status = 200 if status == "ok" else 503

    return web.json_response(
        {
            "status": status,
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.ENV,
            "db": db_ok,
            "redis": rd_ok,
            "services": svc,
        },
        status=http_status,
    )


async def start_webhook_server(bot: Bot, dp: Dispatcher) -> None:
    """Sobe o servidor aiohttp para receber webhooks."""
    global _webhook_runner

    app = web.Application()

    # Healthcheck
    app.router.add_get("/health", _webhook_health)
    app.router.add_get("/", _webhook_health)

    # Handler do Telegram
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.WEBHOOK_SECRET_TOKEN or None,
    )
    handler.register(app, path="/webhook/telegram")

    # Anexa ao app (resolve lifecycle)
    setup_application(app, dp, bot=bot)

    # Sobe
    runner = web.AppRunner(app)
    await runner.setup()
    _webhook_runner = runner

    site = web.TCPSite(
        runner,
        host=settings.WEBHOOK_HOST,
        port=settings.WEBHOOK_PORT,
    )
    await site.start()

    logger.info(
        f"🌐 Webhook ouvindo em "
        f"http://{settings.WEBHOOK_HOST}:{settings.WEBHOOK_PORT}"
    )


async def stop_webhook_server() -> None:
    """Encerra o servidor webhook."""
    global _webhook_runner
    if _webhook_runner is not None:
        try:
            await _webhook_runner.cleanup()
        except Exception as exc:
            logger.debug(f"cleanup webhook falhou: {exc}")
        _webhook_runner = None


# ============================================================
# 🧩 REGISTRO DE HANDLERS
# ============================================================
def register_handlers(dp: Dispatcher) -> None:
    """
    Registra:
      1. Routers do membro (via register_all_routers)
      2. Router admin dedicado (com AdminGuardMiddleware)
      3. Middlewares globais (throttle → user_loader → channel_guard)
    """
    # ---------- Router admin dedicado ----------
    admin_router = Router(name="admin_root")

    # ---------- Routers do membro + admin (via agregador) ----------
    register_all_routers(dp, admin_router=admin_router)

    # ---------- Routers admin (34 módulos) ----------
    try:
        registered, ignored = register_admin_routers(admin_router)
        logger.info(
            f"[admin] {registered} módulos registrados, {ignored} ignorados."
        )
    except Exception as exc:
        logger.exception(f"Falha ao registrar routers admin: {exc}")

    # ---------- Middlewares globais ----------
    setup_middlewares(dp, admin_router=admin_router)

    # ---------- AdminGuard (permission engine) ----------
    try:
        guard = AdminGuardMiddleware()
        admin_router.message.middleware(guard)
        admin_router.callback_query.middleware(guard)
        logger.info("[admin] AdminGuardMiddleware aplicado.")
    except Exception as exc:
        logger.exception(f"Falha ao aplicar AdminGuard: {exc}")

    # ---------- Inclui o router admin no dispatcher ----------
    # IMPORTANTE: incluir por ÚLTIMO para que os handlers do membro
    # tenham prioridade quando os callbacks colidirem (não há colisão
    # porque usamos prefixo "admin:", mas por segurança).
    dp.include_router(admin_router)

    logger.info("Handlers e middlewares registrados.")


# ============================================================
# 🔁 POLLING
# ============================================================
async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Executa o bot em modo polling (dev/local)."""
    logger.info("📡 Rodando em modo POLLING...")
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "chat_member",
            "my_chat_member",
            "inline_query",
            "pre_checkout_query",
        ],
        handle_signals=False,
    )


# ============================================================
# 🎯 MAIN
# ============================================================
async def amain() -> None:
    """Ponto de entrada assíncrono."""
    global _bot, _dp

    # ---------- Logging ----------
    setup_logger()

    # ---------- Bot + Dispatcher ----------
    _bot = build_bot()
    _dp = build_dispatcher()

    # ---------- Handlers ----------
    register_handlers(_dp)

    # ---------- Startup ----------
    await on_startup(_bot)

    try:
        # ---------- Modo de execução ----------
        if settings.RENDER or settings.TELEGRAM_WEBHOOK_URL:
            # WEBHOOK
            await start_webhook_server(_bot, _dp)

            loop = asyncio.get_running_loop()

            def _ask_shutdown() -> None:
                _shutdown_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _ask_shutdown)
                except NotImplementedError:
                    pass

            await _shutdown_event.wait()
            await stop_webhook_server()
        else:
            # POLLING
            loop = asyncio.get_running_loop()

            def _ask_shutdown() -> None:
                _shutdown_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _ask_shutdown)
                except NotImplementedError:
                    pass

            polling_task = asyncio.create_task(run_polling(_bot, _dp))
            shutdown_task = asyncio.create_task(_shutdown_event.wait())

            done, pending = await asyncio.wait(
                {polling_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
    finally:
        try:
            await on_shutdown(_bot)
        except Exception as exc:
            logger.debug(f"on_shutdown falhou: {exc}")


def main() -> None:
    """Wrapper síncrono."""
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário (Ctrl+C).")
    except Exception as exc:
        logger.exception(f"Erro fatal no main: {exc}")
        sys.exit(1)


# ============================================================
# 🚀 ENTRYPOINT
# ============================================================
if __name__ == "__main__":
    main()


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "build_bot",
    "build_dispatcher",
    "build_storage",
    "register_handlers",
    "set_default_commands",
    "run_seed_if_enabled",
    "on_startup",
    "on_shutdown",
    "start_webhook_server",
    "stop_webhook_server",
    "run_polling",
    "amain",
    "main",
]
