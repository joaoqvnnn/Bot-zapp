"""
Larizinha Store — ChannelPost Service
Serviço de auto-post no CANAL conectado.

Regras invioláveis (DIM):
- Auto-post ao COMPRAR       → mensagem editável key="canal_compra"
- Auto-post ao ABASTECER     → mensagem editável key="canal_estoque"
- Auto-post ao ATIVAR PROMO  → mensagem editável key="canal_promo"
- TODOS os textos e botões do post são editáveis pelo painel admin.
- Envio com botões inline (renderizados pelo button_engine).
- Agendamento real (canal / usuários / ambos + data+hora + recorrência).
- Filtros: ativos, com saldo, inativos, compradores, afiliados.
- NUNCA acumula: cada post é enviado uma vez (não edita posts antigos do canal).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from bot.config import settings
from bot.database import (
    Channel,
    Order,
    Product,
    Promotion,
    User,
    UserStatus,
    session_scope,
)
from bot.keyboards.button_engine import render_inline
from bot.logger import logger
from bot.services.message_engine import message_engine


# ============================================================
# 🔑 CONSTANTES
# ============================================================
MSG_KEY_CANAL_COMPRA = "canal_compra"
MSG_KEY_CANAL_ESTOQUE = "canal_estoque"
MSG_KEY_CANAL_PROMO = "canal_promo"

SCREEN_CANAL_COMPRA = "canal_compra"
SCREEN_CANAL_ESTOQUE = "canal_estoque"
SCREEN_CANAL_PROMO = "canal_promo"

# Tipos de agendamento
TARGET_CHANNEL = "channel"
TARGET_USERS = "users"
TARGET_BOTH = "both"

# Recorrências
REC_NONE = "none"
REC_DAILY = "daily"
REC_WEEKLY = "weekly"
REC_MONTHLY = "monthly"
VALID_RECURRENCES = {REC_NONE, REC_DAILY, REC_WEEKLY, REC_MONTHLY}

# Filtros de público
FILTER_ALL = "all"
FILTER_ACTIVE = "active"
FILTER_WITH_BALANCE = "with_balance"
FILTER_INACTIVE = "inactive"
FILTER_BUYERS = "buyers"
FILTER_AFFILIATES = "affiliates"


# ============================================================
# 🧱 DATACLASS DE RESULTADO
# ============================================================
@dataclass
class PostResult:
    ok: bool
    sent: int = 0
    failed: int = 0
    message_id: Optional[int] = None
    error: Optional[str] = None


@dataclass
class PostContext:
    """Contexto mínimo usado para montar as variáveis do post."""

    product_id: Optional[int] = None
    product_name: Optional[str] = None
    product_price: Optional[float] = None
    product_subtitle: Optional[str] = None
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    quantity: Optional[int] = None
    validity: Optional[str] = None
    status: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# 🧠 SERVIÇO
# ============================================================
class ChannelPostService:
    """
    Serviço de auto-post no canal conectado.

    Uso típico:
        await channel_post.post_buy(bot, product=..., user=..., validity="30d")
        await channel_post.post_stock(bot, product=..., quantity=10)
        await channel_post.post_promo(bot, product=...)
        await channel_post.process_scheduled(bot)   # rodar no worker
    """

    def __init__(self) -> None:
        self._bot: Optional[Bot] = None

    # --------------------------------------------------------
    def attach_bot(self, bot: Bot) -> None:
        """Guarda a referência do bot para usos internos."""
        self._bot = bot

    # --------------------------------------------------------
    # 📡 CANAIS ALVO
    # --------------------------------------------------------
    async def _load_channels(self, kind: str = "any") -> List[Dict[str, Any]]:
        """
        kind:
          - "any"      → todos os canais com auto-post ligado para qualquer tipo
          - "buy"      → só com auto_post_buy=True
          - "stock"    → só com auto_post_stock=True
          - "promo"    → só com auto_post_promo=True
        """
        try:
            async with session_scope() as session:
                stmt = select(Channel)
                if kind == "buy":
                    stmt = stmt.where(Channel.auto_post_buy == True)  # noqa: E712
                elif kind == "stock":
                    stmt = stmt.where(Channel.auto_post_stock == True)  # noqa: E712
                elif kind == "promo":
                    stmt = stmt.where(Channel.auto_post_promo == True)  # noqa: E712
                res = await session.execute(stmt)
                rows = res.scalars().all()
                return [
                    {
                        "chat_id": int(c.chat_id),
                        "username": c.username,
                        "title": c.title,
                        "type": c.type.value if hasattr(c.type, "value") else str(c.type),
                    }
                    for c in rows
                ]
        except Exception as exc:
            logger.exception(f"_load_channels({kind}) falhou: {exc}")
            return []

    # --------------------------------------------------------
    # 🧾 RENDER DO POST
    # --------------------------------------------------------
    async def _render(
        self,
        key: str,
        screen: str,
        ctx: PostContext,
    ) -> tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
        """
        Renderiza texto + teclado do post.
        - key: chave do template (canal_compra / canal_estoque / canal_promo)
        - screen: tela para o button_engine
        """
        # Variáveis padrão disponíveis para o template
        variables = {
            "nome_produto": ctx.product_name or "",
            "subtitulo": ctx.product_subtitle or "",
            "preco": ctx.product_price or 0.0,
            "quantidade": ctx.quantity or 0,
            "nome": ctx.user_name or "",
            "validade": ctx.validity or "—",
            "status": ctx.status or "—",
            "data": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "nome_loja": settings.APP_NAME,
            "versao": settings.APP_VERSION,
            **ctx.extra,
        }

        rendered = await message_engine.render(key, **variables)
        keyboard = await render_inline(screen)

        return rendered.content, keyboard

    # --------------------------------------------------------
    # 🚀 ENVIO PARA UM CANAL
    # --------------------------------------------------------
    async def _send_to_channel(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup],
    ) -> PostResult:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return PostResult(ok=True, sent=1, message_id=msg.message_id)
        except Exception as exc:
            logger.warning(f"Falha ao postar no canal {chat_id}: {exc}")
            return PostResult(ok=False, failed=1, error=str(exc))

    # --------------------------------------------------------
    # 🛒 POST: COMPRA
    # --------------------------------------------------------
    async def post_buy(
        self,
        bot: Bot,
        *,
        product_id: Optional[int] = None,
        product_name: Optional[str] = None,
        user_id: Optional[int] = None,
        user_name: Optional[str] = None,
        validity: Optional[str] = None,
        status: Optional[str] = "Acesso liberado",
        extra: Optional[Dict[str, Any]] = None,
    ) -> PostResult:
        """Posta a mensagem editável `canal_compra` em todos os canais com auto_post_buy."""
        channels = await self._load_channels("buy")
        if not channels:
            return PostResult(ok=True, sent=0)

        ctx = PostContext(
            product_id=product_id,
            product_name=product_name,
            user_id=user_id,
            user_name=user_name,
            validity=validity,
            status=status,
            extra=extra or {},
        )
        text, keyboard = await self._render(
            MSG_KEY_CANAL_COMPRA, SCREEN_CANAL_COMPRA, ctx
        )
        if not text:
            return PostResult(ok=False, error="Template canal_compra ausente")

        total = PostResult(ok=True)
        for ch in channels:
            r = await self._send_to_channel(bot, ch["chat_id"], text, keyboard)
            total.sent += r.sent
            total.failed += r.failed
        return total

    # --------------------------------------------------------
    # 📦 POST: ESTOQUE
    # --------------------------------------------------------
    async def post_stock(
        self,
        bot: Bot,
        *,
        product_id: int,
        product_name: str,
        quantity: int,
        extra: Optional[Dict[str, Any]] = None,
    ) -> PostResult:
        """Posta `canal_estoque` em todos os canais com auto_post_stock."""
        channels = await self._load_channels("stock")
        if not channels:
            return PostResult(ok=True, sent=0)

        ctx = PostContext(
            product_id=product_id,
            product_name=product_name,
            quantity=quantity,
            extra=extra or {},
        )
        text, keyboard = await self._render(
            MSG_KEY_CANAL_ESTOQUE, SCREEN_CANAL_ESTOQUE, ctx
        )
        if not text:
            return PostResult(ok=False, error="Template canal_estoque ausente")

        total = PostResult(ok=True)
        for ch in channels:
            r = await self._send_to_channel(bot, ch["chat_id"], text, keyboard)
            total.sent += r.sent
            total.failed += r.failed
        return total

    # --------------------------------------------------------
    # 🎁 POST: PROMOÇÃO
    # --------------------------------------------------------
    async def post_promo(
        self,
        bot: Bot,
        *,
        product_id: int,
        product_name: str,
        price: float,
        subtitle: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> PostResult:
        """Posta `canal_promo` em todos os canais com auto_post_promo."""
        channels = await self._load_channels("promo")
        if not channels:
            return PostResult(ok=True, sent=0)

        ctx = PostContext(
            product_id=product_id,
            product_name=product_name,
            product_price=price,
            product_subtitle=subtitle,
            extra=extra or {},
        )
        text, keyboard = await self._render(
            MSG_KEY_CANAL_PROMO, SCREEN_CANAL_PROMO, ctx
        )
        if not text:
            return PostResult(ok=False, error="Template canal_promo ausente")

        total = PostResult(ok=True)
        for ch in channels:
            r = await self._send_to_channel(bot, ch["chat_id"], text, keyboard)
            total.sent += r.sent
            total.failed += r.failed
        return total

    # --------------------------------------------------------
    # 👥 FILTRO DE PÚBLICO (agendamento)
    # --------------------------------------------------------
    async def _get_target_users(
        self, filter_type: str, extra_value: Optional[str] = None
    ) -> List[int]:
        """
        Retorna a lista de telegram_ids alvo do disparo.
        filter_type: all / active / with_balance / inactive / buyers / affiliates
        """
        try:
            async with session_scope() as session:
                stmt = select(User)

                if filter_type == FILTER_ACTIVE:
                    stmt = stmt.where(User.status == UserStatus.ACTIVE)
                elif filter_type == FILTER_INACTIVE:
                    stmt = stmt.where(User.status == UserStatus.BLOCKED)
                elif filter_type == FILTER_WITH_BALANCE:
                    stmt = stmt.where(User.balance > 0)
                elif filter_type == FILTER_AFFILIATES:
                    stmt = stmt.where(User.affiliate_active == True)  # noqa: E712
                elif filter_type == FILTER_BUYERS:
                    stmt = stmt.where(User.purchases_count > 0)

                res = await session.execute(stmt)
                rows = res.scalars().all()
                return [int(u.telegram_id) for u in rows if u.telegram_id]
        except Exception as exc:
            logger.exception(f"_get_target_users({filter_type}) falhou: {exc}")
            return []

    # --------------------------------------------------------
    # 📤 DISPARO PARA USUÁRIOS (via DM)
    # --------------------------------------------------------
    async def _send_dm(
        self,
        bot: Bot,
        telegram_id: int,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup],
    ) -> bool:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return True
        except Exception:
            return False

    # --------------------------------------------------------
    # ⏰ AGENDAMENTO — CRIAR
    # --------------------------------------------------------
    async def schedule(
        self,
        *,
        product_id: Optional[int],
        target_type: str,
        target_value: Optional[str],
        scheduled_at: datetime,
        recurrence: str = REC_NONE,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Promotion]:
        """
        Cria uma promoção agendada.

        target_type: channel | users | both
        target_value: ex. filter ("all", "buyers") quando users/both
        recurrence:  none | daily | weekly | monthly
        """
        if target_type not in (TARGET_CHANNEL, TARGET_USERS, TARGET_BOTH):
            target_type = TARGET_CHANNEL
        if recurrence not in VALID_RECURRENCES:
            recurrence = REC_NONE

        try:
            async with session_scope() as session:
                promo = Promotion(
                    product_id=product_id,
                    message_key=MSG_KEY_CANAL_PROMO,
                    target_type=target_type,
                    target_value=target_value,
                    scheduled_at=scheduled_at,
                    recurrence=recurrence,
                    status="pending",
                )
                session.add(promo)
                await session.flush()
                return promo
        except Exception as exc:
            logger.exception(f"schedule falhou: {exc}")
            return None

    # --------------------------------------------------------
    # ⏰ AGENDAMENTO — PROCESSAR
    # --------------------------------------------------------
    async def process_scheduled(self, bot: Optional[Bot] = None) -> int:
        """
        Processa promoções vencidas (status=pending e scheduled_at <= now).
        Retorna quantos disparos ocorreram.
        """
        bot = bot or self._bot
        if bot is None:
            logger.debug("process_scheduled: bot não disponível")
            return 0

        now = datetime.now(timezone.utc)
        dispatched = 0

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Promotion)
                    .where(Promotion.status == "pending")
                    .where(Promotion.scheduled_at <= now)
                )
                promos = list(res.scalars().all())
        except Exception as exc:
            logger.exception(f"process_scheduled: query falhou: {exc}")
            return 0

        for promo in promos:
            try:
                # Carrega produto
                product = None
                if promo.product_id:
                    async with session_scope() as session:
                        r = await session.execute(
                            select(Product).where(Product.id == promo.product_id)
                        )
                        product = r.scalar_one_or_none()

                # Envia conforme target
                await self._dispatch_promo(
                    bot=bot,
                    promo=promo,
                    product=product,
                )

                # Atualiza status / recorrência
                await self._reschedule_or_finish(promo)
                dispatched += 1
            except Exception as exc:
                logger.exception(f"process_scheduled: promo {promo.id} falhou: {exc}")

        return dispatched

    async def _dispatch_promo(
        self,
        *,
        bot: Bot,
        promo: Promotion,
        product: Optional[Product],
    ) -> None:
        """Envia a promo agendada para canal e/ou usuários."""
        # ---------- Monta contexto ----------
        ctx = PostContext(
            product_id=int(product.id) if product else None,
            product_name=str(product.name) if product else "—",
            product_price=float(product.price_promo or product.price) if product else 0.0,
            product_subtitle=str(product.subtitle) if product and product.subtitle else None,
        )
        text, keyboard = await self._render(
            MSG_KEY_CANAL_PROMO, SCREEN_CANAL_PROMO, ctx
        )
        if not text:
            logger.warning(f"process_scheduled: template canal_promo ausente (promo {promo.id})")
            return

        # ---------- Canal ----------
        if promo.target_type in (TARGET_CHANNEL, TARGET_BOTH):
            channels = await self._load_channels("promo")
            for ch in channels:
                await self._send_to_channel(bot, ch["chat_id"], text, keyboard)

        # ---------- Usuários ----------
        if promo.target_type in (TARGET_USERS, TARGET_BOTH):
            filter_type = promo.target_value or FILTER_ALL
            user_ids = await self._get_target_users(filter_type)
            for uid in user_ids:
                await self._send_dm(bot, uid, text, keyboard)

    async def _reschedule_or_finish(self, promo: Promotion) -> None:
        """Atualiza o status e, se houver recorrência, agenda a próxima."""
        try:
            async with session_scope() as session:
                # Recarrega
                r = await session.execute(select(Promotion).where(Promotion.id == promo.id))
                db_promo: Optional[Promotion] = r.scalar_one_or_none()
                if db_promo is None:
                    return

                db_promo.status = "sent"
                db_promo.sent_at = datetime.now(timezone.utc)

                # Recorrência
                if promo.recurrence and promo.recurrence != REC_NONE:
                    delta = self._recurrence_delta(promo.recurrence)
                    if delta is not None:
                        next_dt = db_promo.scheduled_at + delta
                        new_promo = Promotion(
                            product_id=db_promo.product_id,
                            message_key=db_promo.message_key,
                            target_type=db_promo.target_type,
                            target_value=db_promo.target_value,
                            scheduled_at=next_dt,
                            recurrence=db_promo.recurrence,
                            status="pending",
                        )
                        session.add(new_promo)

                session.add(db_promo)
        except Exception as exc:
            logger.exception(f"_reschedule_or_finish falhou: {exc}")

    @staticmethod
    def _recurrence_delta(recurrence: str) -> Optional[timedelta]:
        if recurrence == REC_DAILY:
            return timedelta(days=1)
        if recurrence == REC_WEEKLY:
            return timedelta(days=7)
        if recurrence == REC_MONTHLY:
            return timedelta(days=30)
        return None

    # --------------------------------------------------------
    # 🧹 CANCELAR AGENDAMENTO
    # --------------------------------------------------------
    async def cancel_scheduled(self, promo_id: int) -> bool:
        try:
            async with session_scope() as session:
                r = await session.execute(select(Promotion).where(Promotion.id == promo_id))
                p: Optional[Promotion] = r.scalar_one_or_none()
                if p is None or p.status not in ("pending",):
                    return False
                p.status = "canceled"
                session.add(p)
            return True
        except Exception:
            return False

    # --------------------------------------------------------
    # 📜 LISTAR AGENDAMENTOS
    # --------------------------------------------------------
    async def list_scheduled(self, only_pending: bool = True) -> List[Promotion]:
        try:
            async with session_scope() as session:
                stmt = select(Promotion).order_by(Promotion.scheduled_at.asc())
                if only_pending:
                    stmt = stmt.where(Promotion.status == "pending")
                res = await session.execute(stmt)
                return list(res.scalars().all())
        except Exception:
            return []

    # --------------------------------------------------------
    # 💚 HEALTHCHECK
    # --------------------------------------------------------
    async def healthcheck(self) -> bool:
        try:
            from bot.database import healthcheck as db_health

            return await db_health()
        except Exception:
            return False


# ============================================================
# 📌 SINGLETON
# ============================================================
channel_post = ChannelPostService()


# ============================================================
# 🧩 HELPERS DE MÓDULO
# ============================================================
async def post_buy(
    bot: Bot,
    *,
    product_name: str,
    user_name: Optional[str] = None,
    validity: Optional[str] = None,
    status: Optional[str] = "Acesso liberado",
) -> PostResult:
    """Atalho: post de compra no canal."""
    return await channel_post.post_buy(
        bot,
        product_name=product_name,
        user_name=user_name,
        validity=validity,
        status=status,
    )


async def post_stock(
    bot: Bot,
    *,
    product_id: int,
    product_name: str,
    quantity: int,
) -> PostResult:
    """Atalho: post de abastecimento no canal."""
    return await channel_post.post_stock(
        bot,
        product_id=product_id,
        product_name=product_name,
        quantity=quantity,
    )


async def post_promo(
    bot: Bot,
    *,
    product_id: int,
    product_name: str,
    price: float,
    subtitle: Optional[str] = None,
) -> PostResult:
    """Atalho: post de promoção no canal."""
    return await channel_post.post_promo(
        bot,
        product_id=product_id,
        product_name=product_name,
        price=price,
        subtitle=subtitle,
    )


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "ChannelPostService",
    "PostResult",
    "PostContext",
    "channel_post",
    "post_buy",
    "post_stock",
    "post_promo",
    "MSG_KEY_CANAL_COMPRA",
    "MSG_KEY_CANAL_ESTOQUE",
    "MSG_KEY_CANAL_PROMO",
    "SCREEN_CANAL_COMPRA",
    "SCREEN_CANAL_ESTOQUE",
    "SCREEN_CANAL_PROMO",
    "TARGET_CHANNEL",
    "TARGET_USERS",
    "TARGET_BOTH",
    "REC_NONE",
    "REC_DAILY",
    "REC_WEEKLY",
    "REC_MONTHLY",
    "FILTER_ALL",
    "FILTER_ACTIVE",
    "FILTER_WITH_BALANCE",
    "FILTER_INACTIVE",
    "FILTER_BUYERS",
    "FILTER_AFFILIATES",
]
