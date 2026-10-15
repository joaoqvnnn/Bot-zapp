"""
Larizinha Store — Handler de Ranking
Implementa o Top Compradores com botões toggle exatamente como o DIM define:

    [☑️ Serviços] [☑️ Recargas] [☑️ Compras] [☑️ Gift Card] [☑️ Saldo]

    • Ao clicar em um botão → ele vira [✅ <nome>]
      e os OUTROS voltam automaticamente para [☑️ <nome>]
    • A MESMA mensagem é EDITADA (nunca acumula)
    • Ranking sempre 1°) a 10°)

Regras invioláveis:
- NUNCA acumula: sempre edit_message_text.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Dados reais: query no banco (Order, OrderItem, Transaction, GiftCard).
- Toggle state é aplicado diretamente no keyboard a cada clique.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    GiftCardRedemption,
    Order,
    OrderItem,
    OrderStatus,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import Throttle
from bot.services.message_engine import message_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="ranking")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_RANKING = "ranking"
SCREEN_RANKING = "ranking"

CB_RANK_OPEN = "ranking:open"
CB_RANK_CAT = "ranking:cat"       # ranking:cat:<categoria>

# Categorias válidas
CAT_SERVICOS = "servicos"
CAT_RECARGAS = "recargas"
CAT_COMPRAS = "compras"
CAT_GIFTS = "gifts"
CAT_SALDO = "saldo"

CATEGORIES = [
    (CAT_SERVICOS, "☑️ Serviços", "✅ Serviços"),
    (CAT_RECARGAS, "☑️ Recargas", "✅ Recargas"),
    (CAT_COMPRAS, "☑️ Compras", "✅ Compras"),
    (CAT_GIFTS, "☑️ Gift Card", "✅ Gift Card"),
    (CAT_SALDO, "☑️ Saldo", "✅ Saldo"),
]

DEFAULT_CATEGORY = CAT_SERVICOS


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _start_of_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ============================================================
# 📊 CONSULTAS REAIS
# ============================================================
async def _top_servicos(limit: int = 10) -> List[Tuple[str, int]]:
    """Top 10 produtos mais vendidos (mês atual)."""
    try:
        since = _start_of_month()
        async with session_scope() as session:
            res = await session.execute(
                select(
                    OrderItem.product_name,
                    func.sum(OrderItem.quantity).label("qtd"),
                )
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.status == OrderStatus.DELIVERED)
                .where(Order.created_at >= since)
                .group_by(OrderItem.product_name)
                .order_by(func.sum(OrderItem.quantity).desc())
                .limit(limit)
            )
            return [(str(nome), int(qtd or 0)) for nome, qtd in res.all()]
    except Exception as exc:
        logger.debug(f"_top_servicos falhou: {exc}")
        return []


async def _top_recargas(limit: int = 10) -> List[Tuple[str, float]]:
    """Top 10 usuários por valor depositado (mês atual)."""
    try:
        since = _start_of_month()
        async with session_scope() as session:
            res = await session.execute(
                select(
                    User.first_name,
                    User.telegram_id,
                    func.coalesce(func.sum(Transaction.amount), 0).label("total"),
                )
                .join(Transaction, Transaction.user_id == User.id)
                .where(Transaction.type == TransactionType.DEPOSIT)
                .where(Transaction.created_at >= since)
                .group_by(User.id, User.first_name, User.telegram_id)
                .order_by(func.sum(Transaction.amount).desc())
                .limit(limit)
            )
            rows: List[Tuple[str, float]] = []
            for nome, tg, total in res.all():
                label = (nome or f"Usuário {tg}")[:24]
                rows.append((label, float(total or 0)))
            return rows
    except Exception as exc:
        logger.debug(f"_top_recargas falhou: {exc}")
        return []


async def _top_compras(limit: int = 10) -> List[Tuple[str, int]]:
    """Top 10 usuários por número de compras (mês atual)."""
    try:
        since = _start_of_month()
        async with session_scope() as session:
            res = await session.execute(
                select(
                    User.first_name,
                    User.telegram_id,
                    func.count(Order.id).label("qtd"),
                )
                .join(Order, Order.user_id == User.id)
                .where(Order.status == OrderStatus.DELIVERED)
                .where(Order.created_at >= since)
                .group_by(User.id, User.first_name, User.telegram_id)
                .order_by(func.count(Order.id).desc())
                .limit(limit)
            )
            rows: List[Tuple[str, int]] = []
            for nome, tg, qtd in res.all():
                label = (nome or f"Usuário {tg}")[:24]
                rows.append((label, int(qtd or 0)))
            return rows
    except Exception as exc:
        logger.debug(f"_top_compras falhou: {exc}")
        return []


async def _top_gifts(limit: int = 10) -> List[Tuple[str, float]]:
    """Top 10 usuários que mais resgataram Gift Card (mês atual)."""
    try:
        since = _start_of_month()
        async with session_scope() as session:
            res = await session.execute(
                select(
                    User.first_name,
                    User.telegram_id,
                    func.coalesce(func.sum(GiftCardRedemption.amount), 0).label("total"),
                )
                .join(GiftCardRedemption, GiftCardRedemption.user_id == User.id)
                .where(GiftCardRedemption.created_at >= since)
                .group_by(User.id, User.first_name, User.telegram_id)
                .order_by(func.sum(GiftCardRedemption.amount).desc())
                .limit(limit)
            )
            rows: List[Tuple[str, float]] = []
            for nome, tg, total in res.all():
                label = (nome or f"Usuário {tg}")[:24]
                rows.append((label, float(total or 0)))
            return rows
    except Exception as exc:
        logger.debug(f"_top_gifts falhou: {exc}")
        return []


async def _top_saldo(limit: int = 10) -> List[Tuple[str, float]]:
    """Top 10 usuários com maior saldo atual."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User.first_name, User.telegram_id, User.balance)
                .order_by(User.balance.desc())
                .limit(limit)
            )
            rows: List[Tuple[str, float]] = []
            for nome, tg, saldo in res.all():
                label = (nome or f"Usuário {tg}")[:24]
                rows.append((label, float(saldo or 0)))
            return rows
    except Exception as exc:
        logger.debug(f"_top_saldo falhou: {exc}")
        return []


# ============================================================
# 🧾 MONTAGEM DO TEXTO
# ============================================================
async def _build_ranking_text(category: str) -> str:
    if category == CAT_SERVICOS:
        rows = await _top_servicos()
        titulo = "🏆 Ranking dos serviços mais vendidos (deste mês)"
        unidade = "pedidos"
        is_money = False
    elif category == CAT_RECARGAS:
        rows = await _top_recargas()
        titulo = "🏆 Ranking de recargas (deste mês)"
        unidade = ""
        is_money = True
    elif category == CAT_COMPRAS:
        rows = await _top_compras()
        titulo = "🏆 Ranking de compras (deste mês)"
        unidade = "compras"
        is_money = False
    elif category == CAT_GIFTS:
        rows = await _top_gifts()
        titulo = "🏆 Ranking de Gift Cards (deste mês)"
        unidade = ""
        is_money = True
    elif category == CAT_SALDO:
        rows = await _top_saldo()
        titulo = "🏆 Ranking de saldo atual"
        unidade = ""
        is_money = True
    else:
        rows = []
        titulo = "🏆 Ranking"
        unidade = ""
        is_money = False

    linhas: List[str] = [titulo, ""]

    if not rows:
        linhas.append("<i>Sem dados ainda para esta categoria.</i>")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, (nome, value) in enumerate(rows, start=1):
            medal = medals[i - 1] if i <= 3 else ""
            if is_money:
                valor_str = _fmt_brl(float(value))
                linhas.append(f"{i}°) {nome} {medal} — {valor_str}")
            else:
                linhas.append(
                    f"{i}°) {nome} {medal} — Com {int(value)} {unidade}".rstrip()
                )

    lista = "\n".join(linhas)

    rendered = await message_engine.render(
        MSG_KEY_RANKING,
        fallback=lista,
        lista=lista,
        categoria=category,
    )
    return rendered.content


# ============================================================
# 🧩 TECLADO COM TOGGLE (☑️ → ✅)
# ============================================================
def _build_toggle_keyboard(active: str) -> InlineKeyboardMarkup:
    """
    Monta o teclado com 5 botões em linha única.
    O botão da categoria `active` fica como "✅ Nome"; os outros "☑️ Nome".
    """
    buttons: List[InlineKeyboardButton] = []
    for cat, label_inactive, label_active in CATEGORIES:
        label = label_active if cat == active else label_inactive
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{CB_RANK_CAT}:{cat}",
            )
        )

    row1 = [buttons[0], buttons[1]]
    row2 = [buttons[2], buttons[3]]
    row3 = [buttons[4]]

    rows: List[List[InlineKeyboardButton]] = [row1, row2, row3]

    # Voltar
    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# HANDLER: ABRIR RANKING
# ============================================================
@router.callback_query(F.data == CB_RANK_OPEN)
async def on_open_ranking(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "ranking", cooldown=0.4):
            await callback.answer()
            return

        text = await _build_ranking_text(DEFAULT_CATEGORY)
        kb = _build_toggle_keyboard(DEFAULT_CATEGORY)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_open_ranking falhou: {exc}")


# ============================================================
# HANDLER: TOGGLE DE CATEGORIA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RANK_CAT}:"))
async def on_toggle_category(callback: CallbackQuery, user: User) -> None:
    try:
        category = callback.data.split(":")[-1]

        # Se clicar na categoria já ativa, mantém a mesma
        if category not in {c[0] for c in CATEGORIES}:
            category = DEFAULT_CATEGORY

        text = await _build_ranking_text(category)
        kb = _build_toggle_keyboard(category)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                # "message is not modified" → ignora silenciosamente
                if "not modified" not in str(exc).lower():
                    logger.debug(f"edit ranking falhou: {exc}")
    except Exception as exc:
        logger.exception(f"on_toggle_category falhou: {exc}")


# ============================================================
# COMANDO /ranking (atalho)
# ============================================================
@router.message(Command("ranking"))
async def on_cmd_ranking(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        text = await _build_ranking_text(DEFAULT_CATEGORY)
        kb = _build_toggle_keyboard(DEFAULT_CATEGORY)

        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/ranking falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_ranking",
    "on_toggle_category",
    "on_cmd_ranking",
    "CB_RANK_OPEN",
    "CB_RANK_CAT",
    "CAT_SERVICOS",
    "CAT_RECARGAS",
    "CAT_COMPRAS",
    "CAT_GIFTS",
    "CAT_SALDO",
    "CATEGORIES",
    "DEFAULT_CATEGORY",
]
