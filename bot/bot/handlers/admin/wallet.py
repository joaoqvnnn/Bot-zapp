"""
Larizinha Store — Módulo Admin: Carteira e Saldo
Implementa gestão financeira consolidada conforme DIM.

Telas:
- Painel de carteira (disponível/bloqueado/créditos/débitos/ajustes)
- Carteiras por usuário (top saldos + busca)
- Créditos/Débitos do dia (transações de hoje)
- Add Saldo / Remover Saldo (a partir de uma busca de usuário)
- Ajustar Saldo (com motivo obrigatório + auditoria)
- Estornar transação específica (cria transação reversa)
- Extrato por usuário (paginação)
- Conciliação (soma total do sistema x saldo dos usuários)
- Histórico de ajustes manuais

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
router = Router(name="admin_wallet")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_OVERVIEW = "admin_wallet_overview"
MSG_KEY_WALLETS = "admin_wallet_wallets"
MSG_KEY_TX = "admin_wallet_tx"
MSG_KEY_EXTRACT = "admin_wallet_extract"
MSG_KEY_HIST_ADJ = "admin_wallet_hist_adjust"
MSG_KEY_RECON = "admin_wallet_reconciliation"

SCREEN_OVERVIEW = "admin_wallet_overview"
SCREEN_WALLETS = "admin_wallet_wallets"

# Callbacks base
CB_ROOT = "admin:wallet"
CB_OVERVIEW = "admin:wallet:overview"
CB_WALLETS = "admin:wallet:wallets"           # :page:N
CB_TX = "admin:wallet:tx"                     # :kind:page:N
CB_ADD_BAL = "admin:wallet:add"
CB_REM_BAL = "admin:wallet:rem"
CB_ADJUST = "admin:wallet:adjust"
CB_REFUND = "admin:wallet:refund"
CB_REFUND_YES = "admin:wallet:refund:yes"     # :TXID
CB_EXTRACT = "admin:wallet:extract"           # :UID:page:N
CB_RECON = "admin:wallet:recon"
CB_HIST_ADJ = "admin:wallet:histadj"          # :page:N
CB_SEARCH_USER = "admin:wallet:searchuser"

# Categorias de crédito / débito
CREDIT_TYPES = (
    TransactionType.DEPOSIT,
    TransactionType.REFUND,
    TransactionType.COMMISSION,
    TransactionType.GIFT,
    TransactionType.ADMIN_CREDIT,
    TransactionType.BONUS,
)
DEBIT_TYPES = (
    TransactionType.WITHDRAW,
    TransactionType.PURCHASE,
    TransactionType.ADMIN_DEBIT,
)


# ============================================================
# 🧰 HELPERS
# ============================================================
def _sign(t: Transaction) -> str:
    kind = str(getattr(t.type, "value", t.type) or "").upper()
    return "+" if kind in {x.value for x in CREDIT_TYPES} else "-"


def _kind_emoji(t: Transaction) -> str:
    kind = str(getattr(t.type, "value", t.type) or "").upper()
    return {
        "DEPOSIT": "💠",
        "WITHDRAW": "💸",
        "PURCHASE": "🛒",
        "REFUND": "♻️",
        "COMMISSION": "👥",
        "GIFT": "🎁",
        "ADMIN_CREDIT": "➕",
        "ADMIN_DEBIT": "➖",
        "BONUS": "🎉",
    }.get(kind, "🔹")


async def _load_user(uid: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == uid))
            return res.scalar_one_or_none()
    except Exception:
        return None


# ============================================================
# PAINEL PRINCIPAL
# ============================================================
async def _collect_wallet_stats() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    out: Dict[str, Any] = {
        "disp_total": 0.0,
        "bloq_total": 0.0,
        "cred_hoje": 0.0,
        "deb_hoje": 0.0,
        "ajustes_hoje": 0,
        "cred_hoje_count": 0,
        "deb_hoje_count": 0,
    }

    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.coalesce(func.sum(User.balance), 0))
            )
            out["disp_total"] = float(res.scalar() or 0)

            res = await session.execute(
                select(func.coalesce(func.sum(User.balance_locked), 0))
            )
            out["bloq_total"] = float(res.scalar() or 0)

            # Créditos de hoje
            res = await session.execute(
                select(
                    func.count(Transaction.id),
                    func.coalesce(func.sum(Transaction.amount), 0),
                )
                .where(Transaction.created_at >= start_today)
                .where(Transaction.type.in_(CREDIT_TYPES))
            )
            row = res.first()
            if row:
                out["cred_hoje_count"] = int(row[0] or 0)
                out["cred_hoje"] = float(row[1] or 0)

            # Débitos de hoje
            res = await session.execute(
                select(
                    func.count(Transaction.id),
                    func.coalesce(func.sum(Transaction.amount), 0),
                )
                .where(Transaction.created_at >= start_today)
                .where(Transaction.type.in_(DEBIT_TYPES))
            )
            row = res.first()
            if row:
                out["deb_hoje_count"] = int(row[0] or 0)
                out["deb_hoje"] = float(row[1] or 0)

            # Ajustes manuais de hoje (ADMIN_CREDIT + ADMIN_DEBIT)
            res = await session.execute(
                select(func.count(Transaction.id))
                .where(Transaction.created_at >= start_today)
                .where(
                    Transaction.type.in_(
                        (TransactionType.ADMIN_CREDIT, TransactionType.ADMIN_DEBIT)
                    )
                )
            )
            out["ajustes_hoje"] = int(res.scalar() or 0)

    except Exception as exc:
        logger.debug(f"_collect_wallet_stats falhou: {exc}")

    return out


async def render_overview(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    stats = await _collect_wallet_stats()

    rendered = await message_engine.render(
        MSG_KEY_OVERVIEW,
        fallback=(
            "💰 <b>CARTEIRA E SALDO</b>\n\n"
            f"├ Saldo disponível total: <b>{fmt_brl(stats['disp_total'])}</b>\n"
            f"├ Saldo bloqueado total: <b>{fmt_brl(stats['bloq_total'])}</b>\n"
            f"├ Créditos hoje: <b>{fmt_brl(stats['cred_hoje'])}</b> "
            f"({stats['cred_hoje_count']} ops)\n"
            f"├ Débitos hoje: <b>{fmt_brl(stats['deb_hoje'])}</b> "
            f"({stats['deb_hoje_count']} ops)\n"
            f"└ Ajustes manuais hoje: <b>{stats['ajustes_hoje']}</b>"
        ),
        disp_total=fmt_brl(stats["disp_total"]),
        bloq_total=fmt_brl(stats["bloq_total"]),
        cred_hoje=fmt_brl(stats["cred_hoje"]),
        deb_hoje=fmt_brl(stats["deb_hoje"]),
        cred_hoje_count=fmt_num(stats["cred_hoje_count"]),
        deb_hoje_count=fmt_num(stats["deb_hoje_count"]),
        ajustes=fmt_num(stats["ajustes_hoje"]),
    )

    engine_kb = await button_engine.inline(SCREEN_OVERVIEW)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="💰 Carteiras",
                        callback_data=f"{CB_WALLETS}:page:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📜 Créditos",
                        callback_data=f"{CB_TX}:credit:page:1",
                    ),
                    InlineKeyboardButton(
                        text="📜 Débitos",
                        callback_data=f"{CB_TX}:debit:page:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="➕ Add Saldo",
                        callback_data=CB_ADD_BAL,
                    ),
                    InlineKeyboardButton(
                        text="➖ Remover Saldo",
                        callback_data=CB_REM_BAL,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📊 Extrato",
                        callback_data=f"{CB_EXTRACT}:0:page:1",
                    ),
                    InlineKeyboardButton(
                        text="🔍 Conciliação",
                        callback_data=CB_RECON,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📜 Histórico Ajustes",
                        callback_data=f"{CB_HIST_ADJ}:page:1",
                    ),
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu")],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data.in_({CB_ROOT, CB_OVERVIEW}))
async def on_overview(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_overview(callback)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_overview falhou: {exc}")


# ============================================================
# CARTEIRAS (por usuário)
# ============================================================
async def _list_wallets(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User)
                .where(User.balance > 0)
                .order_by(User.balance.desc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_wallets() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(User.id)).where(User.balance > 0)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


@router.callback_query(F.data.startswith(f"{CB_WALLETS}:"))
async def on_wallets(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        page = 1
        if len(parts) >= 4 and parts[3] == "page":
            try:
                page = int(parts[4])
            except Exception:
                page = 1

        total = await _count_wallets()
        total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
        page = max(1, min(page, total_p))

        wallets = await _list_wallets(page)

        if not wallets:
            text = (
                "💰 <b>CARTEIRAS</b>\n\n"
                "<i>Nenhum usuário com saldo positivo.</i>"
            )
            rows_kb: List[List[InlineKeyboardButton]] = []
        else:
            linhas = [f"💰 <b>CARTEIRAS · TOP {total}</b>", ""]
            for u in wallets:
                linhas.append(
                    f"#{u.id} · <b>{truncate(u.first_name or u.username or '—', 20)}</b> · "
                    f"{fmt_brl(u.balance)}"
                )
            text = "\n".join(linhas)

            rows_kb = []
            for u in wallets:
                rows_kb.append(
                    [
                        InlineKeyboardButton(
                            text=f"💰 #{u.id} · {fmt_brl(u.balance)} · "
                            f"{truncate(u.first_name or '—', 15)}",
                            callback_data=f"{CB_EXTRACT}:{u.id}:page:1",
                        )
                    ]
                )

        pag = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=f"{CB_WALLETS}:page",
            back_callback=CB_OVERVIEW,
        )
        if pag:
            rows_kb.append(pag)

        rows_kb.append(build_back_row(CB_OVERVIEW))

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_wallets falhou: {exc}")


# ============================================================
# CRÉDITOS / DÉBITOS
# ============================================================
async def _list_transactions(
    kind: str,
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Transaction]:
    types = CREDIT_TYPES if kind == "credit" else DEBIT_TYPES
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Transaction)
                .where(Transaction.type.in_(types))
                .order_by(Transaction.created_at.desc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_transactions(kind: str) -> int:
    types = CREDIT_TYPES if kind == "credit" else DEBIT_TYPES
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Transaction.id)).where(Transaction.type.in_(types))
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


@router.callback_query(F.data.startswith(f"{CB_TX}:"))
async def on_tx_list(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:wallet:tx:kind:page:N
        kind = "credit"
        page = 1
        if len(parts) >= 4:
            kind = parts[3]
        if len(parts) >= 6 and parts[4] == "page":
            try:
                page = int(parts[5])
            except Exception:
                page = 1

        if kind not in ("credit", "debit"):
            kind = "credit"

        total = await _count_transactions(kind)
        total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
        page = max(1, min(page, total_p))

        txs = await _list_transactions(kind, page)

        header = "📜 <b>CRÉDITOS</b>" if kind == "credit" else "📜 <b>DÉBITOS</b>"

        if not txs:
            text = f"{header}\n\n<i>Nenhuma transação encontrada.</i>"
        else:
            linhas = [f"{header} ({total})", ""]
            for t in txs:
                emoji = _kind_emoji(t)
                linhas.append(
                    f"{emoji} {fmt_brl(t.amount)} · User #{t.user_id} · "
                    f"<code>{getattr(t.type, 'value', t.type)}</code> · "
                    f"{fmt_dt(t.created_at, with_time=False)}"
                )
            text = "\n".join(linhas)

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="📜 Créditos",
                    callback_data=f"{CB_TX}:credit:page:1",
                ),
                InlineKeyboardButton(
                    text="📜 Débitos",
                    callback_data=f"{CB_TX}:debit:page:1",
                ),
            ],
        ]

        pag = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=f"{CB_TX}:{kind}:page",
            back_callback=CB_OVERVIEW,
        )
        if pag:
            rows_kb.append(pag)

        rows_kb.append(build_back_row(CB_OVERVIEW))

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_tx_list falhou: {exc}")


# ============================================================
# ADD / REMOVER SALDO / AJUSTAR
# ============================================================
@router.callback_query(F.data.in_({CB_ADD_BAL, CB_REM_BAL, CB_ADJUST}))
async def on_balance_start(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        if callback.data == CB_ADD_BAL:
            kind = "add"
            title = "ADICIONAR SALDO"
        elif callback.data == CB_REM_BAL:
            kind = "rem"
            title = "REMOVER SALDO"
        else:
            kind = "add"
            title = "AJUSTAR SALDO"

        prompt = (
            f"💰 <b>{title}</b>\n\n"
            "Primeiro, envie o <b>ID numérico</b> do usuário.\n"
            "Exemplo: <code>123</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"wallet_user_id:{kind}",
            screen=f"wallet:{kind}",
            extra={"kind": kind},
            use_force_reply=True,
            reply_placeholder="ID do usuário",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_balance_start falhou: {exc}")


# ============================================================
# BUSCAR USUÁRIO (fluxo inicial)
# ============================================================
@router.callback_query(F.data == CB_SEARCH_USER)
async def on_search_user(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "🔍 <b>BUSCAR USUÁRIO</b>\n\n"
            "Envie o ID numérico ou parte do nome/username.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="wallet_search_user",
            screen="wallet:search",
            use_force_reply=True,
            reply_placeholder="ID ou nome",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_search_user falhou: {exc}")


# ============================================================
# INPUT DE TEXTO (wallet)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_wallet_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # ---------- Buscar usuário ----------
    if awaiting == "wallet_search_user":
        value, _extra = await consume_input(message, admin_id, expected_field="wallet_search_user")
        if value is None:
            return
        await _do_search_wallet_user(message, admin_id, value)
        return

    # ---------- Wallet: recebe ID ----------
    if awaiting.startswith("wallet_user_id:"):
        kind = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _handle_wallet_user_id(message, admin_id, kind, value)
        return

    # ---------- Wallet: recebe valor ----------
    if awaiting.startswith("wallet_value:"):
        # wallet_value:add:UID  OR  wallet_value:rem:UID
        parts = awaiting.split(":")
        kind = parts[1]
        uid = int(parts[2])
        value, extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _confirm_wallet_value(message, admin_id, kind, uid, value)
        return

    # ---------- Wallet: recebe motivo ----------
    if awaiting.startswith("wallet_reason:"):
        parts = awaiting.split(":")
        kind = parts[1]
        uid = int(parts[2])
        value, extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_wallet_reason(message, admin_id, kind, uid, value, extra)
        return

    # ---------- Extrato: buscar user ----------
    if awaiting == "wallet_extract_user":
        value, _extra = await consume_input(message, admin_id, expected_field="wallet_extract_user")
        if value is None:
            return
        try:
            uid = int(value.strip())
        except Exception:
            await message.answer("❌ ID inválido.")
            return
        await _render_extract(message, admin_id, uid, page=1)
        return


# ============================================================
# HANDLERS — BUSCAR USUÁRIO
# ============================================================
async def _do_search_wallet_user(message: Message, admin_id: int, value: str) -> None:
    value = (value or "").strip()
    if not value:
        await message.answer("❌ Valor vazio.")
        return

    results: List[User] = []
    try:
        async with session_scope() as session:
            if value.isdigit():
                uid = int(value)
                res = await session.execute(
                    select(User)
                    .where(or_(User.id == uid, User.telegram_id == uid))
                    .limit(20)
                )
            else:
                like = f"%{value.lower()}%"
                res = await session.execute(
                    select(User)
                    .where(
                        or_(
                            func.lower(func.coalesce(User.first_name, "")).like(like),
                            func.lower(func.coalesce(User.username, "")).like(like),
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
            reply_markup=kb([[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]]),
        )
        return

    lines = [f"🔍 <b>RESULTADOS ({len(results)})</b>", ""]
    rows_kb: List[List[InlineKeyboardButton]] = []
    for u in results:
        lines.append(
            f"#{u.id} · <b>{truncate(u.first_name or '—', 20)}</b> · "
            f"{fmt_brl(u.balance)}"
        )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"💰 #{u.id} · {fmt_brl(u.balance)}",
                    callback_data=f"{CB_EXTRACT}:{u.id}:page:1",
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)])

    await message.answer(
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# RECEBER ID DO USUÁRIO (add/rem/adjust)
# ============================================================
async def _handle_wallet_user_id(
    message: Message,
    admin_id: int,
    kind: str,
    value: str,
) -> None:
    value = (value or "").strip()
    if not value.isdigit():
        await message.answer("❌ ID inválido.")
        return

    uid = int(value)
    u = await _load_user(uid)
    if u is None:
        await message.answer("❌ Usuário não encontrado.")
        return

    label = {"add": "ADICIONAR", "rem": "REMOVER"}.get(kind, "AJUSTAR")

    prompt = (
        f"💰 <b>{label} SALDO</b>\n\n"
        f"Usuário: <b>{u.first_name or '—'}</b> (#{u.id})\n"
        f"Saldo atual: <b>{fmt_brl(u.balance)}</b>\n\n"
        f"Digite o valor a {'adicionar' if kind == 'add' else 'remover'}: "
        f"Ex.: <code>10</code> ou <code>25.50</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await AdminFlow.set_awaiting(
        admin_id,
        field=f"wallet_value:{kind}:{uid}",
        screen=f"wallet:{kind}:{uid}",
        extra={"user_id": uid, "kind": kind},
    )
    await ask_input(
        message,
        prompt,
        field=f"wallet_value:{kind}:{uid}",
        screen=f"wallet:{kind}:{uid}",
        extra={"user_id": uid, "kind": kind},
        use_force_reply=True,
        reply_placeholder="Valor",
    )


# ============================================================
# CONFIRMAR VALOR → PEDIR MOTIVO
# ============================================================
async def _confirm_wallet_value(
    message: Message,
    admin_id: int,
    kind: str,
    uid: int,
    raw_value: str,
) -> None:
    valor = parse_number(raw_value)
    if valor is None or valor <= 0:
        await message.answer("❌ Valor inválido.")
        return

    u = await _load_user(uid)
    if u is None:
        await message.answer("❌ Usuário não encontrado.")
        return

    if kind == "rem" and float(u.balance or 0) < valor:
        await message.answer(
            f"❌ Saldo insuficiente para remover.\nSaldo: {fmt_brl(u.balance)}"
        )
        return

    # Guarda o valor no estado e pede motivo
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
        field=f"wallet_reason:{kind}:{uid}",
        screen=f"wallet:reason:{uid}",
        extra={"user_id": uid, "kind": kind, "value": valor},
        use_force_reply=True,
        reply_placeholder="Motivo...",
    )


# ============================================================
# APLICAR AJUSTE
# ============================================================
async def _apply_wallet_reason(
    message: Message,
    admin_id: int,
    kind: str,
    uid: int,
    reason: str,
    extra: Dict[str, Any],
) -> None:
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
                    description=f"Admin ajuste: {reason}",
                    reference=f"admin:{admin_id}",
                    metadata_={"reason": reason, "admin_id": admin_id},
                )
            )

            tg_id = int(u.telegram_id)
            new_balance_final = new_balance
    except Exception as exc:
        logger.exception(f"_apply_wallet_reason falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    # Notifica o usuário
    try:
        if kind == "add":
            body = (
                f"💰 <b>Saldo adicionado!</b>\n\n"
                f"💵 Valor: <b>{fmt_brl(pending_value)}</b>\n"
                f"💰 Novo saldo: <b>{fmt_brl(new_balance_final)}</b>"
            )
        else:
            body = (
                f"➖ <b>Saldo removido!</b>\n\n"
                f"💵 Valor: <b>{fmt_brl(pending_value)}</b>\n"
                f"💰 Novo saldo: <b>{fmt_brl(new_balance_final)}</b>"
            )
        await message.bot.send_message(chat_id=tg_id, text=body, parse_mode="HTML")
    except Exception:
        pass

    await log_action(
        admin_id=admin_id,
        action=f"wallet.{kind}",
        category="admin",
        user_id=uid,
        details={"value": pending_value, "reason": reason},
    )

    await AdminFlow.clear(admin_id)

    await message.answer(
        text=(
            f"✅ <b>Operação realizada com sucesso.</b>\n\n"
            f"Usuário: <code>#{uid}</code>\n"
            f"Ação: <b>{'Adicionado' if kind == 'add' else 'Removido'}</b> "
            f"{fmt_brl(pending_value)}\n"
            f"Motivo: {reason}"
        ),
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="💰 Ver extrato", callback_data=f"{CB_EXTRACT}:{uid}:page:1"
                    )
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)],
            ]
        ),
    )


# ============================================================
# EXTRATO
# ============================================================
async def _render_extract(
    target: Union[Message, CallbackQuery],
    admin_id: int,
    uid: int,
    *,
    page: int = 1,
) -> None:
    u = await _load_user(uid) if uid else None

    if uid:
        # Extrato por usuário
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == uid
                    )
                )
                total = int(res.scalar() or 0)
        except Exception:
            total = 0

        total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
        page = max(1, min(page, total_p))

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Transaction)
                    .where(Transaction.user_id == uid)
                    .order_by(Transaction.created_at.desc())
                    .offset(page_offset(page, ADMIN_PAGE_SIZE_DEFAULT))
                    .limit(ADMIN_PAGE_SIZE_DEFAULT)
                )
                txs = list(res.scalars().all())
        except Exception:
            txs = []

        if not txs:
            text = "📊 <i>Nenhuma transação encontrada.</i>"
        else:
            header = (
                f"📊 <b>EXTRATO · {truncate(u.first_name or '—', 20) if u else uid}</b> "
                f"({total})\n"
                f"💰 Saldo atual: <b>{fmt_brl(u.balance) if u else '—'}</b>\n"
            )
            linhas = [header, ""]
            for t in txs:
                emoji = _kind_emoji(t)
                sign = _sign(t)
                linhas.append(
                    f"{emoji} {sign} {fmt_brl(t.amount)} · "
                    f"<code>{getattr(t.type, 'value', t.type)}</code> · "
                    f"{fmt_dt(t.created_at)}"
                )
            text = "\n".join(linhas)

        pag = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=f"{CB_EXTRACT}:{uid}:page",
            back_callback=CB_OVERVIEW,
        )
        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="🔍 Buscar outro", callback_data=CB_SEARCH_USER),
            ],
        ]
        if pag:
            rows_kb.append(pag)
        rows_kb.append(build_back_row(CB_OVERVIEW))
    else:
        text = (
            "📊 <b>EXTRATO</b>\n\n"
            "Envie o ID do usuário cujo extrato deseja ver."
        )
        await AdminFlow.set_awaiting(
            admin_id,
            field="wallet_extract_user",
            screen="wallet:extract",
        )
        rows_kb = [[InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH_USER)]]
        rows_kb.append(build_back_row(CB_OVERVIEW))

    if isinstance(target, CallbackQuery):
        await edit_or_send(target, text=text, reply_markup=kb(rows_kb))
    else:
        await target.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb(rows_kb),
            disable_web_page_preview=True,
        )


@router.callback_query(F.data.startswith(f"{CB_EXTRACT}:"))
async def on_extract(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:wallet:extract:UID:page:N
        uid = 0
        page = 1
        try:
            uid = int(parts[3])
        except Exception:
            uid = 0
        if len(parts) >= 6 and parts[4] == "page":
            try:
                page = int(parts[5])
            except Exception:
                page = 1

        await _render_extract(callback, admin_id, uid, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_extract falhou: {exc}")


# ============================================================
# ESTORNO (refund de transação)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REFUND}:"))
async def on_refund(callback: CallbackQuery) -> None:
    """
    Formato: admin:wallet:refund:TXID
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return  # handler do confirm
        try:
            txid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Transaction).where(Transaction.id == txid)
                )
                tx: Optional[Transaction] = r.scalar_one_or_none()
                if tx is None:
                    await toast_err(callback, "❌ Transação não encontrada.")
                    return

                text = (
                    f"♻️ <b>ESTORNAR TRANSAÇÃO</b>\n\n"
                    f"ID: <code>{tx.id}</code>\n"
                    f"Usuário: <code>{tx.user_id}</code>\n"
                    f"Valor: <b>{fmt_brl(tx.amount)}</b>\n"
                    f"Tipo: <code>{getattr(tx.type, 'value', tx.type)}</code>\n\n"
                    "Isso criará uma transação reversa e ajustará o saldo.\n"
                    "Tem certeza?"
                )

                confirm_kb = build_confirm_keyboard(
                    confirm_callback=f"{CB_REFUND_YES}:{txid}",
                    cancel_callback=CB_OVERVIEW,
                    confirm_text="♻️ Sim, estornar",
                    cancel_text="❌ Cancelar",
                )

                await edit_or_send(callback, text=text, reply_markup=confirm_kb)
                await answer(callback)
        except Exception as exc:
            logger.exception(f"on_refund falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
    except Exception as exc:
        logger.exception(f"wallet on_refund falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REFUND_YES}:"))
async def on_refund_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            txid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Transaction).where(Transaction.id == txid)
                )
                tx: Optional[Transaction] = r.scalar_one_or_none()
                if tx is None:
                    await toast_err(callback, "❌ Transação não encontrada.")
                    return

                kind = str(getattr(tx.type, "value", tx.type) or "").upper()
                amount = float(tx.amount or 0)

                # Se era crédito → estorno é débito; vice-versa
                is_credit = kind in {x.value for x in CREDIT_TYPES}

                ures = await session.execute(
                    select(User).where(User.id == tx.user_id).with_for_update()
                )
                u: Optional[User] = ures.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return

                if is_credit:
                    # Remove do saldo
                    new_balance = round(float(u.balance or 0) - amount, 2)
                    u.balance = Decimal(str(new_balance))
                    tx_type = TransactionType.ADMIN_DEBIT
                else:
                    new_balance = round(float(u.balance or 0) + amount, 2)
                    u.balance = Decimal(str(new_balance))
                    tx_type = TransactionType.ADMIN_CREDIT

                session.add(u)
                session.add(
                    Transaction(
                        user_id=u.id,
                        type=tx_type,
                        amount=Decimal(str(round(amount, 2))),
                        balance_after=Decimal(str(new_balance)),
                        description=f"Estorno da transação #{txid}",
                        reference=f"refund:{txid}",
                    )
                )

                tg_id = int(u.telegram_id)
        except Exception as exc:
            logger.exception(f"refund falhou: {exc}")
            await toast_err(callback, "❌ Erro ao estornar.")
            return

        # Notifica
        try:
            await callback.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"♻️ <b>Transação estornada</b>\n\n"
                    f"Valor: <b>{fmt_brl(amount)}</b>\n"
                    f"O saldo foi ajustado automaticamente."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="wallet.refund",
            category="admin",
            details={"tx_id": txid, "amount": amount},
        )

        await edit_or_send(
            callback,
            text=f"✅ <b>Estorno realizado.</b>\nValor: {fmt_brl(amount)}",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]]
            ),
        )
        await toast_ok(callback, "♻️ Estornado.")
    except Exception as exc:
        logger.exception(f"wallet on_refund_confirm falhou: {exc}")


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
                # Soma saldos
                r = await session.execute(
                    select(func.coalesce(func.sum(User.balance), 0))
                )
                saldo_sistema = float(r.scalar() or 0)

                # Soma por tipo (créditos - débitos) histórico
                r = await session.execute(
                    select(func.coalesce(func.sum(Transaction.amount), 0))
                    .where(Transaction.type.in_(CREDIT_TYPES))
                )
                total_credit = float(r.scalar() or 0)

                r = await session.execute(
                    select(func.coalesce(func.sum(Transaction.amount), 0))
                    .where(Transaction.type.in_(DEBIT_TYPES))
                )
                total_debit = float(r.scalar() or 0)

                saldo_teorico = round(total_credit - total_debit, 2)
                diferenca = round(saldo_sistema - saldo_teorico, 2)
        except Exception as exc:
            logger.debug(f"recon falhou: {exc}")
            await toast_err(callback, "❌ Erro na conciliação.")
            return

        conf = "🟢 OK" if abs(diferenca) < 0.01 else "⚠️ DIVERGENTE"

        text = (
            "🔍 <b>CONCILIAÇÃO</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Saldo dos usuários: <b>{fmt_brl(saldo_sistema)}</b>\n"
            f"➕ Total creditado (histórico): <b>{fmt_brl(total_credit)}</b>\n"
            f"➖ Total debitado (histórico): <b>{fmt_brl(total_debit)}</b>\n"
            f"🧮 Saldo teórico: <b>{fmt_brl(saldo_teorico)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Diferença: <b>{fmt_brl(diferenca)}</b>\n"
            f"🔎 Status: <b>{conf}</b>"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_recon falhou: {exc}")


# ============================================================
# HISTÓRICO DE AJUSTES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_HIST_ADJ}:"))
async def on_hist_adjust(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        page = 1
        if len(parts) >= 4 and parts[3] == "page":
            try:
                page = int(parts[4])
            except Exception:
                page = 1

        try:
            async with session_scope() as session:
                total = int(
                    (
                        await session.execute(
                            select(func.count(Transaction.id)).where(
                                Transaction.type.in_(
                                    (
                                        TransactionType.ADMIN_CREDIT,
                                        TransactionType.ADMIN_DEBIT,
                                    )
                                )
                            )
                        )
                    ).scalar()
                    or 0
                )

                total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
                page = max(1, min(page, total_p))

                res = await session.execute(
                    select(Transaction)
                    .where(
                        Transaction.type.in_(
                            (
                                TransactionType.ADMIN_CREDIT,
                                TransactionType.ADMIN_DEBIT,
                            )
                        )
                    )
                    .order_by(Transaction.created_at.desc())
                    .offset(page_offset(page, ADMIN_PAGE_SIZE_DEFAULT))
                    .limit(ADMIN_PAGE_SIZE_DEFAULT)
                )
                ajustes = list(res.scalars().all())
        except Exception:
            total = 0
            total_p = 1
            ajustes = []

        if not ajustes:
            text = "📜 <i>Nenhum ajuste manual registrado.</i>"
        else:
            lines = [f"📜 <b>HISTÓRICO DE AJUSTES</b> ({total})", ""]
            for t in ajustes:
                emoji = _kind_emoji(t)
                sign = _sign(t)
                reason = ""
                try:
                    md = t.metadata_ or {}
                    if isinstance(md, dict):
                        reason = md.get("reason", "") or ""
                except Exception:
                    pass
                lines.append(
                    f"{emoji} {sign} {fmt_brl(t.amount)} · User #{t.user_id} · "
                    f"{fmt_dt(t.created_at, with_time=False)}\n"
                    f"    <i>{truncate(reason, 40) or '—'}</i>"
                )
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        pag = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=f"{CB_HIST_ADJ}:page",
            back_callback=CB_OVERVIEW,
        )
        if pag:
            rows_kb.append(pag)
        rows_kb.append(build_back_row(CB_OVERVIEW))

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"wallet on_hist_adjust falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_overview",
    "on_overview",
    "on_wallets",
    "on_tx_list",
    "on_balance_start",
    "on_search_user",
    "on_wallet_text_input",
    "on_extract",
    "on_refund",
    "on_refund_confirm",
    "on_recon",
    "on_hist_adjust",
    "CB_ROOT",
    "CB_OVERVIEW",
    "CB_WALLETS",
    "CB_TX",
    "CB_ADD_BAL",
    "CB_REM_BAL",
    "CB_ADJUST",
    "CB_REFUND",
    "CB_REFUND_YES",
    "CB_EXTRACT",
    "CB_RECON",
    "CB_HIST_ADJ",
    "CB_SEARCH_USER",
    "CREDIT_TYPES",
    "DEBIT_TYPES",
]
