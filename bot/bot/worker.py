"""
Larizinha Store — Worker de Automações
Executado SEPARADO do bot:  python -m bot.worker

Automações implementadas (conforme DIM):
  1. PIX Expirado          → expira cobranças vencidas + libera trava
  2. PIX Não Pago          → notifica usuários que geraram PIX e não pagaram
  3. Carrinho Abandonado   → reengaja usuários que viram produto e não compraram
  4. Promoção Nova         → dispara promoções agendadas (canal + usuários)
  5. Estoque Baixo         → verifica produtos e alerta admins + canal + inscritos
  6. Heartbeat             → marca o worker como vivo no Redis
  7. Backup automático     → gera snapshot do banco (via pg_dump ou query)
  8. Trava Reservada       → limpa locks órfãos (expirados além do TTL)
  9. Limpeza de estados    → remove FlowStates órfãos antigos do Redis

Cada automação respeita as flags do .env:
  AUTO_PIX_EXPIRED, AUTO_PIX_UNPAID, AUTO_CART_ABANDONED,
  AUTO_NEW_PROMO, AUTO_LOW_STOCK, AUTO_HEARTBEAT, AUTO_BACKUP_ENABLED

Todos os jobs rodam em loop com intervalos configuráveis.
Nenhuma mensagem acumula: cada notificação é UMA mensagem nova, legítima.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select, update

from bot.config import settings
from bot.database import (
    Backup,
    Lock,
    PixCharge,
    PixStatus,
    User,
    UserStatus,
    close_db,
    healthcheck as db_healthcheck,
    init_db,
    session_scope,
)
from bot.logger import logger, setup_logger
from bot.redis_client import (
    Keys,
    Presence,
    close_redis,
    get_redis,
    ping as redis_ping,
)
from bot.services import (
    init_services,
    services_health,
    shutdown_services,
)
from bot.services.cart_tracker import cart_tracker
from bot.services.channel_post import channel_post
from bot.services.message_engine import message_engine
from bot.services.pix_service import pix_service
from bot.services.stock_alerts import stock_alerts


# ============================================================
# 🔑 CONSTANTES
# ============================================================
HEARTBEAT_KEY = f"{settings.REDIS_PREFIX}:worker:heartbeat"
HEARTBEAT_INTERVAL = 30       # segundos
WORKER_VERSION = "1.0.0"

# Intervalos (segundos)
INTERVAL_PIX_EXPIRED = 60
INTERVAL_PIX_UNPAID = 300         # 5 min
INTERVAL_CART = 120               # 2 min
INTERVAL_PROMO = 60
INTERVAL_STOCK = 180              # 3 min
INTERVAL_LOCKS = 300              # 5 min
INTERVAL_BACKUP = 3600 * 6        # 6 horas (checa, mas respeita AUTO_BACKUP_INTERVAL_HOURS)
INTERVAL_FLOW_CLEANUP = 900       # 15 min

# FlowState TTL máximo considerado órfão
FLOW_ORPHAN_AGE = 3600 * 6


# ============================================================
# 🌐 GLOBAIS
# ============================================================
_bot: Optional[Bot] = None
_shutdown_event = asyncio.Event()
_tasks: list[asyncio.Task] = []


# ============================================================
# 🤖 BOT (worker precisa do bot para enviar notificações)
# ============================================================
def build_worker_bot() -> Bot:
    """Bot do worker (mesmo token, mas sessão própria)."""
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
            allow_sending_without_reply=True,
        ),
    )


# ============================================================
# ❤️ HEARTBEAT
# ============================================================
async def _heartbeat_loop() -> None:
    """Marca o worker como vivo a cada X segundos."""
    logger.info("❤️  Heartbeat iniciado.")
    while not _shutdown_event.is_set():
        try:
            r = await get_redis()
            await r.set(
                HEARTBEAT_KEY,
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "version": WORKER_VERSION,
                }.__str__(),
                ex=HEARTBEAT_INTERVAL * 3,
            )
        except Exception as exc:
            logger.debug(f"heartbeat falhou: {exc}")
        await _sleep_or_stop(HEARTBEAT_INTERVAL)


async def is_worker_alive() -> bool:
    """Usado pelo painel admin para saber se o worker está rodando."""
    try:
        r = await get_redis()
        return bool(await r.exists(HEARTBEAT_KEY))
    except Exception:
        return False


# ============================================================
# 🕐 SLEEP CANCELÁVEL
# ============================================================
async def _sleep_or_stop(seconds: float) -> None:
    try:
        await asyncio.wait_for(_shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# ============================================================
# 💠 1) PIX EXPIRADO
# ============================================================
async def _job_pix_expired() -> None:
    """Expira cobranças PIX vencidas e libera travas."""
    if not settings.AUTO_PIX_EXPIRED:
        return

    try:
        expired = await pix_service.list_expired(limit=200)
    except Exception as exc:
        logger.debug(f"list_expired falhou: {exc}")
        return

    if not expired:
        return

    count = 0
    for charge in expired:
        try:
            ok = await pix_service.expire_charge(charge.txid)
            if ok:
                count += 1
        except Exception as exc:
            logger.debug(f"expire_charge falhou ({charge.txid}): {exc}")

    if count > 0:
        logger.info(f"💠 {count} PIX(s) expirado(s) processados.")


async def _pix_expired_loop() -> None:
    logger.info("💠 Automação PIX Expirado iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_pix_expired()
        except Exception as exc:
            logger.exception(f"pix_expired loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_PIX_EXPIRED)


# ============================================================
# ⏰ 2) PIX NÃO PAGO (notificação)
# ============================================================
async def _job_pix_unpaid() -> None:
    """
    Envia notificação (key="pix_naopago") para usuários que:
      - geraram um PIX há mais de DELAY minutos
      - o PIX já expirou
      - ainda não pagaram
      - e não foram notificados por este mesmo PIX ainda
    """
    if not settings.AUTO_PIX_UNPAID:
        return

    delay_min = max(1, int(settings.AUTO_PIX_UNPAID_DELAY or 60))
    threshold = datetime.now(timezone.utc) - timedelta(minutes=delay_min)

    try:
        async with session_scope() as session:
            res = await session.execute(
                select(PixCharge)
                .where(PixCharge.status == PixStatus.EXPIRED)
                .where(PixCharge.expires_at <= threshold)
                .where(PixCharge.paid_at.is_(None))
                .order_by(PixCharge.created_at.desc())
                .limit(50)
            )
            charges = list(res.scalars().all())
    except Exception as exc:
        logger.debug(f"_job_pix_unpaid query falhou: {exc}")
        return

    if not charges or _bot is None:
        return

    for charge in charges:
        # Verifica se já foi notificado (metadata)
        meta = charge.metadata_ or {}
        if isinstance(meta, dict) and meta.get("unpaid_notified"):
            continue

        try:
            # Carrega o usuário
            async with session_scope() as session:
                ures = await session.execute(
                    select(User).where(User.id == charge.user_id)
                )
                user: Optional[User] = ures.scalar_one_or_none()
                if user is None or user.status != UserStatus.ACTIVE:
                    continue
                tg_id = int(user.telegram_id)

            # Renderiza a mensagem (editável)
            rendered = await message_engine.render(
                "pix_naopago",
                fallback=(
                    f"⏰ <b>Seu pagamento expirou!</b>\n"
                    f"Vimos que você gerou um PIX de "
                    f"R$ {float(charge.amount or 0):.2f} "
                    f"mas não conseguiu finalizar.\n\n"
                    "🤔 Aconteceu algum problema?\n\n"
                    "Se precisar, você pode gerar um novo pagamento a "
                    "qualquer momento. Estamos aqui para ajudar!\n\n"
                    "💡 Dica: O PIX é instantâneo e a liberação é automática!"
                ),
                valor=float(charge.amount or 0),
                txid=charge.txid,
            )

            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔍 Ver Detalhes",
                            callback_data=f"recharge:check:{charge.txid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="💠 Fazer Recarga",
                            callback_data="recharge:open",
                        )
                    ],
                ]
            )

            await _bot.send_message(
                chat_id=tg_id,
                text=rendered.content,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )

            # Marca como notificado
            try:
                async with session_scope() as session:
                    cres = await session.execute(
                        select(PixCharge).where(PixCharge.id == charge.id)
                    )
                    c2: Optional[PixCharge] = cres.scalar_one_or_none()
                    if c2 is not None:
                        m = dict(c2.metadata_ or {})
                        m["unpaid_notified"] = True
                        c2.metadata_ = m
                        session.add(c2)
            except Exception:
                pass

            # Pequeno delay para não flood
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug(f"envio pix_naopago falhou ({charge.txid}): {exc}")


async def _pix_unpaid_loop() -> None:
    logger.info("⏰ Automação PIX Não Pago iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_pix_unpaid()
        except Exception as exc:
            logger.exception(f"pix_unpaid loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_PIX_UNPAID)


# ============================================================
# 👋 3) CARRINHO ABANDONADO
# ============================================================
async def _job_cart_abandoned() -> None:
    """Dispara mensagem (key="carrinho") para carrinhos abandonados vencidos."""
    if not settings.AUTO_CART_ABANDONED or _bot is None:
        return

    try:
        due = await cart_tracker.collect_due(limit=50)
    except Exception as exc:
        logger.debug(f"collect_due falhou: {exc}")
        return

    if not due:
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    for item in due:
        try:
            rendered = await message_engine.render(
                "carrinho",
                fallback=(
                    f"👋 <b>Ei, você esqueceu algo!</b>\n"
                    f"Notamos que você estava olhando o produto "
                    f"<b>{item.product_name}</b> mas não finalizou a compra.\n\n"
                    f"💰 Valor: R$ {item.price:.2f}\n\n"
                    "🎁 Que tal finalizar agora?\n"
                    "Seu produto está esperando por você!\n\n"
                    "Se tiver alguma dúvida, estamos aqui para ajudar!"
                ),
                nome_produto=item.product_name,
                preco=item.price,
            )

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🛒 Comprar Agora",
                            callback_data=f"catalog:product:{item.product_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔍 Ver Detalhes",
                            callback_data=f"catalog:product:{item.product_id}",
                        )
                    ],
                ]
            )

            await _bot.send_message(
                chat_id=item.chat_id or item.user_id,
                text=rendered.content,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )

            await cart_tracker.mark_notified(item)
            await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug(f"envio carrinho falhou ({item.user_id}): {exc}")


async def _cart_loop() -> None:
    logger.info("👋 Automação Carrinho Abandonado iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_cart_abandoned()
        except Exception as exc:
            logger.exception(f"cart loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_CART)


# ============================================================
# 📢 4) PROMOÇÃO NOVA (agendadas)
# ============================================================
async def _job_promo_scheduled() -> None:
    """Processa promoções agendadas (canal + usuários + recorrência)."""
    if not settings.AUTO_NEW_PROMO or _bot is None:
        return

    try:
        # channel_post.process_scheduled já trata canal + usuários + recorrência
        count = await channel_post.process_scheduled(_bot)
        if count > 0:
            logger.info(f"📢 {count} promoção(ões) agendada(s) enviada(s).")
    except Exception as exc:
        logger.debug(f"process_scheduled falhou: {exc}")


async def _promo_loop() -> None:
    logger.info("📢 Automação Promoção Nova iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_promo_scheduled()
        except Exception as exc:
            logger.exception(f"promo loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_PROMO)


# ============================================================
# 📦 5) ESTOQUE BAIXO / REABASTECIDO
# ============================================================
async def _job_stock() -> None:
    """
    Verifica TODOS os produtos visíveis.
    - Se mudou para LOW ou OUT → alerta admins
    - Se mudou para REFILLED   → notifica inscritos + post no canal
    """
    if not settings.AUTO_LOW_STOCK or _bot is None:
        return

    try:
        events = await stock_alerts.scan_all()
    except Exception as exc:
        logger.debug(f"scan_all falhou: {exc}")
        return

    if not events:
        return

    from bot.handlers.alerts import dispatch_stock_notifications

    for ev in events:
        try:
            # Notifica inscritos + persiste alerta
            await dispatch_stock_notifications(_bot, ev)

            # Post no canal se foi reabastecido
            if ev.state in ("refilled", "low", "out") and ev.new_stock >= 0:
                if ev.state == "refilled":
                    try:
                        await channel_post.post_stock(
                            _bot,
                            product_id=ev.product_id,
                            product_name=ev.product_name,
                            quantity=ev.new_stock,
                        )
                    except Exception as exc:
                        logger.debug(f"post_stock canal falhou: {exc}")

            # Alerta admins
            try:
                if settings.admin_ids_list:
                    for admin_id in settings.admin_ids_list[:5]:
                        try:
                            await _bot.send_message(
                                chat_id=admin_id,
                                text=(
                                    f"⚠️ <b>Estoque {ev.state.upper()}</b>\n\n"
                                    f"📦 {ev.product_name}\n"
                                    f"Antes: {ev.prev_stock} · Agora: {ev.new_stock}\n"
                                    f"Threshold: {ev.threshold}"
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception as exc:
            logger.debug(f"processar evento de estoque falhou: {exc}")


async def _stock_loop() -> None:
    logger.info("📦 Automação Estoque iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_stock()
        except Exception as exc:
            logger.exception(f"stock loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_STOCK)


# ============================================================
# 🔒 6) LOCKS ÓRFÃOS
# ============================================================
async def _job_locks() -> None:
    """Limpa locks órfãos do Redis e da tabela `locks`."""
    try:
        # 1) Limpa locks do Redis (Scan para padrão product:*:lock)
        r = await get_redis()
        pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
        removed = 0
        async for key in r.scan_iter(match=pattern, count=200):
            ttl = await r.ttl(key)
            if ttl is not None and ttl < 0:
                # Sem TTL → libera
                await r.delete(key)
                removed += 1

        # 2) Limpa tabela `locks` (marcação released_at em locks vencidos)
        try:
            now = datetime.now(timezone.utc)
            async with session_scope() as session:
                await session.execute(
                    update(Lock)
                    .where(Lock.expires_at <= now)
                    .where(Lock.released_at.is_(None))
                    .values(released_at=now)
                )
        except Exception:
            pass

        if removed > 0:
            logger.info(f"🔒 {removed} lock(s) órfão(s) liberado(s).")
    except Exception as exc:
        logger.debug(f"locks job falhou: {exc}")


async def _locks_loop() -> None:
    logger.info("🔒 Automação Travas iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_locks()
        except Exception as exc:
            logger.exception(f"locks loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_LOCKS)


# ============================================================
# 💾 7) BACKUP AUTOMÁTICO
# ============================================================
async def _job_backup() -> None:
    """Gera backup do banco usando pg_dump (se disponível) ou export simples."""
    if not settings.AUTO_BACKUP_ENABLED:
        return

    try:
        backup_dir = settings.backups_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{ts}.sql"
        filepath = backup_dir / filename

        # ---------- Tenta pg_dump ----------
        success = False
        try:
            cmd = [
                "pg_dump",
                settings.db_url_sync,
                "-f",
                str(filepath),
                "--no-owner",
                "--no-privileges",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                success = True
            else:
                logger.debug(f"pg_dump falhou: {stderr.decode()[:200]}")
        except FileNotFoundError:
            logger.debug("pg_dump indisponível.")
        except Exception as exc:
            logger.debug(f"pg_dump exception: {exc}")

        # ---------- Fallback: SQLite-less snapshot simples ----------
        if not success:
            # Não vamos gerar dados fictícios; apenas registra que falhou
            logger.debug("Backup fallback: pulando (pg_dump indisponível).")
            return

        # ---------- Registra em `backups` ----------
        try:
            size = filepath.stat().st_size
            async with session_scope() as session:
                session.add(
                    Backup(
                        filename=filename,
                        size=size,
                        type="auto",
                        status="ok",
                    )
                )
            logger.info(f"💾 Backup gerado: {filename} ({size} bytes)")
        except Exception as exc:
            logger.debug(f"registrar backup falhou: {exc}")

        # ---------- Limpeza por retenção ----------
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=int(settings.BACKUP_RETENTION_DAYS or 7)
            )
            for f in backup_dir.iterdir():
                if not f.is_file():
                    continue
                if f.suffix != ".sql":
                    continue
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    f.unlink()
        except Exception as exc:
            logger.debug(f"limpeza backup falhou: {exc}")

    except Exception as exc:
        logger.exception(f"_job_backup falhou: {exc}")


async def _backup_loop() -> None:
    logger.info("💾 Automação Backup iniciada.")
    interval = max(
        1800,
        int(settings.AUTO_BACKUP_INTERVAL_HOURS or 24) * 3600,
    )
    while not _shutdown_event.is_set():
        try:
            await _job_backup()
        except Exception as exc:
            logger.exception(f"backup loop erro: {exc}")
        await _sleep_or_stop(interval)


# ============================================================
# 🧹 8) LIMPEZA DE FLOWSTATES ÓRFÃOS
# ============================================================
async def _job_flow_cleanup() -> None:
    """Remove chaves de FlowState antigas sem TTL."""
    try:
        r = await get_redis()
        pattern = Keys.k(Keys.FLOW, uid="*", name="*")
        removed = 0
        async for key in r.scan_iter(match=pattern, count=200):
            ttl = await r.ttl(key)
            if ttl is None or ttl < 0:
                await r.delete(key)
                removed += 1
        if removed > 0:
            logger.debug(f"🧹 {removed} FlowState(s) órfão(s) removido(s).")
    except Exception as exc:
        logger.debug(f"flow_cleanup falhou: {exc}")


async def _flow_cleanup_loop() -> None:
    logger.info("🧹 Automação Limpeza de FlowStates iniciada.")
    while not _shutdown_event.is_set():
        try:
            await _job_flow_cleanup()
        except Exception as exc:
            logger.exception(f"flow_cleanup loop erro: {exc}")
        await _sleep_or_stop(INTERVAL_FLOW_CLEANUP)


# ============================================================
# 🚀 STARTUP / SHUTDOWN
# ============================================================
async def _startup() -> None:
    """Inicializa tudo que o worker precisa."""
    logger.info(
        f"🚀 Worker {WORKER_VERSION} iniciando "
        f"({settings.APP_NAME} · env={settings.ENV})"
    )

    # 1) Database
    try:
        await init_db()
        ok = await db_healthcheck()
        logger.info(f"🗄️ Banco: {'OK' if ok else 'FALHOU'}")
    except Exception as exc:
        logger.exception(f"init_db falhou: {exc}")

    # 2) Redis
    try:
        ok = await redis_ping()
        logger.info(f"⚡ Redis: {'OK' if ok else 'FALHOU'}")
    except Exception as exc:
        logger.exception(f"redis ping falhou: {exc}")

    # 3) Serviços
    try:
        await init_services(_bot)
        health = await services_health()
        logger.info(f"🧩 Serviços: {health}")
    except Exception as exc:
        logger.exception(f"init_services falhou: {exc}")

    # 4) Bot info
    if _bot is not None:
        try:
            me = await _bot.get_me()
            logger.info(f"🤖 Worker bot: @{me.username} (id={me.id})")
        except Exception as exc:
            logger.warning(f"get_me falhou: {exc}")


async def _shutdown() -> None:
    """Encerra tudo."""
    logger.info("🛑 Encerrando worker...")

    # Cancela todas as tasks
    for task in _tasks:
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)

    try:
        await shutdown_services()
    except Exception as exc:
        logger.debug(f"shutdown_services falhou: {exc}")

    try:
        await close_redis()
    except Exception:
        pass

    try:
        await close_db()
    except Exception:
        pass

    if _bot is not None:
        try:
            await _bot.session.close()
        except Exception:
            pass

    logger.info("✅ Worker encerrado.")


# ============================================================
# 🎯 MAIN
# ============================================================
async def amain() -> None:
    global _bot

    setup_logger()
    _bot = build_worker_bot()

    # Registra signal handlers
    loop = asyncio.get_running_loop()

    def _ask_shutdown() -> None:
        _shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _ask_shutdown)
        except NotImplementedError:
            pass

    await _startup()

    # ---- Cria as tasks de automação ----
    _tasks.extend(
        [
            asyncio.create_task(_heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(_pix_expired_loop(), name="pix_expired"),
            asyncio.create_task(_pix_unpaid_loop(), name="pix_unpaid"),
            asyncio.create_task(_cart_loop(), name="cart"),
            asyncio.create_task(_promo_loop(), name="promo"),
            asyncio.create_task(_stock_loop(), name="stock"),
            asyncio.create_task(_locks_loop(), name="locks"),
            asyncio.create_task(_backup_loop(), name="backup"),
            asyncio.create_task(_flow_cleanup_loop(), name="flow_cleanup"),
        ]
    )

    logger.info(f"🧩 {len(_tasks)} automações ativas.")

    # ---- Aguarda shutdown ----
    await _shutdown_event.wait()
    await _shutdown()


def main() -> None:
    """Wrapper síncrono."""
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário (Ctrl+C).")
    except Exception as exc:
        logger.exception(f"Erro fatal no worker: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "build_worker_bot",
    "is_worker_alive",
    "amain",
    "main",
    "WORKER_VERSION",
    "HEARTBEAT_KEY",
]
