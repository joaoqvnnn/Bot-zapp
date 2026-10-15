"""
Larizinha Store — Módulo Admin: Depósitos e Pagamentos
Implementa gestão completa de cobranças PIX conforme DIM.

Telas:
- Lista de depósitos (filtros: pendentes/aprovados/recusados/expirados,
  hoje/mês, buscar por TXID, paginação DIM)
- Detalhes do depósito (TXID, cliente, valor, bônus, status, datas)
- Ver comprovante (imagem do QR)
- Aprovar manual (credita saldo + bônus + transaction + comissão)
- Recusar (marca REFUSED/EXPIRED sem creditar)
- Reembolsar (devolve valor pago — usa refund interno)
- Reprocessar (reconsulta status no MP)
- Conciliação (total PIX pago x saldo creditado x comissões)
- Config de depósitos (min/max/métodos/taxa/expiração/bônus/reembolso)

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
    Commission,
    CommissionStatus,
    PixCharge,
    PixStatus,
    Setting,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.services.pix_service import pix_service
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_CANCEL,
    SYM_DONE,
    SYM_ERR,
    SYM_PENDING,
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
router = Router(name="admin_payments")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_payments_list"
MSG_KEY_EMPTY = "admin_payments_empty"
MSG_KEY_DETAIL = "admin_payments_detail"
MSG_KEY_CONFIG = "admin_payments_config"
MSG_KEY_RECON = "admin_payments_recon"

SCREEN_LIST = "admin_payments_list"
SCREEN_DETAIL = "admin_payments_detail"
SCREEN_CONFIG = "admin_payments_config"

# Callbacks base
CB_ROOT = "admin:payments"
CB_PAGE = "admin:payments:page"           # :filter:N
CB_FILTER = "admin:payments:filter"       # :key
CB_SEARCH = "admin:payments:search"
CB_VIEW = "admin:payments:view"           # :TXID
CB_QR = "admin:payments:qr"               # :TXID
CB_APPROVE = "admin:payments:approve"     # :TXID
CB_APPROVE_YES = "admin:payments:approve:yes"  # :TXID
CB_REFUSE = "admin:payments:refuse"       # :TXID
CB_REFUSE_YES = "admin:payments:refuse:yes"    # :TXID
CB_REPROCESS = "admin:payments:reprocess" # :TXID
CB_REFUND = "admin:payments:refund"       # :TXID
CB_REFUND_YES = "admin:payments:refund:yes"    # :TXID
CB_RECON = "admin:payments:recon"
CB_CONFIG = "admin:payments:config"
CB_CONFIG_SET = "admin:payments:cfgset"   # :field
CB_EXPORT = "admin:payments:export"

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "pending": "🟡 Pendentes",
    "paid": "🟢 Aprovados",
    "expired": "⌛️ Expirados",
    "canceled": "❌ Recusados",
    "today": "📅 Hoje",
    "month": "📅 Este mês",
}

# Campos configuráveis (Setting keys)
CONFIG_FIELDS: Dict[str, Dict[str, Any]] = {
    "min_deposit": {"label": "Valor mínimo", "type": "money", "default": str(settings.MIN_DEPOSIT)},
    "max_deposit": {"label": "Valor máximo", "type": "money", "default": str(settings.MAX_DEPOSIT)},
    "pix_enabled": {"label": "PIX habilitado", "type": "bool", "default": "true"},
    "pix_fee_percent": {"label": "Taxa PIX (%)", "type": "number", "default": "0"},
    "auto_refund": {"label": "Reembolso automático", "type": "bool", "default": "false"},
    "pix_expiration_minutes": {
        "label": "Expiração PIX (min)",
        "type": "int",
        "default": str(settings.MP_PIX_EXPIRATION_MINUTES),
    },
    "bonus_percent": {
        "label": "Bônus de recarga (%)",
        "type": "number",
        "default": str(settings.DEPOSIT_BONUS_PERCENT),
    },
    "bonus_min": {
        "label": "Valor mínimo p/ bônus",
        "type": "money",
        "default": str(settings.DEPOSIT_BONUS_MIN),
    },
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": SYM_PENDING,
        "PAID": SYM_DONE,
        "EXPIRED": SYM_WARN,
        "CANCELED": SYM_CANCEL,
        "REFUNDED": SYM_WARN,
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": "Pendente",
        "PAID": "Aprovado",
        "EXPIRED": "Expirado",
        "CANCELED": "Recusado",
        "REFUNDED": "Reembolsado",
    }.get(s, str(status))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "pending":
        return stmt.where(PixCharge.status == PixStatus.PENDING)
    if filter_key == "paid":
        return stmt.where(PixCharge.status == PixStatus.PAID)
    if filter_key == "expired":
        return stmt.where(PixCharge.status == PixStatus.EXPIRED)
    if filter_key == "canceled":
        return stmt.where(PixCharge.status == PixStatus.CANCELED)
    if filter_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(PixCharge.created_at >= start)
    if filter_key == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(PixCharge.created_at >= start)
    return stmt


async def _count_charges(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(PixCharge.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_charges(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[PixCharge]:
    try:
        async with session_scope() as session:
            stmt = select(PixCharge).order_by(PixCharge.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_charge(txid: str) -> Optional[PixCharge]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(PixCharge).where(PixCharge.txid == txid)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_user(uid: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == uid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is not None:
                return s.value
    except Exception:
        pass
    return default


async def _set_setting(key: str, value: str, category: str = "payments") -> bool:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is None:
                session.add(Setting(key=key, value=value, category=category))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting falhou: {exc}")
        return False


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_charges(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    charges = await _list_charges(page, filter_key)

    if not charges:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "💳 <b>DEPÓSITOS</b>\n\n"
                "<i>Nenhum depósito encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for c in charges:
            emoji = _status_emoji(c.status)
            txid_short = (c.txid or "")[-8:]
            rows.append(
                [
                    txid_short,
                    str(c.user_id),
                    fmt_brl(c.amount),
                    emoji,
                    fmt_dt(c.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["TXID", "User", "Valor", "S", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"💳 <b>DEPÓSITOS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="🟡 Pendentes", callback_data=f"{CB_FILTER}:pending"),
                InlineKeyboardButton(text="🟢 Aprovados", callback_data=f"{CB_FILTER}:paid"),
            ],
            [
                InlineKeyboardButton(text="⌛️ Expirados", callback_data=f"{CB_FILTER}:expired"),
                InlineKeyboardButton(text="❌ Recusados", callback_data=f"{CB_FILTER}:canceled"),
            ],
            [
                InlineKeyboardButton(text="📅 Hoje", callback_data=f"{CB_FILTER}:today"),
                InlineKeyboardButton(text="📅 Mês", callback_data=f"{CB_FILTER}:month"),
            ],
            [
                InlineKeyboardButton(text="🔍 Buscar TXID", callback_data=CB_SEARCH),
                InlineKeyboardButton(text="🔍 Conciliação", callback_data=CB_RECON),
            ],
            [
                InlineKeyboardButton(text="⚙️ Config", callback_data=CB_CONFIG),
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for c in charges:
        emoji = _status_emoji(c.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {(c.txid or '')[-8:]} · {fmt_brl(c.amount)}",
                    callback_data=f"{CB_VIEW}:{c.txid}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{filter_key}",
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
        await render_list(callback, page=1, filter_key="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # formato: admin:payments:page:FILTER:N
        parts = callback.data.split(":")
        filter_key = "all"
        page = 1
        if len(parts) >= 4:
            filter_key = parts[3]
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except Exception:
                page = 1
        if filter_key not in FILTERS:
            filter_key = "all"
        await render_list(callback, page=page, filter_key=filter_key)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_page falhou: {exc}")


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
        logger.exception(f"payments on_filter falhou: {exc}")


# ============================================================
# BUSCAR POR TXID
# ============================================================
@router.callback_query(F.data == CB_SEARCH)
async def on_search(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "🔍 <b>BUSCAR POR TXID</b>\n\n"
            "Envie o TXID (ou parte) do depósito.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="payments_search_txid",
            screen="payments:search",
            use_force_reply=True,
            reply_placeholder="TXID",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_payments_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Busca por TXID
    if awaiting == "payments_search_txid":
        value, _extra = await consume_input(message, admin_id, expected_field="payments_search_txid")
        if value is None:
            return
        await _do_search_txid(message, admin_id, value)
        return

    # Config: campos
    if awaiting.startswith("paycfg:"):
        field = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_config_field(message, admin_id, field, value)
        return


async def _do_search_txid(message: Message, admin_id: int, value: str) -> None:
    value = (value or "").strip().upper()
    if not value:
        await message.answer("❌ TXID vazio.")
        return

    try:
        async with session_scope() as session:
            res = await session.execute(
                select(PixCharge)
                .where(PixCharge.txid.like(f"%{value}%"))
                .order_by(PixCharge.created_at.desc())
                .limit(10)
            )
            charges = list(res.scalars().all())
    except Exception:
        charges = []

    if not charges:
        await message.answer(
            text="🔍 <b>NENHUM RESULTADO</b>",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    rows_kb: List[List[InlineKeyboardButton]] = []
    for c in charges:
        emoji = _status_emoji(c.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} {c.txid} · {fmt_brl(c.amount)}",
                    callback_data=f"{CB_VIEW}:{c.txid}",
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

    await message.answer(
        text=f"🔍 <b>RESULTADOS ({len(charges)})</b>",
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(txid: str) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    charge = await _load_charge(txid)
    if charge is None:
        return None, None

    user = await _load_user(int(charge.user_id))

    status_emoji = _status_emoji(charge.status)

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"💳 <b>DEPÓSITO</b>\n\n"
            f"├ TXID: <code>{charge.txid}</code>\n"
            f"├ Cliente: {(user.first_name if user else '—')} "
            f"(<code>{user.telegram_id if user else '—'}</code>)\n"
            f"├ Valor: <b>{fmt_brl(charge.amount)}</b>\n"
            f"├ Bônus: {fmt_brl(charge.bonus)}\n"
            f"├ Status: {status_emoji} {_status_label(charge.status)}\n"
            f"├ Criado em: {fmt_dt(charge.created_at)}\n"
            f"├ Expira em: {fmt_dt(charge.expires_at)}\n"
            f"├ Pago em: {fmt_dt(charge.paid_at)}\n"
            f"└ MP ID: <code>{charge.mp_payment_id or '—'}</code>"
        ),
        txid=charge.txid,
        cliente=user.first_name if user else "—",
        id_cliente=user.telegram_id if user else "—",
        valor=fmt_brl(charge.amount),
        bonus=fmt_brl(charge.bonus),
        status=f"{status_emoji} {_status_label(charge.status)}",
        data=fmt_dt(charge.created_at),
        expira=fmt_dt(charge.expires_at),
        pago=fmt_dt(charge.paid_at),
        mp_id=charge.mp_payment_id or "—",
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(charge))

    return rendered.content, keyboard


def _default_detail_keyboard(c: PixCharge) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = [
        [
            b("🖼️ Ver Comprovante", f"{CB_QR}:{c.txid}"),
        ],
    ]

    status = str(getattr(c.status, "value", c.status) or "").upper()
    if status == "PENDING":
        rows.append(
            [
                b("✅ Aprovar Manual", f"{CB_APPROVE}:{c.txid}"),
                b("❌ Recusar", f"{CB_REFUSE}:{c.txid}"),
            ]
        )
        rows.append([b("🔄 Reprocessar", f"{CB_REPROCESS}:{c.txid}")])
    elif status == "PAID":
        rows.append([b("♻️ Reembolsar", f"{CB_REFUND}:{c.txid}")])
    else:
        rows.append([b("🔄 Reprocessar", f"{CB_REPROCESS}:{c.txid}")])

    rows.append([b("🔙 Voltar", CB_ROOT)])
    return rows


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return
        text, keyboard = await _render_detail(txid)
        if text is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_view falhou: {exc}")


# ============================================================
# VER COMPROVANTE (QR)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_QR}:"))
async def on_view_qr(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return

        charge = await _load_charge(txid)
        if charge is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return

        qr_bytes = None
        try:
            from bot.services.pix_service import decode_qr_base64

            qr_bytes = decode_qr_base64(charge.qr_code_base64)
        except Exception:
            qr_bytes = None

        if qr_bytes is None:
            await toast_err(callback, "❌ QR não disponível.")
            return

        try:
            await callback.message.answer_photo(
                photo=qr_bytes,
                caption=(
                    f"🖼️ <b>Comprovante PIX</b>\n\n"
                    f"TXID: <code>{charge.txid}</code>\n"
                    f"Valor: <b>{fmt_brl(charge.amount)}</b>\n"
                    f"Status: {_status_label(charge.status)}"
                ),
                parse_mode="HTML",
            )
            await answer(callback)
        except Exception as exc:
            logger.debug(f"envio QR falhou: {exc}")
            await toast_err(callback, "❌ Erro ao enviar QR.")
    except Exception as exc:
        logger.exception(f"payments on_view_qr falhou: {exc}")


# ============================================================
# APROVAR MANUAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_APPROVE}:"))
async def on_approve(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return

        txid = parts[-1]
        charge = await _load_charge(txid)
        if charge is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return

        if str(getattr(charge.status, "value", charge.status)).upper() == "PAID":
            await toast_err(callback, "❌ Este depósito já está aprovado.")
            return

        user = await _load_user(int(charge.user_id))

        text = (
            f"✅ <b>APROVAR MANUAL</b>\n\n"
            f"TXID: <code>{charge.txid}</code>\n"
            f"Usuário: <b>{user.first_name if user else '—'}</b> "
            f"(<code>{user.telegram_id if user else '—'}</code>)\n"
            f"Valor: <b>{fmt_brl(charge.amount)}</b>\n"
            f"Bônus: <b>{fmt_brl(charge.bonus)}</b>\n\n"
            "Isso vai creditar o saldo do usuário e marcar como pago."
        )

        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_APPROVE_YES}:{txid}",
            cancel_callback=f"{CB_VIEW}:{txid}",
            confirm_text="✅ Sim, aprovar",
            cancel_text="❌ Cancelar",
        )

        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_approve falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_APPROVE_YES}:"))
async def on_approve_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return

        # Marca como pago via serviço (garante transaction + commission)
        try:
            charge = await pix_service.mark_paid(
                txid,
                mp_payment_id=(await _load_charge(txid)).mp_payment_id if await _load_charge(txid) else None,
                paid_amount=None,
            )
            if charge is None:
                await toast_err(callback, "❌ Falha ao aprovar.")
                return
        except Exception as exc:
            logger.exception(f"mark_paid falhou: {exc}")
            await toast_err(callback, "❌ Erro ao aprovar.")
            return

        await log_action(
            admin_id=admin_id,
            action="payment.approved",
            category="payments",
            details={"txid": txid},
        )

        # Notifica o usuário
        try:
            u = await _load_user(int(charge.user_id))
            if u is not None:
                await callback.bot.send_message(
                    chat_id=int(u.telegram_id),
                    text=(
                        f"✅ <b>Pagamento aprovado manualmente</b>\n\n"
                        f"💵 Valor: <b>{fmt_brl(charge.amount)}</b>\n"
                        f"🎁 Bônus: <b>{fmt_brl(charge.bonus)}</b>\n"
                        f"💰 Novo saldo: <b>{fmt_brl(u.balance)}</b>"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        # Re-render do detalhe
        text, keyboard = await _render_detail(txid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "✅ Aprovado.")
    except Exception as exc:
        logger.exception(f"payments on_approve_confirm falhou: {exc}")


# ============================================================
# RECUSAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REFUSE}:"))
async def on_refuse(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        txid = parts[-1]

        charge = await _load_charge(txid)
        if charge is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return

        if str(getattr(charge.status, "value", charge.status)).upper() == "PAID":
            await toast_err(callback, "❌ Não é possível recusar um depósito já pago.")
            return

        text = (
            f"❌ <b>RECUSAR DEPÓSITO</b>\n\n"
            f"TXID: <code>{charge.txid}</code>\n"
            f"Valor: <b>{fmt_brl(charge.amount)}</b>\n\n"
            "Isso marcará como <b>RECUSADO</b> sem creditar saldo.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REFUSE_YES}:{txid}",
            cancel_callback=f"{CB_VIEW}:{txid}",
            confirm_text="❌ Sim, recusar",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_refuse falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFUSE_YES}:"))
async def on_refuse_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return

        try:
            charge = await pix_service.cancel_charge(txid)
            if charge is None:
                # fallback: atualização direta
                async with session_scope() as session:
                    r = await session.execute(
                        select(PixCharge).where(PixCharge.txid == txid)
                    )
                    c: Optional[PixCharge] = r.scalar_one_or_none()
                    if c is not None and str(getattr(c.status, "value", c.status)).upper() != "PAID":
                        c.status = PixStatus.CANCELED
                        session.add(c)
        except Exception as exc:
            logger.exception(f"cancel_charge falhou: {exc}")

        await log_action(
            admin_id=admin_id,
            action="payment.refused",
            category="payments",
            details={"txid": txid},
        )

        text, keyboard = await _render_detail(txid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "❌ Recusado.")
    except Exception as exc:
        logger.exception(f"payments on_refuse_confirm falhou: {exc}")


# ============================================================
# REPROCESSAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REPROCESS}:"))
async def on_reprocess(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return

        charge = await _load_charge(txid)
        if charge is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return

        mp_id = charge.mp_payment_id
        if not mp_id:
            await toast_err(callback, "❌ Sem MP ID para reprocessar.")
            return

        try:
            info = await pix_service.fetch_status(mp_id)
        except Exception as exc:
            logger.debug(f"fetch_status falhou: {exc}")
            info = None

        if not info:
            await toast_err(callback, "❌ Não foi possível consultar o MP.")
            return

        mp_status = str(info.get("status", "")).lower()
        if mp_status in {"approved", "authorized"}:
            try:
                await pix_service.mark_paid(txid, mp_payment_id=mp_id, paid_amount=None)
                await toast_ok(callback, "✅ Pago — saldo creditado.")
            except Exception:
                await toast_err(callback, "❌ Erro ao creditar.")
        elif mp_status in {"cancelled", "refunded", "charged_back"}:
            await pix_service.cancel_charge(txid)
            await toast_ok(callback, "❌ MP cancelou o pagamento.")
        elif mp_status in {"pending", "in_process"}:
            await toast_ok(callback, "⏳ Ainda pendente no MP.")
        else:
            await toast_ok(callback, f"Status MP: {mp_status or '—'}")

        await log_action(
            admin_id=admin_id,
            action="payment.reprocessed",
            category="payments",
            details={"txid": txid, "mp_status": mp_status},
        )

        text, keyboard = await _render_detail(txid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
    except Exception as exc:
        logger.exception(f"payments on_reprocess falhou: {exc}")


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

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        txid = parts[-1]

        charge = await _load_charge(txid)
        if charge is None:
            await toast_err(callback, "❌ Depósito não encontrado.")
            return

        if str(getattr(charge.status, "value", charge.status)).upper() != "PAID":
            await toast_err(callback, "❌ Só é possível reembolsar depósito pago.")
            return

        text = (
            f"♻️ <b>REEMBOLSAR DEPÓSITO</b>\n\n"
            f"TXID: <code>{charge.txid}</code>\n"
            f"Valor: <b>{fmt_brl(charge.amount)}</b>\n\n"
            "Isso vai debitar o valor do saldo do usuário e marcar\n"
            "a cobrança como reembolsada. Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REFUND_YES}:{txid}",
            cancel_callback=f"{CB_VIEW}:{txid}",
            confirm_text="♻️ Sim, reembolsar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_refund falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFUND_YES}:"))
async def on_refund_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ TXID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(PixCharge).where(PixCharge.txid == txid).with_for_update()
                )
                charge: Optional[PixCharge] = r.scalar_one_or_none()
                if charge is None:
                    await toast_err(callback, "❌ Depósito não encontrado.")
                    return
                if str(getattr(charge.status, "value", charge.status)).upper() != "PAID":
                    await toast_err(callback, "❌ Só é possível reembolsar depósito pago.")
                    return

                total = float(charge.amount or 0) + float(charge.bonus or 0)
                uid = int(charge.user_id)

                ures = await session.execute(
                    select(User).where(User.id == uid).with_for_update()
                )
                u: Optional[User] = ures.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return

                if float(u.balance or 0) < total:
                    await toast_err(callback, "❌ Saldo insuficiente para reembolsar.")
                    return

                new_balance = round(float(u.balance or 0) - total, 2)
                u.balance = Decimal(str(new_balance))
                session.add(u)

                session.add(
                    Transaction(
                        user_id=u.id,
                        type=TransactionType.ADMIN_DEBIT,
                        amount=Decimal(str(round(total, 2))),
                        balance_after=Decimal(str(new_balance)),
                        description=f"Reembolso depósito {txid}",
                        reference=f"refund:{txid}",
                    )
                )

                charge.status = PixStatus.REFUNDED
                session.add(charge)

                tg_id = int(u.telegram_id)
        except Exception as exc:
            logger.exception(f"reembolso falhou: {exc}")
            await toast_err(callback, "❌ Erro ao reembolsar.")
            return

        try:
            await callback.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"♻️ <b>Depósito reembolsado</b>\n\n"
                    f"Valor: <b>{fmt_brl(total)}</b>\n"
                    f"O valor foi removido do seu saldo."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="payment.refunded",
            category="payments",
            details={"txid": txid, "total": total},
        )

        text, keyboard = await _render_detail(txid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "♻️ Reembolsado.")
    except Exception as exc:
        logger.exception(f"payments on_refund_confirm falhou: {exc}")


# ============================================================
# CONCILIAÇÃO
# ============================================================
@router.callback_query(F.data == CB_RECON)
async def on_recon(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                # PIX pagos
                r = await session.execute(
                    select(
                        func.count(PixCharge.id),
                        func.coalesce(func.sum(PixCharge.amount), 0),
                        func.coalesce(func.sum(PixCharge.bonus), 0),
                    ).where(PixCharge.status == PixStatus.PAID)
                )
                row = r.first()
                paid_count = int(row[0] or 0)
                paid_value = float(row[1] or 0)
                paid_bonus = float(row[2] or 0)

                # PIX pendentes
                r = await session.execute(
                    select(
                        func.count(PixCharge.id),
                        func.coalesce(func.sum(PixCharge.amount), 0),
                    ).where(PixCharge.status == PixStatus.PENDING)
                )
                row = r.first()
                pend_count = int(row[0] or 0)
                pend_value = float(row[1] or 0)

                # PIX expirados
                r = await session.execute(
                    select(
                        func.count(PixCharge.id),
                        func.coalesce(func.sum(PixCharge.amount), 0),
                    ).where(PixCharge.status == PixStatus.EXPIRED)
                )
                row = r.first()
                exp_count = int(row[0] or 0)
                exp_value = float(row[1] or 0)

                # Comissões geradas por depósitos
                r = await session.execute(
                    select(func.coalesce(func.sum(Commission.amount), 0)).where(
                        Commission.status.in_(
                            (CommissionStatus.APPROVED, CommissionStatus.PAID)
                        )
                    )
                )
                comissoes = float(r.scalar() or 0)
        except Exception as exc:
            logger.debug(f"recon falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        text = (
            "🔍 <b>CONCILIAÇÃO PIX</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 <b>PAGOS</b>\n"
            f"├ Quantidade: <b>{fmt_num(paid_count)}</b>\n"
            f"├ Valor total: <b>{fmt_brl(paid_value)}</b>\n"
            f"└ Bônus creditado: <b>{fmt_brl(paid_bonus)}</b>\n\n"
            "🟡 <b>PENDENTES</b>\n"
            f"├ Quantidade: <b>{fmt_num(pend_count)}</b>\n"
            f"└ Valor: <b>{fmt_brl(pend_value)}</b>\n\n"
            "⌛️ <b>EXPIRADOS</b>\n"
            f"├ Quantidade: <b>{fmt_num(exp_count)}</b>\n"
            f"└ Valor: <b>{fmt_brl(exp_value)}</b>\n\n"
            "💰 <b>COMISSÕES</b>\n"
            f"└ Total: <b>{fmt_brl(comissoes)}</b>\n"
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
        logger.exception(f"payments on_recon falhou: {exc}")


# ============================================================
# CONFIG (Setting)
# ============================================================
@router.callback_query(F.data == CB_CONFIG)
async def on_config(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Lê valores atuais
        vals: Dict[str, str] = {}
        for k, meta in CONFIG_FIELDS.items():
            v = await _get_setting(k, meta.get("default"))
            vals[k] = str(v or "")

        rendered = await message_engine.render(
            MSG_KEY_CONFIG,
            fallback=(
                "⚙️ <b>CONFIG DEPÓSITOS</b>\n\n"
                f"├ Valor mínimo: <b>{fmt_brl(float(vals['min_deposit']))}</b>\n"
                f"├ Valor máximo: <b>{fmt_brl(float(vals['max_deposit']))}</b>\n"
                f"├ PIX: {'🟢' if vals['pix_enabled'] == 'true' else '🔴'}\n"
                f"├ Taxa PIX: {vals['pix_fee_percent']}%\n"
                f"├ Expiração PIX: {vals['pix_expiration_minutes']} min\n"
                f"├ Bônus: {vals['bonus_percent']}%\n"
                f"├ Bônus mínimo: <b>{fmt_brl(float(vals['bonus_min']))}</b>\n"
                f"└ Reembolso automático: "
                f"{'🟢' if vals['auto_refund'] == 'true' else '🔴'}"
            ),
            min=fmt_brl(float(vals["min_deposit"])),
            max=fmt_brl(float(vals["max_deposit"])),
            metodos="PIX",
            taxa=vals["pix_fee_percent"],
            pix="🟢" if vals["pix_enabled"] == "true" else "🔴",
            reembolso="🟢" if vals["auto_refund"] == "true" else "🔴",
        )

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="✏️ Mínimo", callback_data=f"{CB_CONFIG_SET}:min_deposit"),
                InlineKeyboardButton(text="✏️ Máximo", callback_data=f"{CB_CONFIG_SET}:max_deposit"),
            ],
            [
                InlineKeyboardButton(text="🔄 PIX on/off", callback_data=f"{CB_CONFIG_SET}:pix_enabled"),
                InlineKeyboardButton(text="✏️ Taxa %", callback_data=f"{CB_CONFIG_SET}:pix_fee_percent"),
            ],
            [
                InlineKeyboardButton(text="✏️ Expiração", callback_data=f"{CB_CONFIG_SET}:pix_expiration_minutes"),
            ],
            [
                InlineKeyboardButton(text="✏️ Bônus %", callback_data=f"{CB_CONFIG_SET}:bonus_percent"),
                InlineKeyboardButton(text="✏️ Bônus mín", callback_data=f"{CB_CONFIG_SET}:bonus_min"),
            ],
            [
                InlineKeyboardButton(text="🔄 Reembolso auto", callback_data=f"{CB_CONFIG_SET}:auto_refund"),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_config falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CONFIG_SET}:"))
async def on_config_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            field = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Campo inválido.")
            return

        if field not in CONFIG_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = CONFIG_FIELDS[field]

        # Booleans → toggle direto
        if meta["type"] == "bool":
            current = await _get_setting(field, meta.get("default", "false"))
            new_val = "false" if str(current).lower() == "true" else "true"
            ok = await _set_setting(field, new_val)
            if ok:
                await toast_ok(callback, f"🔄 {meta['label']} → {new_val}")
            else:
                await toast_err(callback, "❌ Erro ao salvar.")
            await on_config(callback)
            return

        # Texto → pedir input
        hint = ""
        if meta["type"] == "money":
            hint = "Ex.: <code>19.90</code>"
        elif meta["type"] == "int":
            hint = "Ex.: <code>30</code>"
        else:
            hint = "Ex.: <code>5</code>"

        prompt = (
            f"✏️ <b>EDITAR {meta['label'].upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"paycfg:{field}",
            screen=f"payments:config:{field}",
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"payments on_config_set falhou: {exc}")


async def _apply_config_field(
    message: Message,
    admin_id: int,
    field: str,
    value: str,
) -> None:
    meta = CONFIG_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    # Validação por tipo
    if meta["type"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        val = f"{num:.2f}"
    elif meta["type"] == "int":
        try:
            n = int(val)
            if n < 0:
                raise ValueError
            val = str(n)
        except Exception:
            await message.answer("❌ Número inválido.")
            return
    elif meta["type"] == "number":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        val = f"{num:g}"

    ok = await _set_setting(field, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"payment.config.set.{field}",
        category="admin",
        details={"field": field, "value": val},
    )

    await message.answer(
        text=f"✅ <b>{meta['label']}</b> atualizado para <code>{val}</code>.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="⚙️ Ver Config", callback_data=CB_CONFIG)]]
        ),
    )


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
    "on_payments_text_input",
    "on_view",
    "on_view_qr",
    "on_approve",
    "on_approve_confirm",
    "on_refuse",
    "on_refuse_confirm",
    "on_reprocess",
    "on_refund",
    "on_refund_confirm",
    "on_recon",
    "on_config",
    "on_config_set",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_QR",
    "CB_APPROVE",
    "CB_APPROVE_YES",
    "CB_REFUSE",
    "CB_REFUSE_YES",
    "CB_REPROCESS",
    "CB_REFUND",
    "CB_REFUND_YES",
    "CB_RECON",
    "CB_CONFIG",
    "CB_CONFIG_SET",
    "CB_EXPORT",
    "FILTERS",
    "CONFIG_FIELDS",
]
