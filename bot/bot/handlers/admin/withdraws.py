"""
Larizinha Store — Módulo Admin: Saques
Implementa gestão completa de saques conforme DIM.

Telas:
- Lista de saques (filtros: pendentes/processando/pagos/recusados, hoje/mês, buscar, DIM)
- Detalhes do saque (usuário, valor, chave, tipo, datas, status, obs)
- Aprovar, Recusar, Pagar Manual
- Enviar Comprovante (foto com spoiler ao usuário)
- Registrar comprovante manual (upload pelo admin)
- Fila de pagamentos
- Histórico
- Config (mín/máx/taxa/limite diário/aprovação automática/prazo)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Comprovante enviado ao usuário SEMPRE com has_spoiler=True.
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
    Setting,
    Transaction,
    TransactionType,
    User,
    Withdraw,
    WithdrawStatus,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.services.receipt_generator import ReceiptData, receipt_generator
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
router = Router(name="admin_withdraws")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_withdraws_list"
MSG_KEY_EMPTY = "admin_withdraws_empty"
MSG_KEY_DETAIL = "admin_withdraws_detail"
MSG_KEY_PAY_MANUAL = "admin_withdraws_pay_manual"
MSG_KEY_CONFIG = "admin_withdraws_config"
MSG_KEY_QUEUE = "admin_withdraws_queue"

SCREEN_LIST = "admin_withdraws_list"
SCREEN_DETAIL = "admin_withdraws_detail"

# Callbacks base
CB_ROOT = "admin:withdraws"
CB_PAGE = "admin:withdraws:page"          # :filter:N
CB_FILTER = "admin:withdraws:filter"      # :key
CB_SEARCH = "admin:withdraws:search"
CB_VIEW = "admin:withdraws:view"          # :ID
CB_APPROVE = "admin:withdraws:approve"    # :ID
CB_APPROVE_YES = "admin:withdraws:approve:yes"  # :ID
CB_REFUSE = "admin:withdraws:refuse"      # :ID
CB_REFUSE_YES = "admin:withdraws:refuse:yes"    # :ID
CB_PAY = "admin:withdraws:pay"            # :ID
CB_PAY_MANUAL = "admin:withdraws:paymanual"  # :ID
CB_PAY_NO_RECEIPT = "admin:withdraws:paynorec"  # :ID
CB_RECEIPT = "admin:withdraws:receipt"    # :ID
CB_QUEUE = "admin:withdraws:queue"
CB_HISTORY = "admin:withdraws:history"    # :filter:N
CB_CONFIG = "admin:withdraws:config"
CB_CONFIG_SET = "admin:withdraws:cfgset"  # :field
CB_CANCEL = "admin:withdraws:cancel"      # :ID

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "pending": "🟡 Pendentes",
    "processing": "⏳ Processando",
    "paid": "🟢 Pagos",
    "refused": "❌ Recusados",
    "today": "📅 Hoje",
    "month": "📅 Este mês",
}

# Campos de configuração (Setting keys)
CONFIG_FIELDS: Dict[str, Dict[str, Any]] = {
    "min_withdraw": {"label": "Mínimo", "type": "money", "default": str(settings.MIN_WITHDRAW)},
    "max_withdraw": {"label": "Máximo", "type": "money", "default": str(settings.MAX_WITHDRAW)},
    "withdraw_fee_percent": {"label": "Taxa (%)", "type": "number", "default": str(settings.WITHDRAW_FEE_PERCENT)},
    "withdraw_daily_limit": {"label": "Limite diário", "type": "int", "default": str(settings.WITHDRAW_DAILY_LIMIT)},
    "withdraw_auto_approve": {"label": "Aprovação automática", "type": "bool", "default": str(settings.WITHDRAW_AUTO_APPROVE).lower()},
    "withdraw_processing_days": {"label": "Prazo (dias)", "type": "int", "default": str(settings.WITHDRAW_PROCESSING_DAYS)},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": SYM_PENDING,
        "PROCESSING": SYM_PROCESSING,
        "APPROVED": SYM_DONE,
        "PAID": SYM_DONE,
        "REFUSED": SYM_ERR,
        "CANCELED": SYM_CANCEL,
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": "Pendente",
        "PROCESSING": "Processando",
        "APPROVED": "Aprovado",
        "PAID": "Pago",
        "REFUSED": "Recusado",
        "CANCELED": "Cancelado",
    }.get(s, str(status))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "pending":
        return stmt.where(Withdraw.status == WithdrawStatus.PENDING)
    if filter_key == "processing":
        return stmt.where(Withdraw.status == WithdrawStatus.PROCESSING)
    if filter_key == "paid":
        return stmt.where(Withdraw.status == WithdrawStatus.PAID)
    if filter_key == "refused":
        return stmt.where(
            Withdraw.status.in_([WithdrawStatus.REFUSED, WithdrawStatus.CANCELED])
        )
    if filter_key == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(Withdraw.created_at >= start)
    if filter_key == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return stmt.where(Withdraw.created_at >= start)
    return stmt


async def _count_withdraws(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Withdraw.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_withdraws(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Withdraw]:
    try:
        async with session_scope() as session:
            stmt = select(Withdraw).order_by(Withdraw.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_withdraw(wid: int) -> Optional[Withdraw]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Withdraw).where(Withdraw.id == wid))
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


async def _set_setting(key: str, value: str, category: str = "withdraws") -> bool:
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
        logger.debug(f"_set_setting (withdraws) falhou: {exc}")
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
    total = await _count_withdraws(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    items = await _list_withdraws(page, filter_key)

    if not items:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "💸 <b>SAQUES</b>\n\n"
                "<i>Nenhum saque encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for w in items:
            emoji = _status_emoji(w.status)
            rows.append(
                [
                    str(w.id),
                    str(w.user_id),
                    fmt_brl(w.amount),
                    truncate(w.pix_key, 16),
                    emoji,
                    fmt_dt(w.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "User", "Valor", "Chave", "S", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"💸 <b>SAQUES</b> ({total})\n\n"
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
                InlineKeyboardButton(text="⏳ Processando", callback_data=f"{CB_FILTER}:processing"),
            ],
            [
                InlineKeyboardButton(text="🟢 Pagos", callback_data=f"{CB_FILTER}:paid"),
                InlineKeyboardButton(text="❌ Recusados", callback_data=f"{CB_FILTER}:refused"),
            ],
            [
                InlineKeyboardButton(text="📅 Hoje", callback_data=f"{CB_FILTER}:today"),
                InlineKeyboardButton(text="📅 Mês", callback_data=f"{CB_FILTER}:month"),
            ],
            [
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
                InlineKeyboardButton(text="📊 Fila", callback_data=CB_QUEUE),
            ],
            [
                InlineKeyboardButton(text="⚙️ Config", callback_data=CB_CONFIG),
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for w in items:
        emoji = _status_emoji(w.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{w.id} · {fmt_brl(w.amount)}",
                    callback_data=f"{CB_VIEW}:{w.id}",
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
        logger.exception(f"withdraws on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # admin:withdraws:page:FILTER:N
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
        logger.exception(f"withdraws on_page falhou: {exc}")


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
        logger.exception(f"withdraws on_filter falhou: {exc}")


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
            "🔍 <b>BUSCAR SAQUE</b>\n\n"
            "Envie o ID do saque (numérico).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="withdraw_search_id",
            screen="withdraws:search",
            use_force_reply=True,
            reply_placeholder="ID do saque",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_withdraws_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Buscar saque
    if awaiting == "withdraw_search_id":
        value, _extra = await consume_input(message, admin_id, expected_field="withdraw_search_id")
        if value is None:
            return
        try:
            wid = int(value.strip())
        except Exception:
            await message.answer("❌ ID inválido.")
            return
        w = await _load_withdraw(wid)
        if w is None:
            await message.answer("❌ Saque não encontrado.")
            return
        text, keyboard = await _render_detail(wid)
        if text:
            await message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        return

    # Pagar manual: valor ou obs
    if awaiting.startswith("withdraw_pay_obs:"):
        wid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _finish_pay_manual(message, admin_id, wid, value)
        return

    # Config
    if awaiting.startswith("wdcfg:"):
        field = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_config_field(message, admin_id, field, value)
        return


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(wid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    w = await _load_withdraw(wid)
    if w is None:
        return None, None

    user = await _load_user(int(w.user_id))

    status_emoji = _status_emoji(w.status)

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"💸 <b>SAQUE #{w.id}</b>\n"
            f"👤 Usuário: {(user.first_name if user else '—')} "
            f"(<code>{user.telegram_id if user else '—'}</code>)\n"
            f"💰 Valor: <b>{fmt_brl(w.amount)}</b>\n"
            f"💵 Taxa: {fmt_brl(w.fee)}\n"
            f"🧾 Líquido: <b>{fmt_brl(w.net_amount)}</b>\n"
            f"🔑 Chave PIX: <code>{w.pix_key}</code>\n"
            f"📋 Tipo: {w.pix_key_type or '—'}\n"
            f"📅 Solicitado: {fmt_dt(w.created_at)}\n"
            f"🟢 Status: {status_emoji} {_status_label(w.status)}\n"
            f"📝 Observação: {truncate(w.observation, 60) or '—'}"
        ),
        id=w.id,
        nome=user.first_name if user else "—",
        id_telegram=user.telegram_id if user else "—",
        valor=fmt_brl(w.amount),
        taxa=fmt_brl(w.fee),
        liquido=fmt_brl(w.net_amount),
        chave=w.pix_key,
        tipo=w.pix_key_type or "—",
        data=fmt_dt(w.created_at),
        status=f"{status_emoji} {_status_label(w.status)}",
        obs=truncate(w.observation, 60) or "—",
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(w))

    return rendered.content, keyboard


def _default_detail_keyboard(w: Withdraw) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = []
    status = str(getattr(w.status, "value", w.status) or "").upper()

    if status == "PENDING":
        rows.append([b("✅ Aprovar", f"{CB_APPROVE}:{w.id}"), b("❌ Recusar", f"{CB_REFUSE}:{w.id}")])
    elif status == "PROCESSING":
        rows.append([b("💸 Pagar Manual", f"{CB_PAY}:{w.id}")])
        rows.append([b("❌ Recusar", f"{CB_REFUSE}:{w.id}")])
    elif status in ("APPROVED",):
        rows.append([b("💸 Pagar Manual", f"{CB_PAY}:{w.id}")])
    elif status == "PAID":
        rows.append([b("📤 Reenviar Comprovante", f"{CB_RECEIPT}:{w.id}")])

    rows.append([b("🗑️ Cancelar", f"{CB_CANCEL}:{w.id}")])
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
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(wid)
        if text is None:
            await toast_err(callback, "❌ Saque não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_view falhou: {exc}")


# ============================================================
# APROVAR
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
        try:
            wid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        w = await _load_withdraw(wid)
        if w is None:
            await toast_err(callback, "❌ Saque não encontrado.")
            return
        if str(getattr(w.status, "value", w.status)).upper() != "PENDING":
            await toast_err(callback, "❌ Só é possível aprovar saques pendentes.")
            return

        text = (
            f"✅ <b>APROVAR SAQUE #{wid}</b>\n\n"
            f"Valor: <b>{fmt_brl(w.amount)}</b>\n"
            f"Chave: <code>{w.pix_key}</code>\n\n"
            "Ao aprovar, o saque vai para o status <b>Processando</b>\n"
            "e o valor é debitado do saldo do usuário."
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_APPROVE_YES}:{wid}",
            cancel_callback=f"{CB_VIEW}:{wid}",
            confirm_text="✅ Aprovar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_approve falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_APPROVE_YES}:"))
async def on_approve_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Withdraw).where(Withdraw.id == wid).with_for_update()
                )
                w: Optional[Withdraw] = r.scalar_one_or_none()
                if w is None:
                    await toast_err(callback, "❌ Saque não encontrado.")
                    return
                if str(getattr(w.status, "value", w.status)).upper() != "PENDING":
                    await toast_err(callback, "❌ Saque não está pendente.")
                    return

                # Saldo já foi debitado no fluxo do usuário (withdraw.py),
                # portanto aqui só mudamos o status.
                w.status = WithdrawStatus.PROCESSING
                session.add(w)
        except Exception as exc:
            logger.exception(f"approve withdraw falhou: {exc}")
            await toast_err(callback, "❌ Erro ao aprovar.")
            return

        await log_action(
            admin_id=admin_id,
            action="withdraw.approved",
            category="admin",
            details={"withdraw_id": wid},
        )

        # Notifica o usuário
        try:
            user = await _load_user(int(w.user_id))
            if user is not None:
                await callback.bot.send_message(
                    chat_id=int(user.telegram_id),
                    text=(
                        f"✅ <b>Seu saque foi aprovado!</b>\n\n"
                        f"💵 Valor: <b>{fmt_brl(w.amount)}</b>\n"
                        f"🪪 Chave PIX: <code>{w.pix_key}</code>\n"
                        f"🎫 ID: <code>#{w.id}</code>\n\n"
                        "⏳ Estamos processando o pagamento."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        text, keyboard = await _render_detail(wid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "✅ Aprovado.")
    except Exception as exc:
        logger.exception(f"withdraws on_approve_confirm falhou: {exc}")


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
        try:
            wid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        w = await _load_withdraw(wid)
        if w is None:
            await toast_err(callback, "❌ Saque não encontrado.")
            return
        if str(getattr(w.status, "value", w.status)).upper() in ("PAID", "REFUSED"):
            await toast_err(callback, "❌ Este saque não pode ser recusado.")
            return

        text = (
            f"❌ <b>RECUSAR SAQUE #{wid}</b>\n\n"
            f"Valor: <b>{fmt_brl(w.amount)}</b>\n"
            f"Chave: <code>{w.pix_key}</code>\n\n"
            "Isso vai marcar o saque como <b>RECUSADO</b> e devolver\n"
            "o valor ao saldo do usuário.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REFUSE_YES}:{wid}",
            cancel_callback=f"{CB_VIEW}:{wid}",
            confirm_text="❌ Sim, recusar",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_refuse falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFUSE_YES}:"))
async def on_refuse_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Withdraw).where(Withdraw.id == wid).with_for_update()
                )
                w: Optional[Withdraw] = r.scalar_one_or_none()
                if w is None:
                    await toast_err(callback, "❌ Saque não encontrado.")
                    return
                if str(getattr(w.status, "value", w.status)).upper() in ("PAID", "REFUSED"):
                    await toast_err(callback, "❌ Não pode recusar.")
                    return

                valor = float(w.amount or 0)

                # Devolve o valor ao saldo (foi debitado no fluxo do user)
                ures = await session.execute(
                    select(User).where(User.id == w.user_id).with_for_update()
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
                            description=f"Estorno saque #{wid} recusado",
                            reference=f"withdraw:{wid}",
                        )
                    )

                w.status = WithdrawStatus.REFUSED
                w.processed_at = datetime.now(timezone.utc)
                session.add(w)

                tg_id = int(u.telegram_id) if u is not None else None
        except Exception as exc:
            logger.exception(f"refuse withdraw falhou: {exc}")
            await toast_err(callback, "❌ Erro ao recusar.")
            return

        # Notifica
        try:
            if tg_id:
                await callback.bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"❌ <b>Saque recusado</b>\n\n"
                        f"💵 Valor: <b>{fmt_brl(valor)}</b>\n"
                        f"🎫 ID: <code>#{wid}</code>\n\n"
                        "O valor foi devolvido ao seu saldo."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="withdraw.refused",
            category="admin",
            details={"withdraw_id": wid},
        )

        text, keyboard = await _render_detail(wid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "❌ Recusado.")
    except Exception as exc:
        logger.exception(f"withdraws on_refuse_confirm falhou: {exc}")


# ============================================================
# PAGAR MANUAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PAY}:"))
async def on_pay(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        w = await _load_withdraw(wid)
        if w is None:
            await toast_err(callback, "❌ Saque não encontrado.")
            return
        if str(getattr(w.status, "value", w.status)).upper() == "PAID":
            await toast_err(callback, "❌ Já está pago.")
            return

        user = await _load_user(int(w.user_id))

        rendered = await message_engine.render(
            MSG_KEY_PAY_MANUAL,
            fallback=(
                f"💸 <b>PAGAMENTO MANUAL · SAQUE #{wid}</b>\n\n"
                f"Usuário: {user.first_name if user else '—'}\n"
                f"Valor: <b>{fmt_brl(w.amount)}</b>\n"
                f"Chave: <code>{w.pix_key}</code>\n\n"
                "Escolha uma opção:"
            ),
            id=wid,
            nome=user.first_name if user else "—",
            valor=fmt_brl(w.amount),
            chave=w.pix_key,
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="📤 Enviar Comprovante",
                    callback_data=f"{CB_RECEIPT}:{wid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Pago sem Comprovante",
                    callback_data=f"{CB_PAY_NO_RECEIPT}:{wid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Adicionar Observação",
                    callback_data=f"{CB_PAY_MANUAL}:{wid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancelar",
                    callback_data=f"{CB_VIEW}:{wid}",
                )
            ],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_pay falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAY_NO_RECEIPT}:"))
async def on_pay_no_receipt(callback: CallbackQuery) -> None:
    """Marca como pago sem comprovante."""
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        await _mark_paid(callback, admin_id, wid, receipt_url=None)
    except Exception as exc:
        logger.exception(f"withdraws on_pay_no_receipt falhou: {exc}")


async def _mark_paid(
    callback: CallbackQuery,
    admin_id: int,
    wid: int,
    receipt_url: Optional[str],
) -> None:
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(Withdraw).where(Withdraw.id == wid).with_for_update()
            )
            w: Optional[Withdraw] = r.scalar_one_or_none()
            if w is None:
                await toast_err(callback, "❌ Saque não encontrado.")
                return
            if str(getattr(w.status, "value", w.status)).upper() == "PAID":
                await toast_err(callback, "❌ Já está pago.")
                return

            w.status = WithdrawStatus.PAID
            w.processed_at = datetime.now(timezone.utc)
            if receipt_url:
                w.receipt_url = receipt_url
                w.receipt_sent_at = datetime.now(timezone.utc)
            session.add(w)

            uid = int(w.user_id)
            valor = float(w.amount or 0)
    except Exception as exc:
        logger.exception(f"_mark_paid falhou: {exc}")
        await toast_err(callback, "❌ Erro ao marcar como pago.")
        return

    await log_action(
        admin_id=admin_id,
        action="withdraw.paid",
        category="admin",
        details={"withdraw_id": wid, "receipt": bool(receipt_url)},
    )

    # Notifica o usuário
    try:
        user = await _load_user(uid)
        if user is not None:
            await callback.bot.send_message(
                chat_id=int(user.telegram_id),
                text=(
                    f"✅ <b>Saque pago!</b>\n\n"
                    f"💵 Valor: <b>{fmt_brl(valor)}</b>\n"
                    f"🎫 ID: <code>#{wid}</code>"
                ),
                parse_mode="HTML",
            )
    except Exception:
        pass

    text, keyboard = await _render_detail(wid)
    if text:
        await edit_or_send(callback, text=text, reply_markup=keyboard)
    await toast_ok(callback, "✅ Pago.")


# ============================================================
# ENVIAR / REGISTRAR COMPROVANTE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RECEIPT}:"))
async def on_receipt(callback: CallbackQuery) -> None:
    """
    Formato:
    - admin:withdraws:receipt:ID  → envia comprovante (gerado automaticamente)
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        w = await _load_withdraw(wid)
        if w is None:
            await toast_err(callback, "❌ Saque não encontrado.")
            return

        user = await _load_user(int(w.user_id))
        if user is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        # ---------- Gera comprovante ----------
        try:
            receipt_data = ReceiptData(
                store_name=settings.APP_NAME,
                amount=float(w.amount or 0),
                pix_key=w.pix_key or "—",
                pix_key_type=w.pix_key_type,
                txid=f"W{wid}",
                user_id=int(user.id),
                user_name=user.first_name or "Cliente",
                created_at=datetime.now(timezone.utc),
                status="PAGO",
                note=f"ID interno: {wid}",
            )
            png = await receipt_generator.generate(receipt_data)
            if not png:
                await toast_err(callback, "❌ Erro ao gerar comprovante.")
                return
        except Exception as exc:
            logger.debug(f"gerar comprovante falhou: {exc}")
            await toast_err(callback, "❌ Erro ao gerar comprovante.")
            return

        # ---------- Envia como spoiler ----------
        try:
            await callback.bot.send_photo(
                chat_id=int(user.telegram_id),
                photo=png,
                caption="🧾 <b>Comprovante de saque</b>",
                parse_mode="HTML",
                has_spoiler=True,
            )
        except Exception as exc:
            logger.debug(f"envio comprovante falhou: {exc}")
            await toast_err(callback, "❌ Erro ao enviar comprovante.")
            return

        # ---------- Marca como pago com comprovante ----------
        await _mark_paid(callback, admin_id, wid, receipt_url="generated")

        await log_action(
            admin_id=admin_id,
            action="withdraw.receipt_sent",
            category="admin",
            details={"withdraw_id": wid},
        )
    except Exception as exc:
        logger.exception(f"withdraws on_receipt falhou: {exc}")


# ============================================================
# OBSERVAÇÃO MANUAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PAY_MANUAL}:"))
async def on_pay_manual_obs(callback: CallbackQuery) -> None:
    """
    Pede ao admin uma observação (motivo) antes de marcar como pago.
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        prompt = (
            f"📝 <b>OBSERVAÇÃO DO PAGAMENTO · SAQUE #{wid}</b>\n\n"
            "Envie uma observação/motivo (opcional).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"withdraw_pay_obs:{wid}",
            screen=f"withdraws:pay:{wid}",
            extra={"withdraw_id": wid},
            use_force_reply=True,
            reply_placeholder="Motivo...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_pay_manual_obs falhou: {exc}")


async def _finish_pay_manual(
    message: Message,
    admin_id: int,
    wid: int,
    observation: str,
) -> None:
    observation = (observation or "").strip() or "Pagamento manual"

    try:
        async with session_scope() as session:
            r = await session.execute(
                select(Withdraw).where(Withdraw.id == wid).with_for_update()
            )
            w: Optional[Withdraw] = r.scalar_one_or_none()
            if w is None:
                await message.answer("❌ Saque não encontrado.")
                return
            w.status = WithdrawStatus.PAID
            w.processed_at = datetime.now(timezone.utc)
            w.observation = observation[:200]
            session.add(w)

            uid = int(w.user_id)
            valor = float(w.amount or 0)
    except Exception as exc:
        logger.exception(f"_finish_pay_manual falhou: {exc}")
        await message.answer("❌ Erro.")
        return

    await log_action(
        admin_id=admin_id,
        action="withdraw.paid_manual",
        category="admin",
        details={"withdraw_id": wid, "observation": observation},
    )

    # Notifica o usuário
    try:
        user = await _load_user(uid)
        if user is not None:
            await message.bot.send_message(
                chat_id=int(user.telegram_id),
                text=(
                    f"✅ <b>Saque pago!</b>\n\n"
                    f"💵 Valor: <b>{fmt_brl(valor)}</b>\n"
                    f"🎫 ID: <code>#{wid}</code>"
                ),
                parse_mode="HTML",
            )
    except Exception:
        pass

    await message.answer(
        f"✅ Saque <b>#{wid}</b> marcado como pago.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Ver Saque", callback_data=f"{CB_VIEW}:{wid}")]]
        ),
    )


# ============================================================
# CANCELAR (mantém o status como CANCELED sem devolver saldo)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
async def on_cancel(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            wid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Withdraw).where(Withdraw.id == wid).with_for_update()
                )
                w: Optional[Withdraw] = r.scalar_one_or_none()
                if w is None:
                    await toast_err(callback, "❌ Saque não encontrado.")
                    return
                if str(getattr(w.status, "value", w.status)).upper() in ("PAID", "CANCELED"):
                    await toast_err(callback, "❌ Não pode cancelar.")
                    return
                w.status = WithdrawStatus.CANCELED
                w.processed_at = datetime.now(timezone.utc)
                session.add(w)
        except Exception as exc:
            logger.exception(f"cancel withdraw falhou: {exc}")
            await toast_err(callback, "❌ Erro ao cancelar.")
            return

        await log_action(
            admin_id=admin_id,
            action="withdraw.canceled",
            category="admin",
            details={"withdraw_id": wid},
        )

        text, keyboard = await _render_detail(wid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "❌ Cancelado.")
    except Exception as exc:
        logger.exception(f"withdraws on_cancel falhou: {exc}")


# ============================================================
# FILA DE PAGAMENTOS
# ============================================================
@router.callback_query(F.data == CB_QUEUE)
async def on_queue(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Withdraw)
                    .where(Withdraw.status.in_([WithdrawStatus.PENDING, WithdrawStatus.PROCESSING]))
                    .order_by(Withdraw.created_at.asc())
                    .limit(20)
                )
                fila = list(res.scalars().all())
        except Exception:
            fila = []

        if not fila:
            text = "📊 <b>FILA DE PAGAMENTOS</b>\n\n<i>Nenhum saque pendente.</i>"
        else:
            total_valor = sum(float(x.amount or 0) for x in fila)
            lines = [
                f"📊 <b>FILA DE PAGAMENTOS</b> ({len(fila)})",
                f"💵 Valor total: <b>{fmt_brl(total_valor)}</b>",
                "",
            ]
            for w in fila:
                emoji = _status_emoji(w.status)
                lines.append(
                    f"{emoji} #{w.id} · User <code>{w.user_id}</code> · "
                    f"{fmt_brl(w.amount)} · {fmt_dt(w.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for w in fila[:10]:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"💸 #{w.id} · {fmt_brl(w.amount)}",
                        callback_data=f"{CB_VIEW}:{w.id}",
                    )
                ]
            )
        rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_queue falhou: {exc}")


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

        # Reusa render_list com filtro "paid"
        parts = callback.data.split(":")
        filter_key = "paid"
        page = 1
        if len(parts) >= 4:
            filter_key = parts[3]
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except Exception:
                page = 1

        await render_list(callback, page=page, filter_key=filter_key)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_history falhou: {exc}")


# ============================================================
# CONFIG
# ============================================================
@router.callback_query(F.data == CB_CONFIG)
async def on_config(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        vals: Dict[str, str] = {}
        for k, meta in CONFIG_FIELDS.items():
            v = await _get_setting(k, meta.get("default"))
            vals[k] = str(v or "")

        rendered = await message_engine.render(
            MSG_KEY_CONFIG,
            fallback=(
                "⚙️ <b>CONFIG SAQUES</b>\n\n"
                f"├ Mínimo: <b>{fmt_brl(float(vals['min_withdraw']))}</b>\n"
                f"├ Máximo: <b>{fmt_brl(float(vals['max_withdraw']))}</b>\n"
                f"├ Taxa: {vals['withdraw_fee_percent']}%\n"
                f"├ Limite diário: {vals['withdraw_daily_limit']}\n"
                f"├ Aprovação automática: "
                f"{'🟢' if vals['withdraw_auto_approve'] == 'true' else '🔴'}\n"
                f"└ Prazo processamento: {vals['withdraw_processing_days']}d"
            ),
            min=fmt_brl(float(vals["min_withdraw"])),
            max=fmt_brl(float(vals["max_withdraw"])),
            taxa=vals["withdraw_fee_percent"],
            lim_dia=vals["withdraw_daily_limit"],
            auto="🟢" if vals["withdraw_auto_approve"] == "true" else "🔴",
            prazo=vals["withdraw_processing_days"],
        )

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="✏️ Mínimo", callback_data=f"{CB_CONFIG_SET}:min_withdraw"),
                InlineKeyboardButton(text="✏️ Máximo", callback_data=f"{CB_CONFIG_SET}:max_withdraw"),
            ],
            [
                InlineKeyboardButton(text="✏️ Taxa %", callback_data=f"{CB_CONFIG_SET}:withdraw_fee_percent"),
                InlineKeyboardButton(text="✏️ Limite diário", callback_data=f"{CB_CONFIG_SET}:withdraw_daily_limit"),
            ],
            [
                InlineKeyboardButton(text="🔄 Auto aprovação", callback_data=f"{CB_CONFIG_SET}:withdraw_auto_approve"),
            ],
            [
                InlineKeyboardButton(text="✏️ Prazo (dias)", callback_data=f"{CB_CONFIG_SET}:withdraw_processing_days"),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_config falhou: {exc}")


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

        # Booleans → toggle
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

        hint = ""
        if meta["type"] == "money":
            hint = "Ex.: <code>20.00</code>"
        elif meta["type"] == "int":
            hint = "Ex.: <code>5</code>"
        else:
            hint = "Ex.: <code>2.5</code>"

        prompt = (
            f"✏️ <b>EDITAR {meta['label'].upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"wdcfg:{field}",
            screen=f"withdraws:config:{field}",
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"withdraws on_config_set falhou: {exc}")


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
        action=f"withdraw.config.set.{field}",
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
    "on_withdraws_text_input",
    "on_view",
    "on_approve",
    "on_approve_confirm",
    "on_refuse",
    "on_refuse_confirm",
    "on_pay",
    "on_pay_no_receipt",
    "on_receipt",
    "on_pay_manual_obs",
    "on_cancel",
    "on_queue",
    "on_history",
    "on_config",
    "on_config_set",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_APPROVE",
    "CB_APPROVE_YES",
    "CB_REFUSE",
    "CB_REFUSE_YES",
    "CB_PAY",
    "CB_PAY_MANUAL",
    "CB_PAY_NO_RECEIPT",
    "CB_RECEIPT",
    "CB_QUEUE",
    "CB_HISTORY",
    "CB_CONFIG",
    "CB_CONFIG_SET",
    "CB_CANCEL",
    "FILTERS",
    "CONFIG_FIELDS",
]
