"""
Larizinha Store — Handler Meu Perfil
Implementa:
- 👤 Meu perfil (info + movimentações reais)
- 📜 Histórico de Compras (paginação 1/2, 2/2 + ✅ Apenas Ativas + ⬅️ Anterior + Próxima ➡️)
- 🎁 Resgatar Gift Card (Reply + ❌ Cancelar)
- ✏️ Alterar Dados (📱 WhatsApp com validação real 10–13 dígitos)

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Inputs via Reply / ForceReply com one_time_keyboard, resize, selective.
- Apaga a mensagem do usuário após capturar o input (segurança).
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Estados guardados em FlowState (Redis) com TTL.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

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
    GiftCard,
    GiftCardRedemption,
    Order,
    OrderStatus,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import audit, log_payment, logger
from bot.redis_client import FlowState, Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from bot.keyboards.reply import build_cancel_reply, build_reply_keyboard_remove


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="profile")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PERFIL = "perfil"
MSG_KEY_HIST_VAZIO = "historico_vazio"
MSG_KEY_HIST_CHEIO = "historico_cheio"
MSG_KEY_GIFT_PROMPT = "gift_prompt"
MSG_KEY_GIFT_OK = "gift_ok"
MSG_KEY_GIFT_ERRO = "gift_erro"
MSG_KEY_ALTERAR = "alterar_dados"
MSG_KEY_WHATS_PROMPT = "whatsapp_prompt"
MSG_KEY_WHATS_OK = "whatsapp_ok"
MSG_KEY_WHATS_ERRO = "whatsapp_erro"

SCREEN_PERFIL = "perfil"
SCREEN_HISTORICO = "historico"
SCREEN_GIFT = "gift"
SCREEN_ALTERAR = "alterar_dados"
SCREEN_WHATSAPP = "whatsapp"

# Callbacks
CB_OPEN_PROFILE = "profile:open"
CB_HIST = "profile:hist"           # + :page:N
CB_HIST_FILTER = "profile:histfilter"  # :all | :active
CB_GIFT = "profile:gift"
CB_EDIT = "profile:edit"
CB_EDIT_WHATS = "profile:editwhats"

# Fluxos
FLOW_GIFT = "gift"
FLOW_WHATS = "whatsapp"

WHATS_RE = re.compile(r"^\d{10,13}$")

DEFAULT_PAGE_SIZE = 5


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


async def _reload_user(user_id: int) -> Optional[User]:
    """Recarrega o usuário do banco (fresh)."""
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


# ============================================================
# RENDER DO PERFIL
# ============================================================
async def _render_profile(user: User) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Renderiza o perfil com dados REAIS:
    - ID, saldo, whatsapp (azul/negrito)
    - Compras, gasto, PIX inseridos, gifts
    """
    # Whatsapp em azul/negrito (link wa.me silencioso)
    whats = user.whatsapp or ""
    if whats:
        whats_display = f'<a href="https://wa.me/{whats}"><b>{whats}</b></a>'
    else:
        whats_display = "<i>não cadastrado</i>"

    rendered = await message_engine.render(
        MSG_KEY_PERFIL,
        fallback=(
            "👤 <b>Meu perfil</b>\n\n"
            "🔍 Veja aqui os detalhes da sua conta:\n\n"
            "- 👤 <b>Informações:</b>\n"
            f"🆔 ID da Carteira: <code>{user.id}</code>\n"
            f"💰 Saldo Atual: <b>{_fmt_brl(float(user.balance or 0))}</b>\n"
            f"📲 Seu WhatsApp: {whats_display}\n\n"
            "─── 📊 <b>Suas Movimentações:</b>\n"
            f"ー 🛒 Compras Realizadas: {int(user.purchases_count or 0)}\n"
            f"ー 💰 Total Gasto Em Compras: {_fmt_brl(float(user.total_spent or 0))}\n"
            f"ー 💠 PIX Inseridos: {_fmt_brl(float(user.total_deposited or 0))}\n"
            f"ー 🎁 Gifts Resgatados: {_fmt_brl(float(user.total_gifts or 0))}"
        ),
        id=user.id,
        telegram_id=user.telegram_id,
        nome=user.first_name or "Cliente",
        saldo=float(user.balance or 0),
        whatsapp=whats or "não cadastrado",
        whatsapp_display=whats_display,
        compras=int(user.purchases_count or 0),
        gasto=float(user.total_spent or 0),
        pix_in=float(user.total_deposited or 0),
        gifts=float(user.total_gifts or 0),
    )

    keyboard = await button_engine.inline(SCREEN_PERFIL)
    if keyboard is None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📜 Histórico de Compras", callback_data=f"{CB_HIST}:page:1")],
                [InlineKeyboardButton(text="🎁 Resgatar Gift Card", callback_data=CB_GIFT)],
                [InlineKeyboardButton(text="✏️ Alterar Dados", callback_data=CB_EDIT)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")],
            ]
        )

    return rendered.content, keyboard


# ============================================================
# HANDLER: ABRIR PERFIL (profile:open)
# ============================================================
@router.callback_query(F.data == CB_OPEN_PROFILE)
async def on_open_profile(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "profile", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        text, keyboard = await _render_profile(fresh)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_open_profile falhou: {exc}")


# ============================================================
# HISTÓRICO DE COMPRAS (paginação)
# ============================================================
async def _count_orders(user_id: int, only_active: bool) -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Order.id)).where(Order.user_id == user_id)
            if only_active:
                stmt = stmt.where(Order.status == OrderStatus.DELIVERED)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_orders_page(
    user_id: int, page: int, per_page: int, only_active: bool
) -> List[Order]:
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            stmt = (
                select(Order)
                .where(Order.user_id == user_id)
                .order_by(Order.created_at.desc())
                .offset(offset)
                .limit(per_page)
            )
            if only_active:
                stmt = stmt.where(Order.status == OrderStatus.DELIVERED)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _render_history(
    user_id: int, page: int, only_active: bool
) -> tuple[str, InlineKeyboardMarkup]:
    per_page = DEFAULT_PAGE_SIZE
    total = await _count_orders(user_id, only_active)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))

    orders = await _load_orders_page(user_id, page, per_page, only_active)

    if total == 0:
        rendered = await message_engine.render(
            MSG_KEY_HIST_VAZIO,
            fallback=(
                "📜 <b>Histórico de Compras</b>\n\n"
                "<i>Você ainda não possui compras registradas.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OPEN_PROFILE)],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["📜 <b>Histórico de Compras</b>", ""]
    for o in orders:
        data = _fmt_dt(o.created_at)
        status_emoji = "🟢" if o.status == OrderStatus.DELIVERED else "⚪️"
        linhas.append(
            f"{status_emoji} <b>#{o.id}</b> · {_fmt_brl(float(o.final_total or 0))}\n"
            f"    <i>{data}</i>"
        )

    rendered = await message_engine.render(
        MSG_KEY_HIST_CHEIO,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        pagina=page,
        total_paginas=total_pages,
        total=total,
    )

    # ---------- Paginação ----------
    rows: List[List[InlineKeyboardButton]] = []

    # Filtro "✅ Apenas Ativas"
    if only_active:
        filter_btn = InlineKeyboardButton(
            text="✅ Apenas Ativas",
            callback_data=f"{CB_HIST_FILTER}:all",
        )
    else:
        filter_btn = InlineKeyboardButton(
            text="✅ Apenas Ativas",
            callback_data=f"{CB_HIST_FILTER}:active",
        )
    rows.append([filter_btn])

    # Navegação
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Anterior",
                callback_data=f"{CB_HIST}:page:{page - 1}:active:{int(only_active)}",
            )
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Próxima ➡️",
                callback_data=f"{CB_HIST}:page:{page + 1}:active:{int(only_active)}",
            )
        )
    if nav:
        rows.append(nav)

    # Voltar
    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OPEN_PROFILE)]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(f"{CB_HIST}:page:"))
async def on_history(callback: CallbackQuery, user: User) -> None:
    """
    Formato: profile:hist:page:N:active:0|1
    """
    try:
        parts = callback.data.split(":")
        page = 1
        only_active = False
        try:
            # parts = [profile, hist, page, N, active, B]
            if len(parts) >= 4:
                page = int(parts[3])
            if len(parts) >= 6:
                only_active = parts[5] == "1"
        except Exception:
            page = 1

        text, kb = await _render_history(user.id, page, only_active)

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
        logger.exception(f"on_history falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_HIST_FILTER}:"))
async def on_history_filter(callback: CallbackQuery, user: User) -> None:
    try:
        parts = callback.data.split(":")
        only_active = False
        if len(parts) >= 2:
            only_active = parts[1] == "active"

        text, kb = await _render_history(user.id, 1, only_active)

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
        logger.exception(f"on_history_filter falhou: {exc}")


# ============================================================
# GIFT CARD
# ============================================================
@router.callback_query(F.data == CB_GIFT)
async def on_gift_start(callback: CallbackQuery, user: User) -> None:
    try:
        rendered = await message_engine.render(
            MSG_KEY_GIFT_PROMPT,
            fallback=(
                "🎁 <b>Resgatar Gift Card</b>\n\n"
                "Envie o código do seu Gift Card abaixo.\n"
                "💡 Digite /cancelar para sair."
            ),
            nome=user.first_name or "Cliente",
        )

        # Estado do fluxo
        await FlowState.set(
            user_id=user.id,
            name=FLOW_GIFT,
            data={"chat_id": callback.message.chat.id if callback.message else user.telegram_id},
            ttl=10 * 60,
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
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text="🎁 Envie o código do Gift Card:",
                    reply_markup=build_cancel_reply(placeholder="Cole o código aqui..."),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_gift_start falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_gift_or_whats_message(message: Message, user: User) -> None:
    """
    Roteia mensagens de texto para:
      - Fluxo de Gift Card (se ativo)
      - Fluxo de Alterar WhatsApp (se ativo)
    Caso contrário, ignora (outros handlers cuidam).
    """
    # ---------------- GIFT ----------------
    gift_state = await FlowState.get(user.id, FLOW_GIFT)
    if gift_state:
        await _handle_gift_code(message, user)
        return

    # ---------------- WHATSAPP ----------------
    whats_state = await FlowState.get(user.id, FLOW_WHATS)
    if whats_state:
        await _handle_whatsapp_input(message, user)
        return


async def _handle_gift_code(message: Message, user: User) -> None:
    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_GIFT)
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.answer(
                "✅ Operação cancelada.",
                reply_markup=build_reply_keyboard_remove(),
            )
        except Exception:
            pass
        return

    # Apaga a mensagem do usuário
    try:
        await message.delete()
    except Exception:
        pass

    code = raw.upper()
    if not code:
        await message.answer("❌ Código inválido.")
        return

    # Busca gift card
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(GiftCard).where(GiftCard.code == code)
            )
            gift: Optional[GiftCard] = res.scalar_one_or_none()
    except Exception:
        gift = None

    if gift is None or not gift.active:
        rendered = await message_engine.render(
            MSG_KEY_GIFT_ERRO,
            fallback="❌ <b>Gift Card inválido.</b>\n\nVerifique o código e tente novamente.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_reply_keyboard_remove(),
        )
        await FlowState.clear(user.id, FLOW_GIFT)
        return

    # Checa validade
    if gift.expires_at is not None and gift.expires_at < datetime.now(timezone.utc):
        rendered = await message_engine.render(
            MSG_KEY_GIFT_ERRO,
            fallback="❌ Este Gift Card está <b>expirado</b>.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_reply_keyboard_remove(),
        )
        await FlowState.clear(user.id, FLOW_GIFT)
        return

    # Checa usos
    if int(gift.uses_max or 0) > 0 and int(gift.uses_count or 0) >= int(gift.uses_max):
        rendered = await message_engine.render(
            MSG_KEY_GIFT_ERRO,
            fallback="❌ Este Gift Card já foi <b>totalmente utilizado</b>.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_reply_keyboard_remove(),
        )
        await FlowState.clear(user.id, FLOW_GIFT)
        return

    # Já resgatado pelo usuário?
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(GiftCardRedemption.id))
                .where(GiftCardRedemption.gift_card_id == gift.id)
                .where(GiftCardRedemption.user_id == user.id)
            )
            already = int(res.scalar() or 0)
    except Exception:
        already = 0

    if already > 0:
        rendered = await message_engine.render(
            MSG_KEY_GIFT_ERRO,
            fallback="❌ Você já resgatou este Gift Card.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_reply_keyboard_remove(),
        )
        await FlowState.clear(user.id, FLOW_GIFT)
        return

    # ---------- Aplica crédito ----------
    valor = float(gift.amount or 0)
    try:
        async with session_scope() as session:
            # recarrega com lock
            res = await session.execute(
                select(User).where(User.id == user.id).with_for_update()
            )
            u: Optional[User] = res.scalar_one_or_none()
            if u is None:
                return

            new_balance = round(float(u.balance or 0) + valor, 2)
            u.balance = Decimal(str(new_balance))
            u.total_gifts = Decimal(
                str(round(float(u.total_gifts or 0) + valor, 2))
            )
            session.add(u)

            # Transaction
            session.add(
                Transaction(
                    user_id=u.id,
                    type=TransactionType.GIFT,
                    amount=Decimal(str(round(valor, 2))),
                    balance_after=Decimal(str(new_balance)),
                    description=f"Gift Card resgatado ({gift.code})",
                    reference=gift.code,
                )
            )

            # Redemption
            session.add(
                GiftCardRedemption(
                    gift_card_id=gift.id,
                    user_id=u.id,
                    amount=Decimal(str(round(valor, 2))),
                )
            )

            # Incrementa usos
            gres = await session.execute(
                select(GiftCard).where(GiftCard.id == gift.id).with_for_update()
            )
            g: Optional[GiftCard] = gres.scalar_one_or_none()
            if g is not None:
                g.uses_count = int(g.uses_count or 0) + 1
                session.add(g)
    except Exception as exc:
        logger.exception(f"gift redemption falhou: {exc}")
        await message.answer("❌ Erro ao resgatar Gift Card.")
        return

    # Mensagem de sucesso (editável)
    rendered = await message_engine.render(
        MSG_KEY_GIFT_OK,
        fallback=(
            "✅ <b>Gift Card resgatado!</b>\n\n"
            f"💵 Valor creditado: <b>{_fmt_brl(valor)}</b>\n"
            f"💰 Novo saldo: <b>{_fmt_brl(float(u.balance or 0))}</b>"
        ),
        valor=valor,
        saldo=float(u.balance or 0),
        code=gift.code,
    )
    await message.answer(
        text=rendered.content,
        parse_mode="HTML",
        reply_markup=build_reply_keyboard_remove(),
    )

    await FlowState.clear(user.id, FLOW_GIFT)

    log_payment(
        "gift.redeemed",
        user_id=user.id,
        amount=valor,
        status="paid",
        details={"code": gift.code},
    )


# ============================================================
# ALTERAR DADOS
# ============================================================
@router.callback_query(F.data == CB_EDIT)
async def on_edit_data(callback: CallbackQuery, user: User) -> None:
    try:
        fresh = await _reload_user(user.id) or user
        whats = fresh.whatsapp or "não cadastrado"
        rendered = await message_engine.render(
            MSG_KEY_ALTERAR,
            fallback=(
                "✏️ <b>Alterar Dados</b>\n\n"
                f"📱 WhatsApp atual: <b>{whats}</b>\n\n"
                "Escolha o que deseja alterar:"
            ),
            whatsapp=whats,
        )

        kb = await button_engine.inline(SCREEN_ALTERAR)
        if kb is None:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📱 WhatsApp", callback_data=CB_EDIT_WHATS)],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OPEN_PROFILE)],
                ]
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
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_edit_data falhou: {exc}")


@router.callback_query(F.data == CB_EDIT_WHATS)
async def on_edit_whats(callback: CallbackQuery, user: User) -> None:
    try:
        rendered = await message_engine.render(
            MSG_KEY_WHATS_PROMPT,
            fallback=(
                "📱 <b>Alterar WhatsApp</b>\n\n"
                "Envie seu número com DDD, <b>apenas números</b>.\n"
                "Exemplo: <code>11987654321</code>\n\n"
                "⚠️ A mensagem digitada será apagada automaticamente."
            ),
        )

        await FlowState.set(
            user_id=user.id,
            name=FLOW_WHATS,
            data={"chat_id": callback.message.chat.id if callback.message else user.telegram_id},
            ttl=10 * 60,
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
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text="📱 Digite seu WhatsApp (só números):",
                    reply_markup=build_cancel_reply(placeholder="DDD + número"),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_edit_whats falhou: {exc}")


async def _handle_whatsapp_input(message: Message, user: User) -> None:
    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_WHATS)
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.answer(
                "✅ Operação cancelada.",
                reply_markup=build_reply_keyboard_remove(),
            )
        except Exception:
            pass
        return

    # Sanitiza
    digits = re.sub(r"\D", "", raw)

    try:
        await message.delete()
    except Exception:
        pass

    if not WHATS_RE.match(digits):
        rendered = await message_engine.render(
            MSG_KEY_WHATS_ERRO,
            fallback=(
                "❌ <b>Número inválido!</b>\n\n"
                "Envie apenas números com DDD (10 a 13 dígitos)."
            ),
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_reply_keyboard_remove(),
        )
        # mantém fluxo ativo para o usuário tentar novamente
        return

    # Atualiza no banco
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.id == user.id)
            )
            u: Optional[User] = res.scalar_one_or_none()
            if u is None:
                return
            u.whatsapp = digits
            session.add(u)
    except Exception as exc:
        logger.exception(f"update whatsapp falhou: {exc}")
        await message.answer("❌ Erro ao salvar. Tente novamente.")
        return

    rendered = await message_engine.render(
        MSG_KEY_WHATS_OK,
        fallback=(
            "✅ <b>WhatsApp atualizado!</b>\n\n"
            f"📱 Novo número: <b>{digits}</b>"
        ),
        whatsapp=digits,
    )
    await message.answer(
        text=rendered.content,
        parse_mode="HTML",
        reply_markup=build_reply_keyboard_remove(),
    )

    await FlowState.clear(user.id, FLOW_WHATS)

    audit(
        action="user.whatsapp.updated",
        user_id=user.id,
        details={"whatsapp": digits},
    )


# ============================================================
# /cancelar — durante fluxos do perfil
# ============================================================
@router.message(Command("cancelar"))
async def on_cancel_profile(message: Message, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_GIFT)
        await FlowState.clear(user.id, FLOW_WHATS)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
    except Exception:
        pass


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_profile",
    "on_history",
    "on_history_filter",
    "on_gift_start",
    "on_edit_data",
    "on_edit_whats",
    "FLOW_GIFT",
    "FLOW_WHATS",
]
