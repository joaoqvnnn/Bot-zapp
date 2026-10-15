"""
Larizinha Store — Handler de Tickets e Recompensas
Implementa o sistema de tickets com sorteio REAL por probabilidades:

    1) Vitrine de tickets (key="tickets_vitrine")
       • Lista os tickets ativos (nome, raridade, preço, estoque)
       • Botões: [🎫 Comprar Ticket <nome>] por ticket + [🔙 Voltar]

    2) Confirmação (key="ticket_confirmar")
       • Mostra preço, saldo e raridade
       • Botões: [✅ Comprar] [❌ Cancelar]

    3) Compra:
       • Debita saldo (atômico)
       • Sorteia prêmio pelas probabilidades (odds)
       • Credita prêmio automaticamente
       • Registra UserTicket + Transaction
       • Edita para key="ticket_resultado" com prêmio + botões:
           [🎁 Ver Meus Tickets] [🎫 Comprar Novamente] [🔙 Voltar]

    4) Meus Tickets (key="tickets_meus")
       • Histórico dos últimos tickets comprados e prêmios ganhos
       • Paginação [⬅️ Anterior] [Próxima ➡️]

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Estoque de prêmios respeitado (rewards[i].stock).
- Probabilidades normalizadas (soma = 100).
- Se sem estoque de tickets/prêmios → mensagens editáveis específicas.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

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
    Ticket,
    Transaction,
    TransactionType,
    User,
    UserTicket,
    session_scope,
)
from bot.logger import audit, log_payment, logger
from bot.redis_client import LockManager, Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="tickets")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_VITRINE = "tickets_vitrine"
MSG_KEY_CONFIRMAR = "ticket_confirmar"
MSG_KEY_RESULTADO = "ticket_resultado"
MSG_KEY_MEUS = "tickets_meus"
MSG_KEY_SEM_TICKETS = "tickets_sem"
MSG_KEY_SALDO_INSUF = "ticket_saldo_insuf"
MSG_KEY_ESGOTADO = "ticket_esgotado"
MSG_KEY_SEM_PREMIOS = "ticket_sem_premios"

SCREEN_VITRINE = "tickets_vitrine"
SCREEN_CONFIRMAR = "ticket_confirmar"
SCREEN_RESULTADO = "ticket_resultado"
SCREEN_MEUS = "tickets_meus"

# Callbacks
CB_TICKETS_OPEN = "tickets:open"
CB_TICKETS_MEUS = "tickets:meus"           # tickets:meus:page:N
CB_TICKETS_BUY = "tickets:buy"             # tickets:buy:ID
CB_TICKETS_CONFIRM = "tickets:confirm"     # tickets:confirm:ID
CB_TICKETS_CANCEL = "tickets:cancel"
CB_TICKETS_AGAIN = "tickets:again"         # tickets:again:ID

PAGE_SIZE = 5


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    try:
        return dt.astimezone().strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return dt.strftime("%d/%m/%Y às %H:%M")


def _rarity_emoji(rarity: str) -> str:
    mapping = {
        "comum": "⚪️",
        "raro": "🔵",
        "épico": "🟣",
        "epico": "🟣",
        "lendário": "🟡",
        "lendario": "🟡",
        "mítico": "🔴",
        "mitico": "🔴",
    }
    return mapping.get((rarity or "").strip().lower(), "🎫")


async def _reload_user(user_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_ticket(ticket_id: int) -> Optional[Ticket]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Ticket).where(Ticket.id == ticket_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _list_active_tickets() -> List[Ticket]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Ticket)
                .where(Ticket.active == True)  # noqa: E712
                .order_by(Ticket.price.asc(), Ticket.id.asc())
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_user_tickets(user_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(UserTicket.id)).where(UserTicket.user_id == user_id)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_user_tickets_page(
    user_id: int, page: int, per_page: int = PAGE_SIZE
) -> List[UserTicket]:
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(UserTicket)
                .where(UserTicket.user_id == user_id)
                .order_by(UserTicket.created_at.desc())
                .offset(offset)
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# 🎲 SORTEIO REAL POR PROBABILIDADES
# ============================================================
def _pick_reward(ticket: Ticket) -> Optional[Dict[str, Any]]:
    """
    Sorteia um prêmio a partir de `Ticket.rewards` (lista) e `Ticket.odds`
    (dict por índice).

    Formato esperado de `rewards`:
      [
        {"name": "Comum", "amount": 5.00, "stock": 100, "type": "balance"},
        {"name": "Raro",  "amount": 20.00, "stock": 10, "type": "balance"},
        ...
      ]

    Formato esperado de `odds`:
      {"0": 60, "1": 25, "2": 10, "3": 5}   # percentuais por índice
      (ou com chaves pelo nome do prêmio)

    Retorna o prêmio escolhido (dict) ou None se sem estoque.
    """
    rewards = ticket.rewards or []
    if not isinstance(rewards, list) or not rewards:
        return None

    odds = ticket.odds or {}
    if not isinstance(odds, dict):
        odds = {}

    # ---------- Normaliza pesos ----------
    entries: List[Tuple[int, Dict[str, Any], float]] = []
    for idx, r in enumerate(rewards):
        if not isinstance(r, dict):
            continue
        # Estoque
        stock = r.get("stock")
        if stock is not None:
            try:
                if int(stock) <= 0:
                    continue
            except Exception:
                pass

        # Peso
        weight = None
        # tenta por índice (string ou int)
        for k in (str(idx), idx):
            if k in odds:
                try:
                    weight = float(odds[k])
                    break
                except Exception:
                    pass
        # tenta por nome
        if weight is None:
            name = str(r.get("name") or "").strip()
            if name and name in odds:
                try:
                    weight = float(odds[name])
                except Exception:
                    weight = None

        if weight is None or weight <= 0:
            weight = 1.0  # fallback mínimo

        entries.append((idx, r, weight))

    if not entries:
        return None

    total_weight = sum(w for _, _, w in entries)
    if total_weight <= 0:
        return None

    pick = random.uniform(0, total_weight)
    upto = 0.0
    chosen: Optional[Dict[str, Any]] = None
    chosen_idx: Optional[int] = None
    for idx, r, w in entries:
        upto += w
        if pick <= upto:
            chosen = dict(r)
            chosen_idx = idx
            break

    if chosen is None:
        # fallback seguro
        idx, r, _ = entries[-1]
        chosen = dict(r)
        chosen_idx = idx

    # ---------- Decrementa estoque ----------
    if chosen_idx is not None and chosen.get("stock") is not None:
        try:
            new_stock = max(0, int(chosen.get("stock")) - 1)
            chosen["stock"] = new_stock
            rewards[chosen_idx] = chosen
            ticket.rewards = list(rewards)
        except Exception:
            pass

    return chosen


# ============================================================
# TELA — VITRINE DE TICKETS
# ============================================================
async def _render_vitrine(user: User) -> Tuple[str, InlineKeyboardMarkup]:
    tickets = await _list_active_tickets()

    if not tickets:
        rendered = await message_engine.render(
            MSG_KEY_SEM_TICKETS,
            fallback=(
                "🎫 <b>Tickets e Recompensas</b>\n\n"
                "<i>Nenhum ticket disponível no momento.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["🎫 <b>Tickets e Recompensas</b>", ""]
    for t in tickets:
        emoji = _rarity_emoji(t.rarity)
        preco = float(t.price or 0)
        estoque = int(t.stock or 0)
        linhas.append(
            f"{emoji} <b>{t.name}</b> · <i>{t.rarity}</i>\n"
            f"    💵 {_fmt_brl(preco)} · 📦 Estoque: {estoque}"
        )
    linhas.append("")
    linhas.append(f"💰 Seu saldo: <b>{_fmt_brl(float(user.balance or 0))}</b>")

    rendered = await message_engine.render(
        MSG_KEY_VITRINE,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        saldo=float(user.balance or 0),
    )

    # Teclado
    kb = await button_engine.inline(SCREEN_VITRINE)
    if kb is None:
        rows: List[List[InlineKeyboardButton]] = []
        for t in tickets:
            emoji = _rarity_emoji(t.rarity)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{emoji} Comprar {t.name} · {_fmt_brl(float(t.price or 0))}",
                        callback_data=f"{CB_TICKETS_BUY}:{t.id}",
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton(text="🎁 Meus Tickets", callback_data=f"{CB_TICKETS_MEUS}:page:1")]
        )
        rows.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
        )
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    return rendered.content, kb


@router.callback_query(F.data == CB_TICKETS_OPEN)
async def on_open_tickets(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "tickets", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)

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
        logger.exception(f"on_open_tickets falhou: {exc}")


# ============================================================
# TELA — CONFIRMAÇÃO
# ============================================================
async def _render_confirm(user: User, ticket: Ticket) -> Tuple[str, InlineKeyboardMarkup]:
    preco = float(ticket.price or 0)
    emoji = _rarity_emoji(ticket.rarity)

    rendered = await message_engine.render(
        MSG_KEY_CONFIRMAR,
        fallback=(
            "🎫 <b>Confirmar compra de Ticket</b>\n\n"
            f"{emoji} <b>{ticket.name}</b> · <i>{ticket.rarity}</i>\n"
            f"💵 Preço: <b>{_fmt_brl(preco)}</b>\n"
            f"💰 Seu saldo: <b>{_fmt_brl(float(user.balance or 0))}</b>\n\n"
            "🎁 Prêmios podem variar de acordo com a raridade.\n"
            "Ao confirmar, o saldo será debitado e um prêmio será sorteado."
        ),
        nome=ticket.name,
        raridade=ticket.rarity,
        preco=preco,
        saldo=float(user.balance or 0),
        emoji=emoji,
    )

    kb = await button_engine.inline(SCREEN_CONFIRMAR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Comprar",
                        callback_data=f"{CB_TICKETS_CONFIRM}:{ticket.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data=CB_TICKETS_CANCEL,
                    ),
                ]
            ]
        )
    return rendered.content, kb


@router.callback_query(F.data.startswith(f"{CB_TICKETS_BUY}:"))
async def on_buy_ticket(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            ticket_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Ticket inválido.", show_alert=True)
            return

        ticket = await _load_ticket(ticket_id)
        if ticket is None or not ticket.active:
            await callback.answer("❌ Ticket indisponível.", show_alert=True)
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_confirm(fresh, ticket)

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
        logger.exception(f"on_buy_ticket falhou: {exc}")


# ============================================================
# HANDLER — CANCELAR
# ============================================================
@router.callback_query(F.data == CB_TICKETS_CANCEL)
async def on_cancel_ticket(callback: CallbackQuery, user: User) -> None:
    try:
        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)

        try:
            await callback.answer("❌ Cancelado.")
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
                pass
    except Exception as exc:
        logger.exception(f"on_cancel_ticket falhou: {exc}")


# ============================================================
# HANDLER — CONFIRMAR (executa a compra)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TICKETS_CONFIRM}:"))
async def on_confirm_ticket(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            ticket_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Ticket inválido.", show_alert=True)
            return

        # Trava de ação (evita duplo clique)
        if not await LockManager.acquire_action(user.id, f"ticket:{ticket_id}", ttl=15):
            await callback.answer("⏳ Processando...", show_alert=False)
            return

        try:
            # Carrega ticket
            async with session_scope() as session:
                res = await session.execute(
                    select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                )
                ticket: Optional[Ticket] = res.scalar_one_or_none()
                if ticket is None or not ticket.active:
                    await callback.answer("❌ Ticket indisponível.", show_alert=True)
                    return

                # Estoque geral
                if int(ticket.stock or 0) <= 0:
                    rendered = await message_engine.render(
                        MSG_KEY_ESGOTADO,
                        fallback=(
                            "❌ <b>Ticket esgotado</b>\n\n"
                            "Todos os tickets deste tipo já foram vendidos."
                        ),
                        nome=ticket.name,
                    )
                    try:
                        await callback.answer()
                    except Exception:
                        pass
                    if callback.message:
                        try:
                            await callback.message.edit_text(
                                text=rendered.content,
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_TICKETS_OPEN,
                                            )
                                        ]
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                preco = float(ticket.price or 0)

                # Carrega user com lock
                ures = await session.execute(
                    select(User).where(User.id == user.id).with_for_update()
                )
                u: Optional[User] = ures.scalar_one_or_none()
                if u is None:
                    await callback.answer("❌ Usuário não encontrado.", show_alert=True)
                    return

                if float(u.balance or 0) < preco:
                    rendered = await message_engine.render(
                        MSG_KEY_SALDO_INSUF,
                        fallback=(
                            "❌ <b>Saldo insuficiente</b>\n\n"
                            f"💰 Seu saldo: {_fmt_brl(float(u.balance or 0))}\n"
                            f"💵 Preço do ticket: {_fmt_brl(preco)}"
                        ),
                        saldo=float(u.balance or 0),
                        preco=preco,
                    )
                    try:
                        await callback.answer()
                    except Exception:
                        pass
                    if callback.message:
                        try:
                            await callback.message.edit_text(
                                text=rendered.content,
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="💠 Recarregar",
                                                callback_data="recharge:open",
                                            )
                                        ],
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_TICKETS_OPEN,
                                            )
                                        ],
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                # Sorteia prêmio
                reward = _pick_reward(ticket)
                if reward is None:
                    rendered = await message_engine.render(
                        MSG_KEY_SEM_PREMIOS,
                        fallback="❌ Este ticket está sem prêmios disponíveis.",
                    )
                    try:
                        await callback.answer()
                    except Exception:
                        pass
                    if callback.message:
                        try:
                            await callback.message.edit_text(
                                text=rendered.content,
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_TICKETS_OPEN,
                                            )
                                        ]
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                # Debita saldo
                new_balance = round(float(u.balance or 0) - preco, 2)
                u.balance = Decimal(str(new_balance))
                session.add(u)

                # Prêmio (padrão: amount em saldo)
                reward_amount = float(reward.get("amount") or 0)
                reward_type = str(reward.get("type") or "balance").lower()
                reward_name = str(reward.get("name") or "Prêmio")

                # Credita prêmio em saldo
                if reward_type == "balance" and reward_amount > 0:
                    new_balance = round(float(u.balance) + reward_amount, 2)
                    u.balance = Decimal(str(new_balance))
                    session.add(u)

                    session.add(
                        Transaction(
                            user_id=u.id,
                            type=TransactionType.BONUS,
                            amount=Decimal(str(round(reward_amount, 2))),
                            balance_after=Decimal(str(new_balance)),
                            description=f"Prêmio ticket: {reward_name}",
                            reference=f"ticket:{ticket.id}",
                        )
                    )

                # Transaction do gasto
                session.add(
                    Transaction(
                        user_id=u.id,
                        type=TransactionType.PURCHASE,
                        amount=Decimal(str(round(preco, 2))),
                        balance_after=Decimal(str(new_balance)),
                        description=f"Compra ticket: {ticket.name}",
                        reference=f"ticket:{ticket.id}",
                    )
                )

                # Decrementa estoque do ticket
                ticket.stock = max(0, int(ticket.stock or 0) - 1)
                session.add(ticket)

                # Registra UserTicket
                ut = UserTicket(
                    ticket_id=ticket.id,
                    user_id=u.id,
                    reward={
                        "name": reward_name,
                        "amount": reward_amount,
                        "type": reward_type,
                        **{k: v for k, v in reward.items() if k not in ("name", "amount", "type")},
                    },
                )
                session.add(ut)
                await session.flush()

                final_balance = new_balance
                ticket_name = ticket.name
                ticket_rarity = ticket.rarity
                ticket_id_local = int(ticket.id)

            # ---------- TELA DE RESULTADO ----------
            emoji = _rarity_emoji(ticket_rarity)
            rendered = await message_engine.render(
                MSG_KEY_RESULTADO,
                fallback=(
                    "🎉 <b>Parabéns!</b>\n\n"
                    f"{emoji} Ticket: <b>{ticket_name}</b>\n"
                    f"🎁 Prêmio: <b>{reward_name}</b>\n"
                    f"💵 Valor creditado: <b>{_fmt_brl(reward_amount)}</b>\n"
                    f"💰 Novo saldo: <b>{_fmt_brl(final_balance)}</b>"
                ),
                ticket=ticket_name,
                raridade=ticket_rarity,
                premio=reward_name,
                valor=reward_amount,
                saldo=final_balance,
                emoji=emoji,
            )

            kb = await button_engine.inline(SCREEN_RESULTADO)
            if kb is None:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎁 Ver Meus Tickets",
                                callback_data=f"{CB_TICKETS_MEUS}:page:1",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🎫 Comprar Novamente",
                                callback_data=f"{CB_TICKETS_AGAIN}:{ticket_id_local}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔙 Voltar",
                                callback_data=CB_TICKETS_OPEN,
                            )
                        ],
                    ]
                )

            try:
                await callback.answer("🎉 Boa sorte!")
            except Exception:
                pass

            if callback.message:
                try:
                    await callback.message.edit_text(
                        text=rendered.content,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    await callback.message.answer(
                        text=rendered.content,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )

            log_payment(
                "ticket.purchased",
                user_id=user.id,
                amount=preco,
                status="paid",
                details={
                    "ticket_id": ticket_id_local,
                    "reward": reward_name,
                    "reward_amount": reward_amount,
                },
            )
            audit(
                action="ticket.purchased",
                user_id=user.id,
                details={"ticket_id": ticket_id_local},
            )
        finally:
            await LockManager.release_action(user.id, f"ticket:{ticket_id}")
    except Exception as exc:
        logger.exception(f"on_confirm_ticket falhou: {exc}")
        try:
            await callback.answer("❌ Erro ao processar ticket.", show_alert=True)
        except Exception:
            pass


# ============================================================
# HANDLER — COMPRAR NOVAMENTE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TICKETS_AGAIN}:"))
async def on_buy_again(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            ticket_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Ticket inválido.", show_alert=True)
            return

        ticket = await _load_ticket(ticket_id)
        if ticket is None or not ticket.active:
            await callback.answer("❌ Ticket indisponível.", show_alert=True)
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_confirm(fresh, ticket)

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
                pass
    except Exception as exc:
        logger.exception(f"on_buy_again falhou: {exc}")


# ============================================================
# MEUS TICKETS (histórico com paginação)
# ============================================================
async def _render_my_tickets(user: User, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    total = await _count_user_tickets(user.id)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))

    tickets = await _load_user_tickets_page(user.id, page, PAGE_SIZE)

    if total == 0:
        rendered = await message_engine.render(
            MSG_KEY_MEUS,
            fallback=(
                "🎁 <b>Meus Tickets</b>\n\n"
                "<i>Você ainda não comprou tickets.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎫 Comprar Ticket", callback_data=CB_TICKETS_OPEN)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_TICKETS_OPEN)],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["🎁 <b>Meus Tickets</b>", ""]
    for ut in tickets:
        reward = ut.reward or {}
        rname = reward.get("name") if isinstance(reward, dict) else "Prêmio"
        ramount = reward.get("amount") if isinstance(reward, dict) else 0
        try:
            ramount_f = float(ramount or 0)
        except Exception:
            ramount_f = 0.0

        linhas.append(
            f"🎫 #{ut.id} · <b>{rname}</b>\n"
            f"    💵 {_fmt_brl(ramount_f)} · {_fmt_dt(ut.created_at)}"
        )

    rendered = await message_engine.render(
        MSG_KEY_MEUS,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        pagina=page,
        total_paginas=total_pages,
        total=total,
    )

    rows: List[List[InlineKeyboardButton]] = []
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Anterior",
                callback_data=f"{CB_TICKETS_MEUS}:page:{page - 1}",
            )
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Próxima ➡️",
                callback_data=f"{CB_TICKETS_MEUS}:page:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [InlineKeyboardButton(text="🎫 Comprar Ticket", callback_data=CB_TICKETS_OPEN)]
    )
    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_TICKETS_OPEN)]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(f"{CB_TICKETS_MEUS}:page:"))
async def on_my_tickets(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_my_tickets(fresh, page)

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
                pass
    except Exception as exc:
        logger.exception(f"on_my_tickets falhou: {exc}")


# ============================================================
# COMANDO /tickets
# ============================================================
@router.message(Command("tickets"))
async def on_cmd_tickets(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/tickets falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_tickets",
    "on_buy_ticket",
    "on_cancel_ticket",
    "on_confirm_ticket",
    "on_buy_again",
    "on_my_tickets",
    "on_cmd_tickets",
    "CB_TICKETS_OPEN",
    "CB_TICKETS_MEUS",
    "CB_TICKETS_BUY",
    "CB_TICKETS_CONFIRM",
    "CB_TICKETS_CANCEL",
    "CB_TICKETS_AGAIN",
    "_pick_reward",
]
