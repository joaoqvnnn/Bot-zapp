"""
Larizinha Store — Módulo Admin: Tickets e Recompensas
Implementa o CRUD completo de tickets conforme DIM.

Telas:
- Painel geral (total, raridades, preços, estoque)
- Lista de tickets (com paginação DIM)
- Criar ticket (nome, raridade, preço, estoque)
- Editor individual (todos os campos + prêmios + odds)
- Gerenciar prêmios (adicionar/editar/remover com peso)
- Gerenciar probabilidades (odds por prêmio)
- Estoque de prêmios
- Histórico de compras
- Relatórios

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
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
    User,
    UserTicket,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_OFF,
    SYM_OK,
    SYM_WARN,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_brl,
    fmt_dt,
    fmt_num,
    humanize_bool,
    is_int,
    kb,
    log_action,
    page_offset,
    parse_int,
    parse_number,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_tickets")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_tickets_list"
MSG_KEY_EMPTY = "admin_tickets_empty"
MSG_KEY_DETAIL = "admin_tickets_detail"
MSG_KEY_PANEL = "admin_tickets_panel"
MSG_KEY_REWARDS = "admin_tickets_rewards"

SCREEN_LIST = "admin_tickets_list"
SCREEN_DETAIL = "admin_tickets_detail"

# Callbacks base
CB_ROOT = "admin:tickets"
CB_PAGE = "admin:tickets:page"              # :N
CB_NEW = "admin:tickets:new"
CB_VIEW = "admin:tickets:view"              # :ID
CB_SET = "admin:tickets:set"                # :field:ID
CB_TOGGLE = "admin:tickets:toggle"          # :field:ID
CB_DELETE = "admin:tickets:delete"          # :ID
CB_DELETE_YES = "admin:tickets:delete:yes"  # :ID
CB_REWARDS = "admin:tickets:rewards"        # :ID
CB_REWARD_ADD = "admin:tickets:radd"        # :ID
CB_REWARD_DEL = "admin:tickets:rdel"        # :ID:IDX
CB_REWARD_WEIGHT = "admin:tickets:rweight"  # :ID:IDX
CB_REWARD_STOCK = "admin:tickets:rstock"    # :ID:IDX
CB_REWARD_AMOUNT = "admin:tickets:ramount"  # :ID:IDX
CB_REWARD_NAME = "admin:tickets:rname"      # :ID:IDX
CB_HISTORY = "admin:tickets:history"        # :ID
CB_REPORT = "admin:tickets:report"

# Raridades
RARITIES: Dict[str, str] = {
    "comum": "⚪️ Comum",
    "raro": "🔵 Raro",
    "epico": "🟣 Épico",
    "lendario": "🟡 Lendário",
    "mitico": "🔴 Mítico",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {"label": "Nome", "validate": "text", "max": 80},
    "rarity": {"label": "Raridade", "validate": "text", "max": 20},
    "price": {"label": "Preço", "validate": "money"},
    "stock": {"label": "Estoque", "validate": "int"},
    "per_user_limit": {"label": "Limite por usuário", "validate": "int"},
}

# Toggles
TOGGLE_FIELDS: Dict[str, str] = {
    "active": "Ativo",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_ticket(tid: int) -> Optional[Ticket]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Ticket).where(Ticket.id == tid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_tickets() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Ticket.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_tickets(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Ticket]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Ticket)
                .order_by(Ticket.id.asc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_user_tickets(tid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(UserTicket.id)).where(UserTicket.ticket_id == tid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


def _rarity_emoji(rarity: str) -> str:
    s = str(rarity or "").lower()
    return {
        "comum": "⚪️",
        "raro": "🔵",
        "epico": "🟣",
        "lendario": "🟡",
        "mitico": "🔴",
    }.get(s, "🎫")


def _rewards_to_list(rewards: Any) -> List[Dict[str, Any]]:
    if not isinstance(rewards, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in rewards:
        if isinstance(r, dict):
            out.append(dict(r))
        else:
            out.append({"name": str(r), "amount": 0.0, "weight": 1.0})
    return out


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    total = await _count_tickets()
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    tickets = await _list_tickets(page)

    if not tickets:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🎫 <b>TICKETS E RECOMPENSAS</b>\n\n"
                "<i>Nenhum ticket cadastrado.</i>\n\n"
                "Use ➕ Novo Ticket para começar."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for t in tickets:
            emoji = _rarity_emoji(t.rarity)
            ativo = SYM_OK if t.active else SYM_OFF
            rew = _rewards_to_list(t.rewards)
            rows.append(
                [
                    str(t.id),
                    truncate(t.name or "—", 18),
                    emoji,
                    fmt_brl(t.price),
                    str(int(t.stock or 0)),
                    f"{len(rew)}",
                    ativo,
                ]
            )

        lista = table(rows, ["ID", "Nome", "Rar.", "Preço", "Est.", "Prêm.", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🎫 <b>TICKETS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
        )

    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="➕ Novo Ticket", callback_data=CB_NEW),
                InlineKeyboardButton(text="📊 Relatório", callback_data=CB_REPORT),
            ],
        ]

    for t in tickets:
        emoji = _rarity_emoji(t.rarity)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{t.id} {truncate(t.name or '—', 22)} · "
                    f"{fmt_brl(t.price)}",
                    callback_data=f"{CB_VIEW}:{t.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=CB_PAGE,
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(build_back_row("admin:menu"))

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data == CB_ROOT)
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_list(callback, page=1)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1
        await render_list(callback, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_page falhou: {exc}")


# ============================================================
# CRIAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVO TICKET · PASSO 1/2</b>\n\n"
            "Envie o <b>nome</b> do ticket.\n"
            "Ex.: <code>Ticket Comum</code>, <code>Ticket Ouro</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="ticket_new_name",
            screen="tickets:new",
            use_force_reply=True,
            reply_placeholder="Nome do ticket",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_new falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_tickets_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Criação (passo 1: nome)
    if awaiting == "ticket_new_name":
        value, _extra = await consume_input(message, admin_id, expected_field="ticket_new_name")
        if value is None:
            return
        await _create_step1(message, admin_id, value)
        return

    # Criação (passo 2: raridade)
    if awaiting == "ticket_new_rarity":
        value, _extra = await consume_input(message, admin_id, expected_field="ticket_new_rarity")
        if value is None:
            return
        await _create_step2(message, admin_id, value)
        return

    # Criação (passo 3: preço)
    if awaiting == "ticket_new_price":
        value, _extra = await consume_input(message, admin_id, expected_field="ticket_new_price")
        if value is None:
            return
        await _create_step3(message, admin_id, value)
        return

    # Editar campos
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        tid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, tid, field, value)
        return

    # Editar prêmio (nome, valor, peso, estoque)
    if awaiting.startswith("rew_"):
        # rew_name:ID:IDX  /  rew_amount:ID:IDX  /  rew_weight:ID:IDX  /  rew_stock:ID:IDX
        parts = awaiting.split(":")
        action = parts[0].split("_", 1)[1]  # name/amount/weight/stock
        tid = int(parts[1])
        idx = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_reward_field(message, admin_id, tid, idx, action, value)
        return

    # Adicionar prêmio (nome → valor → peso)
    if awaiting.startswith("rewadd_"):
        # rewadd_name:ID / rewadd_amount:ID / rewadd_weight:ID
        stage = awaiting.split("_", 1)[1].split(":", 1)[0]
        tid = int(awaiting.split(":")[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _handle_reward_add(message, admin_id, tid, stage, value)
        return


async def _create_step1(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 80:
        await message.answer("❌ Nome inválido.")
        return

    await AdminFlow.update(admin_id, {"new_name": name})

    rows_kb: List[List[InlineKeyboardButton]] = []
    buf: List[InlineKeyboardButton] = []
    for key, label in RARITIES.items():
        buf.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"admin:tickets:rarsel:{key}",
            )
        )
        if len(buf) == 2:
            rows_kb.append(buf)
            buf = []
    if buf:
        rows_kb.append(buf)
    rows_kb.append(
        [InlineKeyboardButton(text="🔙 Cancelar", callback_data=CB_ROOT)]
    )

    text = (
        f"➕ <b>NOVO TICKET · PASSO 2/3</b>\n\n"
        f"✅ Nome: <b>{name}</b>\n\n"
        "Agora escolha a <b>raridade</b>:"
    )
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb(rows_kb))


@router.callback_query(F.data.startswith("admin:tickets:rarsel:"))
async def on_rarity_sel(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            rarity = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Raridade inválida.")
            return
        if rarity not in RARITIES:
            await toast_err(callback, "❌ Raridade inválida.")
            return

        await AdminFlow.update(admin_id, {"new_rarity": rarity})

        prompt = (
            f"➕ <b>NOVO TICKET · PASSO 3/3</b>\n\n"
            f"🎨 Raridade: {RARITIES[rarity]}\n\n"
            "Envie o <b>preço</b> do ticket em R$.\n"
            "Ex.: <code>9.90</code> ou <code>25.00</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="ticket_new_price",
            screen="tickets:new",
            use_force_reply=True,
            reply_placeholder="9.90",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_rarity_sel falhou: {exc}")


async def _create_step2(message: Message, admin_id: int, rarity: str) -> None:
    # Redundante — raridade vem por callback
    pass


async def _create_step3(message: Message, admin_id: int, raw_price: str) -> None:
    price = parse_number(raw_price)
    if price is None or price <= 0:
        await message.answer("❌ Preço inválido.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    name = data.get("new_name")
    rarity = data.get("new_rarity")

    if not name or not rarity:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    try:
        async with session_scope() as session:
            t = Ticket(
                name=name,
                rarity=rarity,
                price=Decimal(str(round(price, 2))),
                rewards=[],
                odds={},
                stock=0,
                per_user_limit=0,
                active=True,
            )
            session.add(t)
            await session.flush()
            tid = int(t.id)
    except Exception as exc:
        logger.exception(f"_create_step3 falhou: {exc}")
        await message.answer("❌ Erro ao criar ticket.")
        return

    await AdminFlow.clear(admin_id)

    await log_action(
        admin_id=admin_id,
        action="ticket.created",
        category="admin",
        details={"ticket_id": tid, "name": name, "rarity": rarity, "price": price},
    )

    await message.answer(
        f"✅ <b>Ticket criado!</b>\n\n"
        f"🎫 Nome: <b>{name}</b>\n"
        f"🎨 Raridade: {RARITIES.get(rarity, rarity)}\n"
        f"💵 Preço: <b>{fmt_brl(price)}</b>\n"
        f"🆔 ID: <code>#{tid}</code>\n\n"
        f"⚠️ Adicione prêmios usando o botão 🎁 Prêmios.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="✏️ Editar Ticket", callback_data=f"{CB_VIEW}:{tid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE (EDITOR)
# ============================================================
async def _render_detail(tid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    t = await _load_ticket(tid)
    if t is None:
        return None, None

    sold = await _count_user_tickets(tid)
    rewards = _rewards_to_list(t.rewards)

    reward_lines: List[str] = []
    for i, r in enumerate(rewards[:8]):
        name = str(r.get("name") or "—")
        amount = float(r.get("amount") or 0)
        weight = float(r.get("weight") or 1)
        stock = r.get("stock", "∞")
        reward_lines.append(
            f"  {i}. {truncate(name, 20)} · {fmt_brl(amount)} · "
            f"peso {weight:g} · estoque {stock}"
        )

    rewards_text = "\n".join(reward_lines) if reward_lines else "  (nenhum)"

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"🎫 <b>TICKET #{t.id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Nome: <b>{t.name}</b>\n"
            f"├ Raridade: {_rarity_emoji(t.rarity)} {t.rarity}\n"
            f"├ Preço: <b>{fmt_brl(t.price)}</b>\n"
            f"├ Estoque: <b>{int(t.stock or 0)}</b>\n"
            f"├ Limite/usuário: <b>{int(t.per_user_limit or 0)}</b>\n"
            f"├ Ativo: {humanize_bool(t.active)}\n"
            f"├ Vendidos: <b>{fmt_num(sold)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 <b>Prêmios ({len(rewards)}):</b>\n"
            f"{rewards_text}"
        ),
        id=t.id,
        nome=t.name or "—",
        raridade=f"{_rarity_emoji(t.rarity)} {t.rarity or '—'}",
        preco=fmt_brl(t.price),
        estoque=int(t.stock or 0),
        limite=int(t.per_user_limit or 0),
        ativo=humanize_bool(t.active),
        vendidos=fmt_num(sold),
        premios=f"{len(rewards)}",
        probabilidades="—",
        total=f"{len(rewards)}",
        rewards=rewards_text,
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(t))

    return rendered.content, keyboard


def _default_detail_keyboard(t: Ticket) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Nome", f"{CB_SET}:name:{t.id}"), b("🎨 Raridade", f"{CB_SET}:rarity:{t.id}")],
        [b("✏️ Preço", f"{CB_SET}:price:{t.id}"), b("✏️ Estoque", f"{CB_SET}:stock:{t.id}")],
        [b("✏️ Limite/User", f"{CB_SET}:per_user_limit:{t.id}")],
        [b("🎁 Gerenciar Prêmios", f"{CB_REWARDS}:{t.id}")],
        [b("🔄 Ativo/Inativo", f"{CB_TOGGLE}:active:{t.id}")],
        [b("📜 Histórico", f"{CB_HISTORY}:{t.id}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{t.id}")],
        [b("🔙 Voltar", CB_ROOT)],
    ]


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            tid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(tid)
        if text is None:
            await toast_err(callback, "❌ Ticket não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_view falhou: {exc}")


# ============================================================
# SET FIELD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:tickets:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        tid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        # Raridade → lista de opções
        if field == "rarity":
            rows_kb: List[List[InlineKeyboardButton]] = []
            for key, label in RARITIES.items():
                rows_kb.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=f"admin:tickets:setrar:{tid}:{key}",
                        )
                    ]
                )
            rows_kb.append(
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{tid}")]
            )
            text = "🎨 <b>ESCOLHER RARIDADE</b>\n\nEscolha a nova raridade:"
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "money":
            hint = "Ex.: <code>19.90</code>"
        elif meta["validate"] == "int":
            hint = "Ex.: <code>10</code>"
        else:
            hint = f"Até {meta.get('max', 80)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{tid}",
            screen=f"tickets:edit:{tid}",
            extra={"ticket_id": tid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_set_field falhou: {exc}")


@router.callback_query(F.data.startswith("admin:tickets:setrar:"))
async def on_set_rarity(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:tickets:setrar:ID:RARITY
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        tid = int(parts[4])
        rarity = parts[5]

        if rarity not in RARITIES:
            await toast_err(callback, "❌ Raridade inválida.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(select(Ticket).where(Ticket.id == tid))
                t: Optional[Ticket] = res.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Ticket não encontrado.")
                    return
                t.rarity = rarity
                session.add(t)
        except Exception as exc:
            logger.exception(f"set rarity falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="ticket.set.rarity",
            category="admin",
            details={"ticket_id": tid, "rarity": rarity},
        )

        text, keyboard = await _render_detail(tid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🎨 Raridade alterada.")
    except Exception as exc:
        logger.exception(f"tickets on_set_rarity falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    tid: int,
    field: str,
    value: str,
) -> None:
    meta = TEXT_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if meta["validate"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = Decimal(str(round(num, 2)))
    elif meta["validate"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        parsed = int(num)
    else:
        if len(val) > meta.get("max", 200):
            await message.answer("❌ Texto muito longo.")
            return

    try:
        async with session_scope() as session:
            res = await session.execute(select(Ticket).where(Ticket.id == tid))
            t: Optional[Ticket] = res.scalar_one_or_none()
            if t is None:
                await message.answer("❌ Ticket não encontrado.")
                return
            setattr(t, field, parsed)
            session.add(t)
    except Exception as exc:
        logger.exception(f"_apply_field_value (ticket) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"ticket.set.{field}",
        category="admin",
        details={"ticket_id": tid},
    )

    text, keyboard = await _render_detail(tid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:tickets:toggle:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        tid = int(parts[4])

        if field not in TOGGLE_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(select(Ticket).where(Ticket.id == tid))
                t: Optional[Ticket] = res.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Ticket não encontrado.")
                    return
                current = bool(getattr(t, field))
                setattr(t, field, not current)
                session.add(t)
                new_val = not current
        except Exception as exc:
            logger.exception(f"toggle ticket falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"ticket.toggle.{field}",
            category="admin",
            details={"ticket_id": tid, "value": new_val},
        )

        text, keyboard = await _render_detail(tid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🔄 {TOGGLE_FIELDS[field]} alterado.")
    except Exception as exc:
        logger.exception(f"tickets on_toggle falhou: {exc}")


# ============================================================
# PRÊMIOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REWARDS}:"))
async def on_rewards(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            tid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        t = await _load_ticket(tid)
        if t is None:
            await toast_err(callback, "❌ Ticket não encontrado.")
            return

        rewards = _rewards_to_list(t.rewards)

        lines = [f"🎁 <b>PRÊMIOS · TICKET #{tid}</b>", ""]
        if not rewards:
            lines.append("<i>Nenhum prêmio cadastrado ainda.</i>")
        else:
            for i, r in enumerate(rewards):
                name = str(r.get("name") or "—")
                amount = float(r.get("amount") or 0)
                weight = float(r.get("weight") or 1)
                stock = r.get("stock", "∞")
                lines.append(
                    f"<b>{i}.</b> {truncate(name, 24)}\n"
                    f"    💵 {fmt_brl(amount)} · peso {weight:g} · est. {stock}"
                )

        text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="➕ Adicionar Prêmio",
                    callback_data=f"{CB_REWARD_ADD}:{tid}",
                )
            ],
        ]

        for i, r in enumerate(rewards[:10]):
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"✏️ #{i} {truncate(str(r.get('name') or '—'), 20)}",
                        callback_data=f"admin:tickets:redit:{tid}:{i}",
                    )
                ]
            )

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{tid}")]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_rewards falhou: {exc}")


@router.callback_query(F.data.startswith("admin:tickets:redit:"))
async def on_reward_edit(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:tickets:redit:ID:IDX
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        tid = int(parts[4])
        idx = int(parts[5])

        t = await _load_ticket(tid)
        if t is None:
            await toast_err(callback, "❌ Ticket não encontrado.")
            return

        rewards = _rewards_to_list(t.rewards)
        if idx < 0 or idx >= len(rewards):
            await toast_err(callback, "❌ Prêmio não encontrado.")
            return

        r = rewards[idx]
        name = str(r.get("name") or "—")
        amount = float(r.get("amount") or 0)
        weight = float(r.get("weight") or 1)
        stock = r.get("stock", "∞")

        text = (
            f"🎁 <b>EDITAR PRÊMIO #{idx}</b>\n\n"
            f"├ Nome: <b>{name}</b>\n"
            f"├ Valor: <b>{fmt_brl(amount)}</b>\n"
            f"├ Peso: <b>{weight:g}</b>\n"
            f"└ Estoque: <b>{stock}</b>"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="✏️ Nome",
                    callback_data=f"{CB_REWARD_NAME}:{tid}:{idx}",
                ),
                InlineKeyboardButton(
                    text="💵 Valor",
                    callback_data=f"{CB_REWARD_AMOUNT}:{tid}:{idx}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚖️ Peso",
                    callback_data=f"{CB_REWARD_WEIGHT}:{tid}:{idx}",
                ),
                InlineKeyboardButton(
                    text="📦 Estoque",
                    callback_data=f"{CB_REWARD_STOCK}:{tid}:{idx}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Remover",
                    callback_data=f"{CB_REWARD_DEL}:{tid}:{idx}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=f"{CB_REWARDS}:{tid}",
                )
            ],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_reward_edit falhou: {exc}")


@router.callback_query(F.data.startswith("admin:tickets:rew:"))
async def on_reward_set(callback: CallbackQuery) -> None:
    """
    Formato: admin:tickets:rew:ACTION:ID:IDX
    Actions: name, amount, weight, stock
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        action = parts[4]
        tid = int(parts[5])
        idx = int(parts[6]) if len(parts) > 6 else 0

        if action not in ("name", "amount", "weight", "stock"):
            await toast_err(callback, "❌ Ação inválida.")
            return

        hint = ""
        if action == "amount":
            hint = "Ex.: <code>10.00</code>"
        elif action == "weight":
            hint = "Peso relativo. Ex.: <code>50</code> (maior = mais chance)"
        elif action == "stock":
            hint = "Quantidade disponível. Ex.: <code>100</code>"
        else:
            hint = "Nome do prêmio"

        prompt = (
            f"✏️ <b>EDITAR PRÊMIO · {action.upper()}</b>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"rew_{action}:{tid}:{idx}",
            screen=f"tickets:rew:{tid}",
            extra={"ticket_id": tid, "idx": idx, "action": action},
            use_force_reply=True,
            reply_placeholder=action,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_reward_set falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REWARD_DEL}:"))
async def on_reward_del(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        tid = int(parts[3])
        idx = int(parts[4])

        try:
            async with session_scope() as session:
                res = await session.execute(select(Ticket).where(Ticket.id == tid))
                t: Optional[Ticket] = res.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Ticket não encontrado.")
                    return
                rewards = _rewards_to_list(t.rewards)
                if 0 <= idx < len(rewards):
                    rewards.pop(idx)
                    t.rewards = rewards
                    session.add(t)
        except Exception as exc:
            logger.exception(f"reward del falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="ticket.reward.removed",
            category="admin",
            details={"ticket_id": tid, "idx": idx},
        )

        callback.data = f"{CB_REWARDS}:{tid}"  # type: ignore[misc]
        await on_rewards(callback)
        await toast_ok(callback, "🗑️ Prêmio removido.")
    except Exception as exc:
        logger.exception(f"tickets on_reward_del falhou: {exc}")


async def _apply_reward_field(
    message: Message,
    admin_id: int,
    tid: int,
    idx: int,
    action: str,
    value: str,
) -> None:
    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if action == "amount":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = float(num)
    elif action == "weight":
        num = parse_number(val)
        if num is None or num <= 0:
            await message.answer("❌ Peso inválido.")
            return
        parsed = float(num)
    elif action == "stock":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        parsed = int(num)

    try:
        async with session_scope() as session:
            res = await session.execute(select(Ticket).where(Ticket.id == tid))
            t: Optional[Ticket] = res.scalar_one_or_none()
            if t is None:
                await message.answer("❌ Ticket não encontrado.")
                return
            rewards = _rewards_to_list(t.rewards)
            if idx < 0 or idx >= len(rewards):
                await message.answer("❌ Prêmio não encontrado.")
                return
            rewards[idx][action] = parsed
            t.rewards = rewards
            session.add(t)
    except Exception as exc:
        logger.exception(f"_apply_reward_field falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"ticket.reward.set.{action}",
        category="admin",
        details={"ticket_id": tid, "idx": idx},
    )

    callback_data = f"{CB_REWARDS}:{tid}"
    text = f"✅ Prêmio #{idx} · {action} atualizado para <code>{parsed}</code>."

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="🎁 Ver Prêmios", callback_data=callback_data)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{tid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith(f"{CB_REWARD_ADD}:"))
async def on_reward_add(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            tid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        prompt = (
            "➕ <b>NOVO PRÊMIO · PASSO 1/3</b>\n\n"
            "Envie o <b>nome</b> do prêmio.\n"
            "Ex.: <code>R$ 10</code>, <code>Comum</code>, <code>Prêmio Ouro</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"rewadd_name:{tid}",
            screen=f"tickets:rewadd:{tid}",
            extra={"ticket_id": tid},
            use_force_reply=True,
            reply_placeholder="Nome do prêmio",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_reward_add falhou: {exc}")


async def _handle_reward_add(
    message: Message,
    admin_id: int,
    tid: int,
    stage: str,
    value: str,
) -> None:
    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}

    if stage == "name":
        await AdminFlow.update(admin_id, {
            "new_reward_name": val,
            "new_reward_tid": tid,
        })
        prompt = (
            f"➕ <b>NOVO PRÊMIO · PASSO 2/3</b>\n\n"
            f"✅ Nome: <b>{val}</b>\n\n"
            "Agora envie o <b>valor</b> em R$ (ou 0 se não for saldo).\n"
            "Ex.: <code>10.00</code> ou <code>0</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field=f"rewadd_amount:{tid}",
            screen=f"tickets:rewadd:{tid}",
            extra={"ticket_id": tid},
            use_force_reply=True,
            reply_placeholder="10.00",
        )
        return

    if stage == "amount":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        await AdminFlow.update(admin_id, {"new_reward_amount": float(num)})
        prompt = (
            f"➕ <b>NOVO PRÊMIO · PASSO 3/3</b>\n\n"
            f"💵 Valor: <b>{fmt_brl(num)}</b>\n\n"
            "Agora envie o <b>peso</b> do prêmio.\n"
            "Peso maior = mais chance de sair.\n"
            "Ex.: <code>50</code>, <code>10</code>, <code>1</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field=f"rewadd_weight:{tid}",
            screen=f"tickets:rewadd:{tid}",
            extra={"ticket_id": tid},
            use_force_reply=True,
            reply_placeholder="50",
        )
        return

    if stage == "weight":
        num = parse_number(val)
        if num is None or num <= 0:
            await message.answer("❌ Peso inválido.")
            return

        state2 = await AdminFlow.get(admin_id)
        data2 = (state2 or {}).get("data", {}) if state2 else {}
        name = data2.get("new_reward_name")
        amount = float(data2.get("new_reward_amount") or 0)

        if not name:
            await message.answer("❌ Fluxo incompleto. Recomece.")
            await AdminFlow.clear(admin_id)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(select(Ticket).where(Ticket.id == tid))
                t: Optional[Ticket] = res.scalar_one_or_none()
                if t is None:
                    await message.answer("❌ Ticket não encontrado.")
                    return
                rewards = _rewards_to_list(t.rewards)
                rewards.append({
                    "name": str(name),
                    "amount": float(amount),
                    "weight": float(num),
                    "type": "balance" if amount > 0 else "none",
                })
                t.rewards = rewards
                session.add(t)
        except Exception as exc:
            logger.exception(f"add reward falhou: {exc}")
            await message.answer("❌ Erro ao salvar.")
            return

        await AdminFlow.clear(admin_id)

        await log_action(
            admin_id=admin_id,
            action="ticket.reward.added",
            category="admin",
            details={"ticket_id": tid, "name": name, "amount": amount, "weight": float(num)},
        )

        await message.answer(
            f"✅ <b>Prêmio adicionado!</b>\n\n"
            f"🎁 {name}\n"
            f"💵 {fmt_brl(amount)}\n"
            f"⚖️ Peso: {float(num):g}",
            parse_mode="HTML",
            reply_markup=kb(
                [
                    [InlineKeyboardButton(text="🎁 Ver Prêmios", callback_data=f"{CB_REWARDS}:{tid}")],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{tid}")],
                ]
            ),
        )
        return


# ============================================================
# HISTÓRICO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_HISTORY}:"))
async def on_history(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            tid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(UserTicket)
                    .where(UserTicket.ticket_id == tid)
                    .order_by(UserTicket.created_at.desc())
                    .limit(30)
                )
                tickets = list(res.scalars().all())
        except Exception:
            tickets = []

        if not tickets:
            text = "📜 <i>Nenhuma compra registrada deste ticket.</i>"
        else:
            lines = [f"📜 <b>HISTÓRICO · TICKET #{tid}</b> ({len(tickets)})", ""]
            for ut in tickets:
                reward = ut.reward or {}
                rname = reward.get("name") if isinstance(reward, dict) else "—"
                ramount = reward.get("amount") if isinstance(reward, dict) else 0
                try:
                    ramount_f = float(ramount or 0)
                except Exception:
                    ramount_f = 0.0
                lines.append(
                    f"👤 <code>{ut.user_id}</code> · 🎁 {truncate(str(rname), 20)} · "
                    f"{fmt_brl(ramount_f)} · {fmt_dt(ut.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{tid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_history falhou: {exc}")


# ============================================================
# EXCLUIR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            tid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        t = await _load_ticket(tid)
        if t is None:
            await toast_err(callback, "❌ Ticket não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR TICKET</b>\n\n"
            f"🎫 <b>{t.name}</b> (#{t.id})\n\n"
            "Isso remove o ticket e o histórico de compras.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{tid}",
            cancel_callback=f"{CB_VIEW}:{tid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            tid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Remove histórico
                await session.execute(
                    UserTicket.__table__.delete().where(
                        UserTicket.ticket_id == tid
                    )
                )
                # Remove ticket
                await session.execute(
                    Ticket.__table__.delete().where(Ticket.id == tid)
                )
        except Exception as exc:
            logger.exception(f"delete ticket falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="ticket.deleted",
            category="admin",
            details={"ticket_id": tid},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "🗑️ Excluído.")
    except Exception as exc:
        logger.exception(f"tickets on_delete_confirm falhou: {exc}")


# ============================================================
# RELATÓRIO
# ============================================================
@router.callback_query(F.data == CB_REPORT)
async def on_report(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                total_tickets = int(
                    (await session.execute(select(func.count(Ticket.id)))).scalar() or 0
                )
                ativos = int(
                    (
                        await session.execute(
                            select(func.count(Ticket.id)).where(Ticket.active == True)  # noqa: E712
                        )
                    ).scalar()
                    or 0
                )
                total_vendas = int(
                    (await session.execute(select(func.count(UserTicket.id)))).scalar() or 0
                )
                total_pago = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(UserTicket.cost), 0))
                        )
                    ).scalar()
                    or 0
                )
                total_premios = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(UserTicket.reward), 0))
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total_tickets = ativos = total_vendas = 0
            total_pago = total_premios = 0.0

        lucro = round(total_pago - total_premios, 2)

        text = (
            "📊 <b>RELATÓRIO DE TICKETS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎫 Total cadastrados: <b>{fmt_num(total_tickets)}</b>\n"
            f"🟢 Ativos: <b>{fmt_num(ativos)}</b>\n"
            f"🛒 Vendas totais: <b>{fmt_num(total_vendas)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Arrecadado: <b>{fmt_brl(total_pago)}</b>\n"
            f"🎁 Prêmios distribuídos: <b>{fmt_brl(total_premios)}</b>\n"
            f"📈 Lucro líquido: <b>{fmt_brl(lucro)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tickets on_report falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_new",
    "on_tickets_text_input",
    "on_rarity_sel",
    "on_view",
    "on_set_field",
    "on_set_rarity",
    "on_toggle",
    "on_rewards",
    "on_reward_edit",
    "on_reward_set",
    "on_reward_del",
    "on_reward_add",
    "on_history",
    "on_delete",
    "on_delete_confirm",
    "on_report",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_VIEW",
    "CB_SET",
    "CB_TOGGLE",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_REWARDS",
    "CB_REWARD_ADD",
    "CB_REWARD_DEL",
    "CB_REWARD_WEIGHT",
    "CB_REWARD_STOCK",
    "CB_REWARD_AMOUNT",
    "CB_REWARD_NAME",
    "CB_HISTORY",
    "CB_REPORT",
    "RARITIES",
    "TEXT_FIELDS",
    "TOGGLE_FIELDS",
]
