"""
Larizinha Store — Módulo Admin: Usuários
Implementa gerenciamento completo de usuários conforme DIM.

Telas:
- Lista de usuários (filtros: hoje, com saldo, ativos, bloqueados, banidos, top)
- Buscar por ID / Nome
- Perfil completo (dados + movimentações + status)
- Ajuste de saldo (Add/Remover) com MOTIVO OBRIGATÓRIO + auditoria
- Bloquear / Desbloquear / Banir / Desbanir
- Editar dados (WhatsApp, e-mail, chave PIX)
- Redefinir conta (reset de saldo + histórico)
- Enviar mensagem / notificação
- Ver transações, compras, indicações, comissões

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Ajuste de saldo cria Transaction (ADMIN_CREDIT / ADMIN_DEBIT) e grava motivo.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import (
    Commission,
    CommissionStatus,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    Withdraw,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.services.security_monitor import security_monitor
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_ACTIVE,
    SYM_BANNED,
    SYM_BLOCKED,
    SYM_DONE,
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
    is_number,
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
router = Router(name="admin_users")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_users_list"
MSG_KEY_EMPTY = "admin_users_empty"
MSG_KEY_PROFILE = "admin_users_profile"
MSG_KEY_ADJUST = "admin_users_adjust"
MSG_KEY_TRANSACTIONS = "admin_users_tx"
MSG_KEY_PURCHASES = "admin_users_orders"
MSG_KEY_REFERRALS = "admin_users_refs"
MSG_KEY_COMMISSIONS = "admin_users_comms"
MSG_KEY_EDIT = "admin_users_edit"

SCREEN_LIST = "admin_users_list"
SCREEN_PROFILE = "admin_users_profile"
SCREEN_ADJUST = "admin_users_adjust"

# Callbacks base
CB_ROOT = "admin:users"
CB_PAGE = "admin:users:page"              # :N
CB_FILTER = "admin:users:filter"          # :key
CB_SEARCH = "admin:users:search"          # :id|:name
CB_VIEW = "admin:users:view"              # :ID
CB_STATUS = "admin:users:status"          # :block|:ban|:unblock|:unban:ID
CB_ADD_BAL = "admin:users:addbal"         # :ID
CB_REM_BAL = "admin:users:rembal"         # :ID
CB_BAL_CONFIRM = "admin:users:bal:yes"    # :add|rem:ID
CB_EDIT_FIELD = "admin:users:editfield"   # :field:ID
CB_RESET = "admin:users:reset"            # :ID
CB_RESET_YES = "admin:users:reset:yes"    # :ID
CB_MSG = "admin:users:msg"                # :ID
CB_NOTIFY = "admin:users:notify"          # :ID
CB_TX = "admin:users:tx"                  # :ID
CB_ORDERS = "admin:users:orders"          # :ID
CB_REFS = "admin:users:refs"              # :ID
CB_COMMS = "admin:users:comms"            # :ID

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "today": "📅 Hoje",
    "balance": "💰 Com Saldo",
    "active": "🟢 Ativos",
    "blocked": "🚫 Bloqueados",
    "banned": "⛔ Banidos",
    "top": "🏆 Top (saldo)",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "whatsapp": {"label": "WhatsApp", "validate": "text", "max": 20},
    "email": {"label": "E-mail", "validate": "email", "max": 120},
    "pix_key": {"label": "Chave PIX", "validate": "text", "max": 120},
    "pix_key_type": {"label": "Tipo da Chave PIX", "validate": "text", "max": 30},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "ACTIVE": SYM_ACTIVE,
        "BLOCKED": SYM_BLOCKED,
        "BANNED": SYM_BANNED,
        "DELETED": "⚪️",
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "ACTIVE": "Ativo",
        "BLOCKED": "Bloqueado",
        "BANNED": "Banido",
        "DELETED": "Deletado",
    }.get(s, str(status))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(User.created_at >= start)
    if filter_key == "balance":
        return stmt.where(User.balance > 0)
    if filter_key == "active":
        return stmt.where(User.status == UserStatus.ACTIVE)
    if filter_key == "blocked":
        return stmt.where(User.status == UserStatus.BLOCKED)
    if filter_key == "banned":
        return stmt.where(User.status == UserStatus.BANNED)
    if filter_key == "top":
        return stmt.order_by(User.balance.desc())
    return stmt


async def _count_users(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(User.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_users(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[User]:
    try:
        async with session_scope() as session:
            stmt = select(User)
            if filter_key == "top":
                stmt = stmt.order_by(User.balance.desc())
            else:
                stmt = stmt.order_by(User.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_user_by_id(uid: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == uid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_user_orders(uid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Order.id)).where(
                    Order.user_id == uid,
                    Order.status == OrderStatus.DELIVERED,
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _count_user_refs(uid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(User.id)).where(User.referred_by_id == uid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_users(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    users = await _list_users(page, filter_key)

    if not users:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "👥 <b>USUÁRIOS</b>\n\n"
                "<i>Nenhum usuário encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for u in users:
            emoji = _status_emoji(u.status)
            rows.append(
                [
                    str(u.id),
                    truncate(u.first_name or u.username or "—", 14),
                    f"@{u.username}" if u.username else "—",
                    fmt_brl(u.balance),
                    str(int(u.purchases_count or 0)),
                    emoji,
                ]
            )

        lista = table(rows, ["ID", "Nome", "@user", "Saldo", "Compras", "St"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"👥 <b>USUÁRIOS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="📅 Hoje", callback_data=f"{CB_FILTER}:today"),
                InlineKeyboardButton(text="💰 Com Saldo", callback_data=f"{CB_FILTER}:balance"),
            ],
            [
                InlineKeyboardButton(text="🟢 Ativos", callback_data=f"{CB_FILTER}:active"),
                InlineKeyboardButton(text="🚫 Bloqueados", callback_data=f"{CB_FILTER}:blocked"),
            ],
            [
                InlineKeyboardButton(text="⛔ Banidos", callback_data=f"{CB_FILTER}:banned"),
                InlineKeyboardButton(text="🏆 Top", callback_data=f"{CB_FILTER}:top"),
            ],
            [
                InlineKeyboardButton(text="🔍 Buscar ID", callback_data=f"{CB_SEARCH}:id"),
                InlineKeyboardButton(text="🔍 Buscar Nome", callback_data=f"{CB_SEARCH}:name"),
            ],
            [
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for u in users:
        emoji = _status_emoji(u.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{u.id} {truncate(u.first_name or u.username or '—', 18)}",
                    callback_data=f"{CB_VIEW}:{u.id}",
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

    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=kb(rows_kb),
    )


@router.callback_query(F.data == CB_ROOT)
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_list(callback, page=1, filter_key="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_root falhou: {exc}")


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
        await render_list(callback, page=page, filter_key="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_page falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_FILTER}:"))
async def on_filter(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        filter_key = callback.data.split(":")[-1]
        if filter_key not in FILTERS:
            filter_key = "all"
        await render_list(callback, page=1, filter_key=filter_key)
        await toast_ok(callback, f"{FILTERS[filter_key]} selecionado.")
    except Exception as exc:
        logger.exception(f"users on_filter falhou: {exc}")


# ============================================================
# BUSCAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SEARCH}:"))
async def on_search(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        mode = callback.data.split(":")[-1]  # "id" or "name"
        field = "search_user_id" if mode == "id" else "search_user_name"
        prompt = (
            "🔍 <b>BUSCAR POR ID</b>\n\n"
            "Envie o ID numérico do usuário.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
            if mode == "id"
            else
            "🔍 <b>BUSCAR POR NOME</b>\n\n"
            "Envie parte do nome, username ou WhatsApp.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=field,
            screen="users:search",
            use_force_reply=True,
            reply_placeholder="Digite aqui...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_users_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # ---------- Busca ----------
    if awaiting in ("search_user_id", "search_user_name"):
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _do_search_users(message, admin_id, value, mode=awaiting.split("_")[-1])
        return

    # ---------- Ajuste de saldo ----------
    if awaiting.startswith("balance:"):
        # formatos: balance:add:ID:VALOR  OR  balance:add:ID:VALOR:REASON
        parts = awaiting.split(":", 3)
        kind = parts[1]          # add | rem
        uid = int(parts[2])
        step = state["data"].get("step")
        if step == "value":
            value, extra = await consume_input(message, admin_id, expected_field=awaiting)
            if value is None:
                return
            await _confirm_balance(message, admin_id, uid, kind, value)
            return
        if step == "reason":
            value, extra = await consume_input(message, admin_id, expected_field=awaiting)
            if value is None:
                return
            await _apply_balance(message, admin_id, uid, kind, value, extra)
            return

    # ---------- Editar campo ----------
    if awaiting.startswith("editfield:"):
        # editfield:field:ID
        parts = awaiting.split(":", 2)
        field = parts[1]
        uid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_edit_field(message, admin_id, uid, field, value)
        return

    # ---------- Enviar mensagem ----------
    if awaiting.startswith("sendmsg:"):
        uid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _send_admin_message(message, admin_id, uid, value, as_notify=False)
        return

    if awaiting.startswith("notify:"):
        uid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _send_admin_message(message, admin_id, uid, value, as_notify=True)
        return


async def _do_search_users(
    message: Message,
    admin_id: int,
    value: str,
    *,
    mode: str,
) -> None:
    value = (value or "").strip()
    if not value:
        await message.answer("❌ Valor vazio.")
        return

    results: List[User] = []
    try:
        async with session_scope() as session:
            if mode == "id":
                try:
                    uid = int(value)
                except Exception:
                    await message.answer("❌ ID inválido.")
                    return
                res = await session.execute(
                    select(User).where(
                        or_(
                            User.id == uid,
                            User.telegram_id == uid,
                        )
                    ).limit(20)
                )
            else:
                like = f"%{value.lower()}%"
                res = await session.execute(
                    select(User)
                    .where(
                        or_(
                            func.lower(func.coalesce(User.first_name, "")).like(like),
                            func.lower(func.coalesce(User.last_name, "")).like(like),
                            func.lower(func.coalesce(User.username, "")).like(like),
                            func.coalesce(User.whatsapp, "").like(f"%{value}%"),
                        )
                    )
                    .limit(20)
                )
            results = list(res.scalars().all())
    except Exception:
        results = []

    if not results:
        await message.answer(
            text="🔍 <b>NENHUM RESULTADO</b>",
            parse_mode="HTML",
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🔙 Voltar", callback_data=CB_ROOT
                        )
                    ]
                ]
            ),
        )
        return

    lines = [f"🔍 <b>RESULTADOS ({len(results)})</b>", ""]
    rows_kb: List[List[InlineKeyboardButton]] = []
    for u in results:
        emoji = _status_emoji(u.status)
        lines.append(
            f"{emoji} #{u.id} · <b>{u.first_name or '—'}</b> · "
            f"{fmt_brl(u.balance)}"
        )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{u.id} {truncate(u.first_name or '—', 18)}",
                    callback_data=f"{CB_VIEW}:{u.id}",
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

    await message.answer(
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# PERFIL
# ============================================================
async def _render_profile(u: User) -> Tuple[str, InlineKeyboardMarkup]:
    refs = await _count_user_refs(int(u.id))
    orders_count = await _count_user_orders(int(u.id))

    # Comissões (todas as geradas para o usuário, se for afiliado)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.coalesce(func.sum(Commission.amount), 0)).where(
                    Commission.earner_id == u.id,
                    Commission.status.in_(
                        [CommissionStatus.APPROVED, CommissionStatus.PAID]
                    ),
                )
            )
            commissions = float(res.scalar() or 0)
    except Exception:
        commissions = 0.0

    status_emoji = _status_emoji(u.status)

    rendered = await message_engine.render(
        MSG_KEY_PROFILE,
        fallback=(
            f"👤 <b>USUÁRIO #{u.id}</b>\n"
            f"├ Nome: {u.first_name or '—'}\n"
            f"├ Username: @{u.username or '—'}\n"
            f"├ Idioma: {u.language_code or '—'}\n"
            f"├ Saldo: {fmt_brl(u.balance)}\n"
            f"├ Saldo bloqueado: {fmt_brl(u.balance_locked)}\n"
            f"├ WhatsApp: {u.whatsapp or '—'}\n"
            f"├ E-mail: {u.email or '—'}\n"
            f"├ Chave PIX: {u.pix_key or '—'}\n"
            f"├ Cadastrado: {fmt_dt(u.created_at)}\n"
            f"├ Último acesso: {fmt_dt(u.last_seen_at)}\n"
            f"├ Compras: {orders_count}\n"
            f"├ Gasto total: {fmt_brl(u.total_spent)}\n"
            f"├ PIX inseridos: {fmt_brl(u.total_deposited)}\n"
            f"├ Indicações: {refs}\n"
            f"├ Comissões: {fmt_brl(commissions)}\n"
            f"└ Status: {status_emoji} {_status_label(u.status)}"
        ),
        id=u.id,
        nome=u.first_name or "—",
        username=u.username or "—",
        lang=u.language_code or "—",
        saldo=fmt_brl(u.balance),
        saldo_bloq=fmt_brl(u.balance_locked),
        whatsapp=u.whatsapp or "—",
        email=u.email or "—",
        pix=u.pix_key or "—",
        created=fmt_dt(u.created_at),
        last_seen=fmt_dt(u.last_seen_at),
        compras=orders_count,
        gasto=fmt_brl(u.total_spent),
        pix_in=fmt_brl(u.total_deposited),
        ind=refs,
        comissoes=fmt_brl(commissions),
        status=f"{status_emoji} {_status_label(u.status)}",
        foto="—",
    )

    engine_kb = await button_engine.inline(SCREEN_PROFILE)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_profile_keyboard(int(u.id), u.status))

    return rendered.content, keyboard


def _default_profile_keyboard(uid: int, status: Any) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = [
        [b("💰 Add Saldo", f"{CB_ADD_BAL}:{uid}"), b("➖ Remover Saldo", f"{CB_REM_BAL}:{uid}")],
    ]

    s = str(getattr(status, "value", status) or "").upper()
    if s == "BANNED":
        rows.append([b("✅ Desbanir", f"{CB_STATUS}:unban:{uid}")])
    elif s == "BLOCKED":
        rows.append([b("✅ Desbloquear", f"{CB_STATUS}:unblock:{uid}")])
    else:
        rows.append([b("🚫 Bloquear", f"{CB_STATUS}:block:{uid}"), b("⛔ Banir", f"{CB_STATUS}:ban:{uid}")])

    rows.extend([
        [b("✏️ Editar Dados", f"{CB_EDIT_FIELD}:whatsapp:{uid}")],
        [b("🔄 Redefinir Conta", f"{CB_RESET}:{uid}")],
        [b("📩 Enviar Msg", f"{CB_MSG}:{uid}"), b("🔔 Notificar", f"{CB_NOTIFY}:{uid}")],
        [b("📜 Transações", f"{CB_TX}:{uid}"), b("🛒 Compras", f"{CB_ORDERS}:{uid}")],
        [b("👥 Indicações", f"{CB_REFS}:{uid}"), b("💸 Comissões", f"{CB_COMMS}:{uid}")],
        [b("🔙 Voltar", CB_ROOT)],
    ])
    return rows


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return
        text, keyboard = await _render_profile(u)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_view falhou: {exc}")


# ============================================================
# AJUSTE DE SALDO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_ADD_BAL}:") | F.data.startswith(f"{CB_REM_BAL}:"))
async def on_balance_change(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        kind = "add" if parts[1] == "addbal" else "rem"
        uid = int(parts[2])

        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        label = "ADICIONAR" if kind == "add" else "REMOVER"
        prompt = (
            f"💰 <b>{label} SALDO · {u.first_name or '—'}</b>\n\n"
            f"Saldo atual: <b>{fmt_brl(u.balance)}</b>\n\n"
            f"Digite o valor a {'adicionar' if kind == 'add' else 'remover'}: "
            "Ex.: <code>10</code> ou <code>25.50</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )

        # Marca que estamos aguardando o valor
        await AdminFlow.set_awaiting(
            admin_id,
            field=f"balance:{kind}:{uid}",
            screen=f"users:balance:{uid}",
            extra={"user_id": uid, "kind": kind, "step": "value"},
        )
        await ask_input(
            callback,
            prompt,
            field=f"balance:{kind}:{uid}",
            screen=f"users:balance:{uid}",
            extra={"user_id": uid, "kind": kind, "step": "value"},
            use_force_reply=True,
            reply_placeholder="Ex.: 10",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_balance_change falhou: {exc}")


async def _confirm_balance(
    message: Message,
    admin_id: int,
    uid: int,
    kind: str,
    raw_value: str,
) -> None:
    """Recebe o valor e pede o MOTIVO (obrigatório)."""
    valor = parse_number(raw_value)
    if valor is None or valor <= 0:
        await message.answer("❌ Valor inválido.")
        return

    u = await _load_user_by_id(uid)
    if u is None:
        await message.answer("❌ Usuário não encontrado.")
        return

    if kind == "rem" and float(u.balance or 0) < valor:
        await message.answer(
            f"❌ Saldo insuficiente para remover.\n"
            f"Saldo: {fmt_brl(u.balance)}"
        )
        return

    # Guarda valor e pede motivo
    await AdminFlow.update(admin_id, {"pending_value": valor, "step": "reason"})

    prompt = (
        f"📝 <b>MOTIVO OBRIGATÓRIO</b>\n\n"
        f"Usuário: <b>{u.first_name or '—'}</b> (#{uid})\n"
        f"Ação: <b>{'Adicionar' if kind == 'add' else 'Remover'}</b> "
        f"{fmt_brl(valor)}\n\n"
        "Envie o motivo (obrigatório, será salvo no histórico).\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field=f"balance:{kind}:{uid}",
        screen=f"users:balance:{uid}",
        extra={"user_id": uid, "kind": kind, "step": "reason", "value": valor},
        use_force_reply=True,
        reply_placeholder="Motivo...",
    )


async def _apply_balance(
    message: Message,
    admin_id: int,
    uid: int,
    kind: str,
    reason: str,
    extra: Dict[str, Any],
) -> None:
    """Executa a alteração de saldo com motivo."""
    reason = (reason or "").strip()
    if not reason:
        await message.answer("❌ Motivo é obrigatório.")
        return

    try:
        state = await AdminFlow.get(admin_id)
        pending_value = float((state or {}).get("data", {}).get("pending_value", 0))
    except Exception:
        pending_value = 0.0

    if pending_value <= 0:
        await message.answer("❌ Valor não identificado. Reinicie o fluxo.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(
                select(User).where(User.id == uid).with_for_update()
            )
            u: Optional[User] = r.scalar_one_or_none()
            if u is None:
                await message.answer("❌ Usuário não encontrado.")
                return

            if kind == "add":
                new_balance = round(float(u.balance or 0) + pending_value, 2)
                u.balance = Decimal(str(new_balance))
                tx_type = TransactionType.ADMIN_CREDIT
            else:
                if float(u.balance or 0) < pending_value:
                    await message.answer("❌ Saldo insuficiente.")
                    return
                new_balance = round(float(u.balance or 0) - pending_value, 2)
                u.balance = Decimal(str(new_balance))
                tx_type = TransactionType.ADMIN_DEBIT

            session.add(u)
            session.add(
                Transaction(
                    user_id=u.id,
                    type=tx_type,
                    amount=Decimal(str(round(pending_value, 2))),
                    balance_after=Decimal(str(new_balance)),
                    description=f"Admin: {reason}",
                    reference=f"admin:{admin_id}",
                    metadata_={"reason": reason, "admin_id": admin_id},
                )
            )

            tg_id = int(u.telegram_id)
            new_balance_final = new_balance
    except Exception as exc:
        logger.exception(f"_apply_balance falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    # Notifica o usuário
    try:
        if kind == "add":
            text = (
                f"💰 <b>Saldo adicionado!</b>\n\n"
                f"💵 Valor: <b>{fmt_brl(pending_value)}</b>\n"
                f"💰 Novo saldo: <b>{fmt_brl(new_balance_final)}</b>"
            )
        else:
            text = (
                f"➖ <b>Saldo removido!</b>\n\n"
                f"💵 Valor: <b>{fmt_brl(pending_value)}</b>\n"
                f"💰 Novo saldo: <b>{fmt_brl(new_balance_final)}</b>"
            )
        await message.bot.send_message(
            chat_id=tg_id,
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        pass

    await log_action(
        admin_id=admin_id,
        action=f"user.balance.{kind}",
        category="admin",
        user_id=uid,
        details={"value": pending_value, "reason": reason},
    )

    # Limpa estado
    await AdminFlow.clear(admin_id)

    # Reabre o perfil
    u2 = await _load_user_by_id(uid)
    if u2 is not None:
        text2, keyboard2 = await _render_profile(u2)
        await message.answer(
            text=text2,
            parse_mode="HTML",
            reply_markup=keyboard2,
            disable_web_page_preview=True,
        )


# ============================================================
# STATUS (bloquear / banir / desbloquear / desbanir)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_STATUS}:"))
async def on_change_status(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:users:status:action:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        action = parts[3]
        uid = int(parts[4])

        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        mapping = {
            "block": (UserStatus.BLOCKED, "Bloqueado"),
            "unblock": (UserStatus.ACTIVE, "Desbloqueado"),
            "ban": (UserStatus.BANNED, "Banido"),
            "unban": (UserStatus.ACTIVE, "Desbanido"),
        }
        if action not in mapping:
            await toast_err(callback, "❌ Ação inválida.")
            return

        new_status, label = mapping[action]

        try:
            async with session_scope() as session:
                r = await session.execute(select(User).where(User.id == uid))
                uu: Optional[User] = r.scalar_one_or_none()
                if uu is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return
                uu.status = new_status
                if new_status == UserStatus.ACTIVE:
                    uu.ban_reason = None
                    uu.block_reason = None
                session.add(uu)
        except Exception as exc:
            logger.exception(f"change status falhou: {exc}")
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"user.status.{action}",
            category="admin",
            user_id=uid,
            details={"new_status": new_status.value},
        )

        # Notifica o usuário
        try:
            uu2 = await _load_user_by_id(uid)
            if uu2 is not None:
                await callback.bot.send_message(
                    chat_id=int(uu2.telegram_id),
                    text=f"⚠️ <b>Sua conta foi {label.lower()}.</b>",
                    parse_mode="HTML",
                )
        except Exception:
            pass

        # Re-render
        u3 = await _load_user_by_id(uid)
        if u3 is not None:
            text, keyboard = await _render_profile(u3)
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"✅ {label}.")
    except Exception as exc:
        logger.exception(f"users on_change_status falhou: {exc}")


# ============================================================
# EDITAR CAMPO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EDIT_FIELD}:"))
async def on_edit_field(callback: CallbackQuery) -> None:
    """
    Formato: admin:users:editfield:field:ID
    Se field == "whatsapp" abre menu para escolher entre whatsapp/email/pix/pix_type.
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:users:editfield:whatsapp:ID  → abre seletor
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        uid = int(parts[4])

        # Menu de escolha quando é o botão raiz
        if field == "whatsapp":
            # Botão raiz → mostra opções
            u = await _load_user_by_id(uid)
            if u is None:
                await toast_err(callback, "❌ Usuário não encontrado.")
                return

            rows_kb = [
                [
                    InlineKeyboardButton(
                        text=f"📱 WhatsApp ({u.whatsapp or '—'})",
                        callback_data=f"{CB_EDIT_FIELD}:pick:whatsapp:{uid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"✉️ E-mail ({u.email or '—'})",
                        callback_data=f"{CB_EDIT_FIELD}:pick:email:{uid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"🔑 Chave PIX ({truncate(u.pix_key, 20) if u.pix_key else '—'})",
                        callback_data=f"{CB_EDIT_FIELD}:pick:pix_key:{uid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"🏷️ Tipo PIX ({u.pix_key_type or '—'})",
                        callback_data=f"{CB_EDIT_FIELD}:pick:pix_key_type:{uid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=f"{CB_VIEW}:{uid}",
                    )
                ],
            ]
            text = "✏️ <b>EDITAR DADOS</b>\n\nEscolha o campo que deseja alterar:"
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        if field == "pick":
            # admin:users:editfield:pick:REALFIELD:ID
            if len(parts) < 6:
                await toast_err(callback, "❌ Callback inválido.")
                return
            real_field = parts[4]
            uid = int(parts[5])

            if real_field not in TEXT_FIELDS:
                await toast_err(callback, "❌ Campo inválido.")
                return

            meta = TEXT_FIELDS[real_field]
            label = meta["label"]

            prompt = (
                f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
                f"Envie o novo valor.\n\n"
                "⚠️ A mensagem será apagada automaticamente."
            )
            await ask_input(
                callback,
                prompt,
                field=f"editfield:{real_field}:{uid}",
                screen=f"users:edit:{uid}",
                extra={"user_id": uid, "field": real_field},
                use_force_reply=True,
                reply_placeholder=label,
            )
            await answer(callback)
            return
    except Exception as exc:
        logger.exception(f"users on_edit_field falhou: {exc}")


async def _apply_edit_field(
    message: Message,
    admin_id: int,
    uid: int,
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

    if meta["validate"] == "email":
        import re
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val):
            await message.answer("❌ E-mail inválido.")
            return

    if len(val) > meta.get("max", 200):
        await message.answer("❌ Texto muito longo.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(select(User).where(User.id == uid))
            u: Optional[User] = r.scalar_one_or_none()
            if u is None:
                await message.answer("❌ Usuário não encontrado.")
                return
            setattr(u, field, val)
            session.add(u)
    except Exception as exc:
        logger.exception(f"edit field falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"user.edit.{field}",
        category="admin",
        user_id=uid,
        details={"value": val},
    )

    u2 = await _load_user_by_id(uid)
    if u2 is not None:
        text, keyboard = await _render_profile(u2)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# REDEFINIR CONTA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESET}:"))
async def on_reset(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 4 and parts[3] == "yes":
            return  # ignora o "yes" aqui

        uid = int(parts[-1])
        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        text = (
            f"🔄 <b>REDEFINIR CONTA · #{uid}</b>\n\n"
            f"Isso irá:\n"
            f"• Zerar o saldo\n"
            f"• Zerar total gasto\n"
            f"• Zerar total depositado\n"
            f"• Zerar total sacado\n"
            f"• Zerar total gifts\n"
            f"• Zerar contador de compras\n\n"
            "O histórico de transações NÃO será apagado.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RESET_YES}:{uid}",
            cancel_callback=f"{CB_VIEW}:{uid}",
            confirm_text="🔄 Sim, redefinir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_reset falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RESET_YES}:"))
async def on_reset_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(User).where(User.id == uid))
                u: Optional[User] = r.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return
                u.balance = Decimal("0.00")
                u.balance_locked = Decimal("0.00")
                u.total_spent = Decimal("0.00")
                u.total_deposited = Decimal("0.00")
                u.total_withdrawn = Decimal("0.00")
                u.total_gifts = Decimal("0.00")
                u.purchases_count = 0
                session.add(u)
        except Exception as exc:
            logger.exception(f"reset falhou: {exc}")
            await toast_err(callback, "❌ Erro ao redefinir.")
            return

        await log_action(
            admin_id=admin_id,
            action="user.reset",
            category="admin",
            user_id=uid,
        )

        u2 = await _load_user_by_id(uid)
        if u2 is not None:
            text, keyboard = await _render_profile(u2)
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔄 Conta redefinida.")
    except Exception as exc:
        logger.exception(f"users on_reset_confirm falhou: {exc}")


# ============================================================
# ENVIAR MENSAGEM / NOTIFICAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_MSG}:"))
async def on_send_msg(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        prompt = (
            f"📩 <b>ENVIAR MENSAGEM · {u.first_name or '—'}</b>\n\n"
            "Envie o texto da mensagem. Aceita HTML.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"sendmsg:{uid}",
            screen=f"users:msg:{uid}",
            extra={"user_id": uid},
            use_force_reply=True,
            reply_placeholder="Digite a mensagem...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_send_msg falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NOTIFY}:"))
async def on_notify(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        u = await _load_user_by_id(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        prompt = (
            f"🔔 <b>NOTIFICAR · {u.first_name or '—'}</b>\n\n"
            "Envie o texto da notificação. Aceita HTML.\n"
            "Será enviada com prefixo <b>🔔 Notificação:</b>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"notify:{uid}",
            screen=f"users:notify:{uid}",
            extra={"user_id": uid},
            use_force_reply=True,
            reply_placeholder="Digite a notificação...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_notify falhou: {exc}")


async def _send_admin_message(
    message: Message,
    admin_id: int,
    uid: int,
    text_body: str,
    *,
    as_notify: bool,
) -> None:
    body = (text_body or "").strip()
    if not body:
        await message.answer("❌ Texto vazio.")
        return

    u = await _load_user_by_id(uid)
    if u is None:
        await message.answer("❌ Usuário não encontrado.")
        return

    final = body if not as_notify else f"🔔 <b>Notificação:</b>\n\n{body}"

    try:
        await message.bot.send_message(
            chat_id=int(u.telegram_id),
            text=final,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug(f"envio admin msg falhou: {exc}")
        await message.answer("❌ Não foi possível enviar.")
        return

    await log_action(
        admin_id=admin_id,
        action="user.message_sent" if not as_notify else "user.notified",
        category="admin",
        user_id=uid,
        details={"preview": body[:100]},
    )

    await message.answer("✅ Mensagem enviada.")


# ============================================================
# TRANSAÇÕES / COMPRAS / INDICAÇÕES / COMISSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TX}:"))
async def on_view_transactions(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Transaction)
                    .where(Transaction.user_id == uid)
                    .order_by(Transaction.created_at.desc())
                    .limit(20)
                )
                txs = list(res.scalars().all())
        except Exception:
            txs = []

        if not txs:
            text = "📜 <i>Nenhuma transação registrada.</i>"
        else:
            lines = [f"📜 <b>TRANSAÇÕES · #{uid}</b>", ""]
            for t in txs:
                kind = str(getattr(t.type, "value", t.type) or "")
                sign = "+" if kind in ("DEPOSIT", "REFUND", "COMMISSION", "GIFT", "ADMIN_CREDIT", "BONUS") else "-"
                lines.append(
                    f"{sign} {fmt_brl(t.amount)} · <code>{kind}</code> · "
                    f"{fmt_dt(t.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{uid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_view_transactions falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_ORDERS}:"))
async def on_view_orders(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Order)
                    .where(Order.user_id == uid)
                    .order_by(Order.created_at.desc())
                    .limit(20)
                )
                orders = list(res.scalars().all())
        except Exception:
            orders = []

        if not orders:
            text = "🛒 <i>Nenhuma compra registrada.</i>"
        else:
            lines = [f"🛒 <b>COMPRAS · #{uid}</b>", ""]
            for o in orders:
                s = str(getattr(o.status, "value", o.status) or "")
                lines.append(
                    f"#{o.id} · {fmt_brl(o.final_total)} · {s} · "
                    f"{fmt_dt(o.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{uid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_view_orders falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFS}:"))
async def on_view_refs(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(User)
                    .where(User.referred_by_id == uid)
                    .order_by(User.created_at.desc())
                    .limit(30)
                )
                refs = list(res.scalars().all())
        except Exception:
            refs = []

        if not refs:
            text = "👥 <i>Nenhuma indicação registrada.</i>"
        else:
            lines = [f"👥 <b>INDICAÇÕES · #{uid}</b> ({len(refs)})", ""]
            for r in refs:
                lines.append(
                    f"#{r.id} · {r.first_name or '—'} · "
                    f"{fmt_brl(r.balance)} · {fmt_dt(r.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{uid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_view_refs falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_COMMS}:"))
async def on_view_commissions(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            uid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Commission)
                    .where(Commission.earner_id == uid)
                    .order_by(Commission.created_at.desc())
                    .limit(20)
                )
                comms = list(res.scalars().all())
        except Exception:
            comms = []

        if not comms:
            text = "💸 <i>Nenhuma comissão registrada.</i>"
        else:
            lines = [f"💸 <b>COMISSÕES · #{uid}</b>", ""]
            for c in comms:
                s = str(getattr(c.status, "value", c.status) or "")
                lines.append(
                    f"{fmt_brl(c.amount)} · {s} · "
                    f"{fmt_dt(c.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{uid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"users on_view_commissions falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_filter",
    "on_search",
    "on_users_text_input",
    "on_view",
    "on_balance_change",
    "on_change_status",
    "on_edit_field",
    "on_reset",
    "on_reset_confirm",
    "on_send_msg",
    "on_notify",
    "on_view_transactions",
    "on_view_orders",
    "on_view_refs",
    "on_view_commissions",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_STATUS",
    "CB_ADD_BAL",
    "CB_REM_BAL",
    "CB_BAL_CONFIRM",
    "CB_EDIT_FIELD",
    "CB_RESET",
    "CB_RESET_YES",
    "CB_MSG",
    "CB_NOTIFY",
    "CB_TX",
    "CB_ORDERS",
    "CB_REFS",
    "CB_COMMS",
    "FILTERS",
    "TEXT_FIELDS",
]
