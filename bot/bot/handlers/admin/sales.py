"""
Larizinha Store — Módulo Admin: Vendas / Pedidos
Implementa listagem completa de pedidos com filtros + detalhes + ações.

Telas:
- Lista de pedidos (filtros: pendentes/aprovados/cancelados, hoje/mês, buscar, paginação DIM)
- Detalhes do pedido (com credenciais entregues)
- Reenviar entrega (comprovante + credenciais ao usuário)
- Reembolsar (devolve saldo + cria Transaction + marca REFUNDED)
- Cancelar pedido
- Histórico de entregas
- Relatório de vendas

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

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
    Order,
    OrderItem,
    OrderStatus,
    Product,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_CANCEL,
    SYM_DONE,
    SYM_ERR,
    SYM_PENDING,
    SYM_PROCESSING,
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
    kb,
    log_action,
    page_offset,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_sales")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_sales_list"
MSG_KEY_EMPTY = "admin_sales_empty"
MSG_KEY_DETAIL = "admin_sales_detail"
MSG_KEY_REPORT = "admin_sales_report"

SCREEN_LIST = "admin_sales_list"
SCREEN_DETAIL = "admin_sales_detail"
SCREEN_REPORT = "admin_sales_report"

# Callbacks base
CB_ROOT = "admin:sales"
CB_PAGE = "admin:sales:page"           # :N
CB_FILTER = "admin:sales:filter"       # :key
CB_SEARCH = "admin:sales:search"
CB_VIEW = "admin:sales:view"           # :ID
CB_RESEND = "admin:sales:resend"       # :ID
CB_REFUND = "admin:sales:refund"       # :ID
CB_REFUND_YES = "admin:sales:refund:yes"  # :ID
CB_CANCEL = "admin:sales:cancel"       # :ID
CB_CANCEL_YES = "admin:sales:cancel:yes"  # :ID
CB_HISTORY = "admin:sales:history"
CB_REPORT = "admin:sales:report"
CB_PAYMENTS = "admin:sales:payments"

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "pending": "🟡 Pendentes",
    "delivered": "🟢 Aprovados",
    "canceled": "❌ Cancelados",
    "refunded": "♻️ Reembolsados",
    "today": "📅 Hoje",
    "month": "📅 Este mês",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": SYM_PENDING,
        "PAID": SYM_DONE,
        "DELIVERED": SYM_DONE,
        "CANCELED": SYM_CANCEL,
        "REFUNDED": SYM_WARN,
        "EXPIRED": SYM_ERR,
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": "Pendente",
        "PAID": "Pago",
        "DELIVERED": "Entregue",
        "CANCELED": "Cancelado",
        "REFUNDED": "Reembolsado",
        "EXPIRED": "Expirado",
    }.get(s, str(status))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "pending":
        return stmt.where(Order.status == OrderStatus.PENDING)
    if filter_key == "delivered":
        return stmt.where(Order.status == OrderStatus.DELIVERED)
    if filter_key == "canceled":
        return stmt.where(Order.status == OrderStatus.CANCELED)
    if filter_key == "refunded":
        return stmt.where(Order.status == OrderStatus.REFUNDED)
    if filter_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(Order.created_at >= start)
    if filter_key == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(Order.created_at >= start)
    return stmt


async def _count_orders(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Order.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_orders(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Order]:
    try:
        async with session_scope() as session:
            stmt = select(Order).order_by(Order.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_order(oid: int) -> Optional[Order]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Order).where(Order.id == oid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_order_items(oid: int) -> List[OrderItem]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(OrderItem).where(OrderItem.order_id == oid)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_user(uid: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == uid))
            return res.scalar_one_or_none()
    except Exception:
        return None


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_orders(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    orders = await _list_orders(page, filter_key)

    if not orders:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🛒 <b>PEDIDOS</b>\n\n"
                "<i>Nenhum pedido encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for o in orders:
            emoji = _status_emoji(o.status)
            rows.append(
                [
                    str(o.id),
                    str(o.user_id),
                    fmt_brl(o.final_total),
                    str(getattr(o.method, "value", o.method) or ""),
                    emoji,
                    fmt_dt(o.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "User", "Valor", "Método", "S", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🛒 <b>PEDIDOS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
        )

    # ---------- Teclado ----------
    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="🟡 Pendentes", callback_data=f"{CB_FILTER}:pending"),
                InlineKeyboardButton(text="🟢 Aprovados", callback_data=f"{CB_FILTER}:delivered"),
            ],
            [
                InlineKeyboardButton(text="❌ Cancelados", callback_data=f"{CB_FILTER}:canceled"),
                InlineKeyboardButton(text="♻️ Reembolsados", callback_data=f"{CB_FILTER}:refunded"),
            ],
            [
                InlineKeyboardButton(text="📅 Hoje", callback_data=f"{CB_FILTER}:today"),
                InlineKeyboardButton(text="📅 Mês", callback_data=f"{CB_FILTER}:month"),
            ],
            [
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
                InlineKeyboardButton(text="📊 Relatório", callback_data=CB_REPORT),
            ],
            [
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
                InlineKeyboardButton(text="📜 Entregas", callback_data=CB_HISTORY),
            ],
        ]

    # Botões por pedido (até 8)
    for o in orders:
        emoji = _status_emoji(o.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{o.id} · {fmt_brl(o.final_total)}",
                    callback_data=f"{CB_VIEW}:{o.id}",
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
        logger.exception(f"sales on_root falhou: {exc}")


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
        logger.exception(f"sales on_page falhou: {exc}")


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
        logger.exception(f"sales on_filter falhou: {exc}")


# ============================================================
# BUSCAR
# ============================================================
@router.callback_query(F.data == CB_SEARCH)
async def on_search(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        prompt = (
            "🔍 <b>BUSCAR PEDIDO</b>\n\n"
            "Envie o ID do pedido (número).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="search_order_id",
            screen="sales:search",
            use_force_reply=True,
            reply_placeholder="ID do pedido",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_sales_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return
    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if awaiting != "search_order_id":
        return

    value, _extra = await consume_input(message, admin_id, expected_field="search_order_id")
    if value is None:
        return

    try:
        oid = int(value.strip())
    except Exception:
        await message.answer("❌ ID inválido.")
        return

    order = await _load_order(oid)
    if order is None:
        await message.answer("❌ Pedido não encontrado.")
        return
    await _show_order_detail_by_id(message, admin_id, oid)


# ============================================================
# DETALHE
# ============================================================
async def _show_order_detail(
    target: Union[CallbackQuery, Message],
    oid: int,
) -> Optional[Message]:
    order = await _load_order(oid)
    if order is None:
        return None

    items = await _load_order_items(oid)
    user = await _load_user(int(order.user_id))

    # Primeira credencial
    first_cred: Optional[Dict[str, Any]] = None
    try:
        if items and items[0].credentials:
            creds = items[0].credentials
            if isinstance(creds, list) and creds:
                first_cred = creds[0] if isinstance(creds[0], dict) else {"senha": str(creds[0])}
    except Exception:
        first_cred = None

    email = (first_cred or {}).get("email", "—")
    senha = (first_cred or {}).get("senha", "—")

    prod_name = "—"
    if items:
        prod_name = items[0].product_name or "—"

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"💰 <b>VENDA #{order.id}</b>\n"
            f"👤 Cliente: {(user.first_name if user else '—')} "
            f"(<code>{user.telegram_id if user else '—'}</code>)\n"
            f"📦 Produto: {prod_name}\n"
            f"💰 Valor: {fmt_brl(order.final_total)}\n"
            f"📆 Data: {fmt_dt(order.created_at)}\n"
            f"⏰ Vencimento: {fmt_dt(order.expires_at)}\n"
            f"💳 Método: {getattr(order.method, 'value', order.method)}\n"
            f"🟢 Status: {_status_label(order.status)}\n\n"
            f"📧 Email: <code>{email}</code>\n"
            f"🔐 Senha: <code>{senha}</code>\n"
            f"🎫 ID: <code>{order.id}</code>"
        ),
        id=order.id,
        nome=user.first_name if user else "—",
        id_telegram=user.telegram_id if user else "—",
        produto=prod_name,
        valor=fmt_brl(order.final_total),
        data=fmt_dt(order.created_at),
        venc=fmt_dt(order.expires_at),
        servico=prod_name,
        email=email,
        senha=senha,
        id_compra=order.id,
        metodo=str(getattr(order.method, "value", order.method) or "—"),
        status=_status_label(order.status),
    )

    keyboard = await button_engine.inline(SCREEN_DETAIL)
    if keyboard is None:
        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="🔄 Reenviar",
                    callback_data=f"{CB_RESEND}:{order.id}",
                ),
                InlineKeyboardButton(
                    text="♻️ Reembolsar",
                    callback_data=f"{CB_REFUND}:{order.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Cancelar",
                    callback_data=f"{CB_CANCEL}:{order.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                )
            ],
        ]
        keyboard = kb(rows_kb)

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


async def _show_order_detail_by_id(
    message: Message, admin_id: int, oid: int
) -> None:
    """Mostra o detalhe a partir de uma mensagem (busca)."""
    order = await _load_order(oid)
    if order is None:
        await message.answer("❌ Pedido não encontrado.")
        return

    items = await _load_order_items(oid)
    user = await _load_user(int(order.user_id))

    first_cred: Optional[Dict[str, Any]] = None
    try:
        if items and items[0].credentials:
            creds = items[0].credentials
            if isinstance(creds, list) and creds:
                first_cred = creds[0] if isinstance(creds[0], dict) else {"senha": str(creds[0])}
    except Exception:
        first_cred = None

    email = (first_cred or {}).get("email", "—")
    senha = (first_cred or {}).get("senha", "—")

    prod_name = items[0].product_name if items else "—"

    text = (
        f"💰 <b>VENDA #{order.id}</b>\n"
        f"👤 Cliente: {(user.first_name if user else '—')} "
        f"(<code>{user.telegram_id if user else '—'}</code>)\n"
        f"📦 Produto: {prod_name}\n"
        f"💰 Valor: {fmt_brl(order.final_total)}\n"
        f"📆 Data: {fmt_dt(order.created_at)}\n"
        f"🟢 Status: {_status_label(order.status)}\n\n"
        f"📧 Email: <code>{email}</code>\n"
        f"🔐 Senha: <code>{senha}</code>"
    )

    keyboard = kb(
        [
            [
                InlineKeyboardButton(
                    text="🔄 Reenviar",
                    callback_data=f"{CB_RESEND}:{order.id}",
                ),
                InlineKeyboardButton(
                    text="♻️ Reembolsar",
                    callback_data=f"{CB_REFUND}:{order.id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                )
            ],
        ]
    )

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            oid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        await _show_order_detail(callback, oid)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_view falhou: {exc}")


# ============================================================
# REENVIAR ENTREGA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESEND}:"))
async def on_resend(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            oid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        order = await _load_order(oid)
        if order is None:
            await toast_err(callback, "❌ Pedido não encontrado.")
            return

        user = await _load_user(int(order.user_id))
        if user is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        items = await _load_order_items(oid)
        prod_name = items[0].product_name if items else "—"

        # Reenvia as credenciais por DM
        try:
            cred_lines: List[str] = []
            for item in items:
                creds = item.credentials or []
                if isinstance(creds, list):
                    for c in creds:
                        if isinstance(c, dict):
                            email = c.get("email") or c.get("login") or "—"
                            senha = c.get("senha") or c.get("password") or "—"
                            cred_lines.append(f"📧 {email} · 🔐 {senha}")
                        else:
                            cred_lines.append(f"🔐 {c}")

            body = (
                f"🔄 <b>REENVIO DE ENTREGA</b>\n\n"
                f"📦 <b>{prod_name}</b>\n"
                f"💰 Valor: {fmt_brl(order.final_total)}\n"
                f"🎫 Pedido: <code>#{order.id}</code>\n\n"
            )
            if cred_lines:
                body += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                body += "\n".join(cred_lines)

            await callback.bot.send_message(
                chat_id=int(user.telegram_id),
                text=body,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await toast_ok(callback, "🔄 Reenviado com sucesso.")
        except Exception as exc:
            logger.debug(f"reenvio falhou: {exc}")
            await toast_err(callback, "❌ Não foi possível reenviar.")

        await log_action(
            admin_id=admin_id,
            action="sale.resent",
            category="admin",
            details={"order_id": oid},
        )
    except Exception as exc:
        logger.exception(f"sales on_resend falhou: {exc}")


# ============================================================
# REEMBOLSAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REFUND}:"))
async def on_refund(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Ignora se for refund:yes
        parts = callback.data.split(":")
        if len(parts) >= 4 and parts[3] == "yes":
            return

        try:
            oid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        order = await _load_order(oid)
        if order is None:
            await toast_err(callback, "❌ Pedido não encontrado.")
            return

        if str(getattr(order.status, "value", order.status)).upper() in ("REFUNDED", "CANCELED"):
            await toast_err(callback, "❌ Este pedido já foi reembolsado/cancelado.")
            return

        text = (
            f"♻️ <b>REEMBOLSAR PEDIDO #{oid}</b>\n\n"
            f"Valor a devolver: <b>{fmt_brl(order.final_total)}</b>\n"
            f"Usuário: <code>{order.user_id}</code>\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REFUND_YES}:{oid}",
            cancel_callback=f"{CB_VIEW}:{oid}",
            confirm_text="♻️ Sim, reembolsar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_refund falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFUND_YES}:"))
async def on_refund_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            oid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Carrega o pedido
                r = await session.execute(
                    select(Order).where(Order.id == oid).with_for_update()
                )
                order: Optional[Order] = r.scalar_one_or_none()
                if order is None:
                    await toast_err(callback, "❌ Pedido não encontrado.")
                    return

                if str(getattr(order.status, "value", order.status)).upper() == "REFUNDED":
                    await toast_err(callback, "❌ Já reembolsado.")
                    return

                valor = float(order.final_total or 0)

                # Credita o usuário
                ures = await session.execute(
                    select(User).where(User.id == order.user_id).with_for_update()
                )
                u: Optional[User] = ures.scalar_one_or_none()
                if u is not None:
                    new_balance = round(float(u.balance or 0) + valor, 2)
                    u.balance = Decimal(str(new_balance))
                    session.add(u)

                    session.add(
                        Transaction(
                            user_id=u.id,
                            type=TransactionType.REFUND,
                            amount=Decimal(str(round(valor, 2))),
                            balance_after=Decimal(str(new_balance)),
                            description=f"Reembolso pedido #{oid}",
                            reference=f"order:{oid}",
                        )
                    )

                # Marca pedido
                order.status = OrderStatus.REFUNDED
                order.refunded_at = datetime.now(timezone.utc)
                session.add(order)

                tg_id = int(u.telegram_id) if u is not None else None
        except Exception as exc:
            logger.exception(f"refund falhou: {exc}")
            await toast_err(callback, "❌ Erro ao reembolsar.")
            return

        # Notifica o usuário
        try:
            if tg_id:
                await callback.bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"♻️ <b>Reembolso realizado</b>\n\n"
                        f"Pedido <code>#{oid}</code>\n"
                        f"Valor: <b>{fmt_brl(valor)}</b>\n"
                        f"O valor foi devolvido ao seu saldo."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="sale.refunded",
            category="admin",
            details={"order_id": oid, "amount": valor},
        )

        await _show_order_detail(callback, oid)
        await toast_ok(callback, "♻️ Pedido reembolsado.")
    except Exception as exc:
        logger.exception(f"sales on_refund_confirm falhou: {exc}")


# ============================================================
# CANCELAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
async def on_cancel_order(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 4 and parts[3] == "yes":
            return

        try:
            oid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        order = await _load_order(oid)
        if order is None:
            await toast_err(callback, "❌ Pedido não encontrado.")
            return

        if str(getattr(order.status, "value", order.status)).upper() in ("CANCELED", "REFUNDED"):
            await toast_err(callback, "❌ Já cancelado/reembolsado.")
            return

        text = (
            f"🗑️ <b>CANCELAR PEDIDO #{oid}</b>\n\n"
            "Isso marcará o pedido como <b>cancelado</b>.\n"
            "NÃO devolve o saldo automaticamente (use o botão Reembolsar para isso).\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CANCEL_YES}:{oid}",
            cancel_callback=f"{CB_VIEW}:{oid}",
            confirm_text="🗑️ Sim, cancelar",
            cancel_text="❌ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_cancel_order falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CANCEL_YES}:"))
async def on_cancel_order_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            oid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Order).where(Order.id == oid))
                order: Optional[Order] = r.scalar_one_or_none()
                if order is None:
                    await toast_err(callback, "❌ Pedido não encontrado.")
                    return
                order.status = OrderStatus.CANCELED
                session.add(order)
        except Exception as exc:
            logger.exception(f"cancel order falhou: {exc}")
            await toast_err(callback, "❌ Erro ao cancelar.")
            return

        await log_action(
            admin_id=admin_id,
            action="sale.canceled",
            category="admin",
            details={"order_id": oid},
        )

        await _show_order_detail(callback, oid)
        await toast_ok(callback, "🗑️ Pedido cancelado.")
    except Exception as exc:
        logger.exception(f"sales on_cancel_order_confirm falhou: {exc}")


# ============================================================
# HISTÓRICO DE ENTREGAS
# ============================================================
@router.callback_query(F.data == CB_HISTORY)
async def on_history(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Order)
                    .where(Order.status == OrderStatus.DELIVERED)
                    .order_by(Order.delivered_at.desc())
                    .limit(20)
                )
                orders = list(res.scalars().all())
        except Exception:
            orders = []

        if not orders:
            text = (
                "📜 <b>HISTÓRICO DE ENTREGAS</b>\n\n"
                "<i>Nenhuma entrega registrada.</i>"
            )
        else:
            lines = ["📜 <b>HISTÓRICO DE ENTREGAS (últimas 20)</b>", ""]
            for o in orders:
                lines.append(
                    f"🟢 #{o.id} · User <code>{o.user_id}</code> · "
                    f"{fmt_brl(o.final_total)} · {fmt_dt(o.delivered_at)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
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
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_history falhou: {exc}")


# ============================================================
# RELATÓRIO DE VENDAS
# ============================================================
@router.callback_query(F.data == CB_REPORT)
async def on_report(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        now = datetime.now(timezone.utc)
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        try:
            async with session_scope() as session:
                # Total entregue
                res = await session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.final_total), 0),
                    ).where(Order.status == OrderStatus.DELIVERED)
                )
                row = res.first()
                total_count = int(row[0] or 0)
                total_value = float(row[1] or 0)

                # Hoje
                res = await session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.final_total), 0),
                    ).where(
                        Order.status == OrderStatus.DELIVERED,
                        Order.created_at >= start_today,
                    )
                )
                row = res.first()
                today_count = int(row[0] or 0)
                today_value = float(row[1] or 0)

                # Mês
                res = await session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.final_total), 0),
                    ).where(
                        Order.status == OrderStatus.DELIVERED,
                        Order.created_at >= start_month,
                    )
                )
                row = res.first()
                month_count = int(row[0] or 0)
                month_value = float(row[1] or 0)

                # Pendentes
                res = await session.execute(
                    select(func.count(Order.id)).where(
                        Order.status == OrderStatus.PENDING
                    )
                )
                pending_count = int(res.scalar() or 0)

        except Exception as exc:
            logger.debug(f"relatório vendas falhou: {exc}")
            await toast_err(callback, "❌ Erro ao gerar relatório.")
            return

        ticket_medio = round(total_value / total_count, 2) if total_count else 0.0

        text = (
            "📊 <b>RELATÓRIO DE VENDAS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📈 <b>TOTAL GERAL</b>\n"
            f"├ Pedidos entregues: <b>{fmt_num(total_count)}</b>\n"
            f"├ Faturamento: <b>{fmt_brl(total_value)}</b>\n"
            f"└ Ticket médio: <b>{fmt_brl(ticket_medio)}</b>\n\n"
            "📅 <b>HOJE</b>\n"
            f"├ Pedidos: <b>{fmt_num(today_count)}</b>\n"
            f"└ Faturamento: <b>{fmt_brl(today_value)}</b>\n\n"
            "📆 <b>MÊS ATUAL</b>\n"
            f"├ Pedidos: <b>{fmt_num(month_count)}</b>\n"
            f"└ Faturamento: <b>{fmt_brl(month_value)}</b>\n\n"
            "⏳ <b>PENDENTES</b>\n"
            f"└ Aguardando: <b>{fmt_num(pending_count)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        await edit_or_send(
            callback,
            text=text,
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
        await answer(callback)
    except Exception as exc:
        logger.exception(f"sales on_report falhou: {exc}")


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
    "on_sales_text_input",
    "on_view",
    "on_resend",
    "on_refund",
    "on_refund_confirm",
    "on_cancel_order",
    "on_cancel_order_confirm",
    "on_history",
    "on_report",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_RESEND",
    "CB_REFUND",
    "CB_REFUND_YES",
    "CB_CANCEL",
    "CB_CANCEL_YES",
    "CB_HISTORY",
    "CB_REPORT",
    "CB_PAYMENTS",
    "FILTERS",
]
