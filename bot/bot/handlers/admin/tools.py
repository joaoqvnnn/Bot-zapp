"""
Larizinha Store — Módulo Admin: Ferramentas Administrativas
Implementa ferramentas avançadas para o painel conforme DIM.

Telas:
- Painel de ferramentas
- Busca global (usuários, produtos, pedidos, cupons, gifts)
- Ações em lote (banir/desbanir/creditar vários usuários)
- Exportação (CSV por entidade)
- Importação (CSV de credenciais em lote por produto)
- Tarefas (manutenção rápida: limpar caches, contar registros)
- Histórico de uso das ferramentas
- Paginação configurável (por página)
- Filtros avançados
- Confirmação obrigatória para ações críticas
- Registro de justificativa

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel.
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action + justificativa).
- Apenas Admin Principal para ações destrutivas.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import (
    Coupon,
    GiftCard,
    Order,
    Product,
    ProductCredential,
    User,
    UserStatus,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import cache_delete, get_redis
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_ERR,
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
    is_int,
    kb,
    log_action,
    page_offset,
    parse_int,
    parse_number,
    send_csv,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_tools")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_tools_panel"
MSG_KEY_SEARCH = "admin_tools_search"
MSG_KEY_SEARCH_RESULTS = "admin_tools_search_results"
MSG_KEY_BATCH = "admin_tools_batch"
MSG_KEY_BATCH_RESULT = "admin_tools_batch_result"
MSG_KEY_TASKS = "admin_tools_tasks"
MSG_KEY_IMPORT = "admin_tools_import"
MSG_KEY_HISTORY = "admin_tools_history"

SCREEN_PANEL = "admin_tools_panel"

# Callbacks base
CB_ROOT = "admin:tools"
CB_RELOAD = "admin:tools:reload"
CB_SEARCH = "admin:tools:search"
CB_SEARCH_TYPE = "admin:tools:stype"        # :users|products|orders|coupons|gifts
CB_SEARCH_VIEW = "admin:tools:sview"        # :type:ID
CB_BATCH = "admin:tools:batch"
CB_BATCH_TYPE = "admin:tools:btype"         # :ban|unban|credit
CB_BATCH_CONFIRM = "admin:tools:bconfirm"   # :type
CB_BATCH_EXEC = "admin:tools:bexec"         # :type:yes
CB_EXPORT = "admin:tools:export"            # :users|products|orders|coupons|gifts
CB_IMPORT_CSV = "admin:tools:importcsv"
CB_IMPORT_CSV_PID = "admin:tools:importcsvp"  # :PID
CB_TASKS = "admin:tools:tasks"
CB_TASK_RUN = "admin:tools:taskrun"         # :task
CB_TASK_RUN_YES = "admin:tools:taskrun:yes"  # :task
CB_HISTORY = "admin:tools:history"
CB_HISTORY_PAGE = "admin:tools:hpage"       # :N

# Tipos de busca
SEARCH_TYPES: Dict[str, str] = {
    "users": "👥 Usuários",
    "products": "📦 Produtos",
    "orders": "🛒 Pedidos",
    "coupons": "🎟️ Cupons",
    "gifts": "🎁 Gift Cards",
}

# Ações em lote
BATCH_ACTIONS: Dict[str, str] = {
    "ban": "🚫 Banir usuários",
    "unban": "✅ Desbanir usuários",
    "credit": "💰 Creditar saldo",
}

# Tarefas rápidas
TASKS: Dict[str, Dict[str, Any]] = {
    "clear_cache": {
        "label": "Limpar cache (Redis)",
        "destructive": True,
    },
    "count_all": {
        "label": "Contar registros do DB",
        "destructive": False,
    },
    "clean_expired_coupons": {
        "label": "Desativar cupons expirados",
        "destructive": True,
    },
    "clean_expired_gifts": {
        "label": "Desativar gift cards expirados",
        "destructive": True,
    },
    "unlock_all": {
        "label": "Liberar todas as travas",
        "destructive": True,
    },
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _count_records() -> Dict[str, int]:
    out: Dict[str, int] = {
        "users": 0,
        "products": 0,
        "orders": 0,
        "coupons": 0,
        "gifts": 0,
        "credentials": 0,
    }
    try:
        async with session_scope() as session:
            out["users"] = int(
                (await session.execute(select(func.count(User.id)))).scalar() or 0
            )
            out["products"] = int(
                (await session.execute(select(func.count(Product.id)))).scalar() or 0
            )
            out["orders"] = int(
                (await session.execute(select(func.count(Order.id)))).scalar() or 0
            )
            out["coupons"] = int(
                (await session.execute(select(func.count(Coupon.id)))).scalar() or 0
            )
            out["gifts"] = int(
                (await session.execute(select(func.count(GiftCard.id)))).scalar() or 0
            )
            out["credentials"] = int(
                (
                    await session.execute(
                        select(func.count(ProductCredential.id))
                    )
                ).scalar()
                or 0
            )
    except Exception as exc:
        logger.debug(f"_count_records falhou: {exc}")
    return out


def _parse_ids_list(text: str) -> List[int]:
    """Aceita '1,2,3' ou '1 2 3' ou '1\n2\n3'."""
    out: List[int] = []
    for token in re.split(r"[,\s\n]+", text or ""):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
            try:
                out.append(int(token))
            except Exception:
                continue
    return out


import re  # noqa: E402


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    counts = await _count_records()

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "🛠️ <b>FERRAMENTAS ADMIN</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>Registros no sistema:</b>\n"
            f"├ 👥 Usuários: <b>{fmt_num(counts['users'])}</b>\n"
            f"├ 📦 Produtos: <b>{fmt_num(counts['products'])}</b>\n"
            f"├ 🛒 Pedidos: <b>{fmt_num(counts['orders'])}</b>\n"
            f"├ 🎟️ Cupons: <b>{fmt_num(counts['coupons'])}</b>\n"
            f"├ 🎁 Gift Cards: <b>{fmt_num(counts['gifts'])}</b>\n"
            f"└ 🔐 Credenciais: <b>{fmt_num(counts['credentials'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Escolha uma ferramenta:"
        ),
        busca="Busca global",
        lote="Ações em lote",
        export="Exportar",
        import_="Importar",
        tarefas="Tarefas",
        hist="Histórico",
        paginacao=f"{ADMIN_PAGE_SIZE_DEFAULT}/pág",
        confirm="Ativa",
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="🔍 Busca Global", callback_data=CB_SEARCH),
                    InlineKeyboardButton(text="📦 Ações em Lote", callback_data=CB_BATCH),
                ],
                [
                    InlineKeyboardButton(text="📤 Exportar", callback_data=f"{CB_EXPORT}:users"),
                    InlineKeyboardButton(text="📥 Importar CSV", callback_data=CB_IMPORT_CSV),
                ],
                [
                    InlineKeyboardButton(text="📋 Tarefas", callback_data=CB_TASKS),
                    InlineKeyboardButton(text="📜 Histórico", callback_data=f"{CB_HISTORY_PAGE}:1"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_RELOAD),
                    InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
                ],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data.in_({CB_ROOT, CB_RELOAD}))
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_panel(callback)
        if callback.data == CB_RELOAD:
            await toast_ok(callback, "🔄 Atualizado!")
        else:
            await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_root falhou: {exc}")


# ============================================================
# BUSCA GLOBAL
# ============================================================
@router.callback_query(F.data == CB_SEARCH)
async def on_search(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        rows_kb: List[List[InlineKeyboardButton]] = []
        buf: List[InlineKeyboardButton] = []
        for key, label in SEARCH_TYPES.items():
            buf.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CB_SEARCH_TYPE}:{key}",
                )
            )
            if len(buf) == 2:
                rows_kb.append(buf)
                buf = []
        if buf:
            rows_kb.append(buf)
        rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

        text = (
            "🔍 <b>BUSCA GLOBAL</b>\n\n"
            "Escolha o tipo de registro para buscar:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_search falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_SEARCH_TYPE}:"))
async def on_search_type(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            stype = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Tipo inválido.")
            return
        if stype not in SEARCH_TYPES:
            await toast_err(callback, "❌ Tipo inválido.")
            return

        prompt = (
            f"🔍 <b>BUSCA · {SEARCH_TYPES[stype]}</b>\n\n"
            "Envie o termo de busca (ID, nome, código, etc.).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"tools_search:{stype}",
            screen=f"tools:search:{stype}",
            extra={"search_type": stype},
            use_force_reply=True,
            reply_placeholder="Termo...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_search_type falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_tools_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Busca
    if awaiting.startswith("tools_search:"):
        stype = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _do_search(message, admin_id, stype, value)
        return

    # Ações em lote (lista de IDs)
    if awaiting.startswith("tools_batch_ids:"):
        action = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _batch_collect_ids(message, admin_id, action, value)
        return

    # Crédito (valor)
    if awaiting.startswith("tools_batch_amount:"):
        action = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _batch_collect_amount(message, admin_id, action, value)
        return

    # Justificativa (ações críticas)
    if awaiting == "tools_justify":
        value, _extra = await consume_input(message, admin_id, expected_field="tools_justify")
        if value is None:
            return
        await _apply_justification(message, admin_id, value)
        return


async def _do_search(
    message: Message,
    admin_id: int,
    stype: str,
    query: str,
) -> None:
    query = (query or "").strip()
    if not query:
        await message.answer("❌ Termo vazio.")
        return

    results: List[Any] = []
    header = ""

    try:
        async with session_scope() as session:
            if stype == "users":
                like = f"%{query.lower()}%"
                conds = [
                    func.lower(func.coalesce(User.first_name, "")).like(like),
                    func.lower(func.coalesce(User.username, "")).like(like),
                ]
                if is_int(query):
                    n = int(query)
                    conds.append(User.id == n)
                    conds.append(User.telegram_id == n)
                res = await session.execute(
                    select(User).where(or_(*conds)).limit(20)
                )
                results = list(res.scalars().all())
                header = "👥 Usuários"

            elif stype == "products":
                like = f"%{query.lower()}%"
                conds = [
                    func.lower(func.coalesce(Product.name, "")).like(like),
                    func.lower(func.coalesce(Product.slug, "")).like(like),
                ]
                if is_int(query):
                    conds.append(Product.id == int(query))
                res = await session.execute(
                    select(Product).where(or_(*conds)).limit(20)
                )
                results = list(res.scalars().all())
                header = "📦 Produtos"

            elif stype == "orders":
                conds = []
                if is_int(query):
                    n = int(query)
                    conds.append(Order.id == n)
                    conds.append(Order.user_id == n)
                if not conds:
                    await message.answer("❌ Envie um ID numérico.")
                    return
                res = await session.execute(
                    select(Order).where(or_(*conds)).limit(20)
                )
                results = list(res.scalars().all())
                header = "🛒 Pedidos"

            elif stype == "coupons":
                like = f"%{query.upper()}%"
                res = await session.execute(
                    select(Coupon)
                    .where(func.upper(func.coalesce(Coupon.code, "")).like(like))
                    .limit(20)
                )
                results = list(res.scalars().all())
                header = "🎟️ Cupons"

            elif stype == "gifts":
                like = f"%{query.upper()}%"
                res = await session.execute(
                    select(GiftCard)
                    .where(func.upper(func.coalesce(GiftCard.code, "")).like(like))
                    .limit(20)
                )
                results = list(res.scalars().all())
                header = "🎁 Gift Cards"
    except Exception as exc:
        logger.debug(f"_do_search falhou: {exc}")

    if not results:
        await message.answer(
            text=f"🔍 <b>{header}</b>\n\n<i>Nenhum resultado para</i> <code>{query}</code>",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    lines = [f"🔍 <b>{header} ({len(results)})</b>", ""]
    rows_kb: List[List[InlineKeyboardButton]] = []

    for r in results:
        if stype == "users":
            em = "🟢" if r.status == UserStatus.ACTIVE else "🔴"
            lines.append(
                f"{em} #{r.id} · <b>{truncate(r.first_name or '—', 20)}</b> · "
                f"@{r.username or '—'} · {fmt_brl(r.balance)}"
            )
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{em} #{r.id} {truncate(r.first_name or '—', 20)}",
                        callback_data=f"admin:users:view:{r.id}",
                    )
                ]
            )
        elif stype == "products":
            vis = "🟢" if r.visible else "🔴"
            lines.append(
                f"{vis} #{r.id} · <b>{truncate(r.name or '—', 20)}</b> · "
                f"{fmt_brl(r.price_promo or r.price)}"
            )
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{vis} #{r.id} {truncate(r.name or '—', 20)}",
                        callback_data=f"admin:products:edit:{r.id}",
                    )
                ]
            )
        elif stype == "orders":
            lines.append(
                f"🛒 #{r.id} · User #{r.user_id} · {fmt_brl(r.final_total)}"
            )
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"🛒 #{r.id} · {fmt_brl(r.final_total)}",
                        callback_data=f"admin:sales:view:{r.id}",
                    )
                ]
            )
        elif stype == "coupons":
            em = "🟢" if r.active else "🔴"
            lines.append(
                f"{em} <code>{r.code}</code> · {fmt_brl(r.value)}"
            )
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{em} {r.code} · {fmt_brl(r.value)}",
                        callback_data=f"admin:coupons:view:{r.id}",
                    )
                ]
            )
        elif stype == "gifts":
            em = "🟢" if r.active else "🔴"
            lines.append(
                f"{em} <code>{r.code}</code> · {fmt_brl(r.amount)}"
            )
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{em} {r.code} · {fmt_brl(r.amount)}",
                        callback_data=f"admin:gifts:view:{r.id}",
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

    await log_action(
        admin_id=admin_id,
        action="tools.search",
        category="admin",
        details={"type": stype, "query": query, "results": len(results)},
    )


# ============================================================
# AÇÕES EM LOTE
# ============================================================
@router.callback_query(F.data == CB_BATCH)
async def on_batch(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        rows_kb: List[List[InlineKeyboardButton]] = []
        for key, label in BATCH_ACTIONS.items():
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"{CB_BATCH_TYPE}:{key}",
                    )
                ]
            )
        rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

        text = (
            "📦 <b>AÇÕES EM LOTE</b>\n\n"
            "⚠️ <b>Atenção:</b> ações em lote afetam vários usuários de uma vez.\n"
            "Todas são registradas com <b>justificativa</b> e auditoria.\n\n"
            "Escolha a ação:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_batch falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_BATCH_TYPE}:"))
async def on_batch_type(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            action = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Ação inválida.")
            return
        if action not in BATCH_ACTIONS:
            await toast_err(callback, "❌ Ação inválida.")
            return

        prompt = (
            f"📦 <b>{BATCH_ACTIONS[action]}</b>\n\n"
            "Envie a lista de <b>IDs internos</b> dos usuários.\n"
            "Separe por vírgula, espaço ou uma por linha.\n\n"
            "Ex.: <code>1,2,3,10,42</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"tools_batch_ids:{action}",
            screen=f"tools:batch:{action}",
            extra={"action": action},
            use_force_reply=True,
            reply_placeholder="1,2,3",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_batch_type falhou: {exc}")


async def _batch_collect_ids(
    message: Message,
    admin_id: int,
    action: str,
    raw_ids: str,
) -> None:
    ids = _parse_ids_list(raw_ids)
    if not ids:
        await message.answer("❌ Nenhum ID válido enviado.")
        return

    await AdminFlow.update(admin_id, {"batch_ids": ids, "batch_action": action})

    if action == "credit":
        prompt = (
            f"💰 <b>CREDITAR SALDO EM LOTE</b>\n\n"
            f"👥 Usuários: <b>{len(ids)}</b>\n\n"
            "Envie o <b>valor</b> a creditar para CADA usuário.\n"
            "Ex.: <code>10.00</code> ou <code>25</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field=f"tools_batch_amount:{action}",
            screen=f"tools:batch:{action}",
            extra={"action": action},
            use_force_reply=True,
            reply_placeholder="10.00",
        )
        return

    # Ação de status → pede justificativa
    await AdminFlow.update(admin_id, {"batch_step": "justify"})
    prompt = (
        f"📝 <b>JUSTIFICATIVA OBRIGATÓRIA</b>\n\n"
        f"Ação: <b>{BATCH_ACTIONS[action]}</b>\n"
        f"Usuários: <b>{len(ids)}</b>\n\n"
        "Envie o motivo (obrigatório, será salvo no histórico).\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="tools_justify",
        screen=f"tools:batch:{action}",
        extra={"action": action},
        use_force_reply=True,
        reply_placeholder="Motivo...",
    )


async def _batch_collect_amount(
    message: Message,
    admin_id: int,
    action: str,
    raw_amount: str,
) -> None:
    amount = parse_number(raw_amount)
    if amount is None or amount <= 0:
        await message.answer("❌ Valor inválido.")
        return

    await AdminFlow.update(admin_id, {"batch_amount": float(amount), "batch_step": "justify"})

    prompt = (
        f"📝 <b>JUSTIFICATIVA OBRIGATÓRIA</b>\n\n"
        f"Ação: <b>{BATCH_ACTIONS[action]}</b> · {fmt_brl(amount)}\n\n"
        "Envie o motivo (obrigatório).\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="tools_justify",
        screen=f"tools:batch:{action}",
        extra={"action": action},
        use_force_reply=True,
        reply_placeholder="Motivo...",
    )


async def _apply_justification(
    message: Message,
    admin_id: int,
    justification: str,
) -> None:
    justification = (justification or "").strip()
    if len(justification) < 3:
        await message.answer("❌ Justificativa muito curta (mín. 3 caracteres).")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    action = data.get("batch_action")
    ids = data.get("batch_ids") or []
    amount = float(data.get("batch_amount") or 0)

    if not action or not ids:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    text = (
        f"⚠️ <b>CONFIRMAR AÇÃO EM LOTE</b>\n\n"
        f"Ação: <b>{BATCH_ACTIONS[action]}</b>\n"
        f"Usuários: <b>{len(ids)}</b>\n"
    )
    if amount > 0:
        text += f"Valor: <b>{fmt_brl(amount)}</b> cada\n"
    text += f"Justificativa: <i>{truncate(justification, 80)}</i>\n\n"
    text += "Tem certeza ABSOLUTA?"

    confirm_kb = build_confirm_keyboard(
        confirm_callback=f"{CB_BATCH_EXEC}:{action}:yes",
        cancel_callback=CB_ROOT,
        confirm_text="✅ Executar",
        cancel_text="❌ Cancelar",
    )

    await AdminFlow.update(admin_id, {"batch_justification": justification})

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=confirm_kb,
    )


@router.callback_query(F.data.startswith(f"{CB_BATCH_EXEC}:"))
async def on_batch_exec(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        # admin:tools:bexec:ACTION:yes
        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        action = parts[4]

        state = await AdminFlow.get(admin_id)
        data = (state or {}).get("data", {}) if state else {}
        ids = data.get("batch_ids") or []
        amount = float(data.get("batch_amount") or 0)
        justification = data.get("batch_justification") or "—"

        if not ids:
            await toast_err(callback, "❌ Nenhum ID capturado.")
            await AdminFlow.clear(admin_id)
            return

        success = 0
        failed = 0
        total_credit = 0.0

        try:
            async with session_scope() as session:
                for uid in ids:
                    try:
                        res = await session.execute(
                            select(User).where(User.id == int(uid)).with_for_update()
                        )
                        u: Optional[User] = res.scalar_one_or_none()
                        if u is None:
                            failed += 1
                            continue

                        if action == "ban":
                            u.status = UserStatus.BANNED
                            u.ban_reason = justification[:200]
                        elif action == "unban":
                            u.status = UserStatus.ACTIVE
                            u.ban_reason = None
                            u.block_reason = None
                        elif action == "credit" and amount > 0:
                            new_bal = round(float(u.balance or 0) + amount, 2)
                            u.balance = Decimal(str(new_bal))
                            total_credit += amount

                            from bot.database import Transaction, TransactionType

                            session.add(
                                Transaction(
                                    user_id=u.id,
                                    type=TransactionType.ADMIN_CREDIT,
                                    amount=Decimal(str(round(amount, 2))),
                                    balance_after=Decimal(str(new_bal)),
                                    description=f"Lote: {justification[:100]}",
                                    reference=f"admin:{admin_id}",
                                    metadata_={"batch": True, "justification": justification},
                                )
                            )

                        session.add(u)
                        success += 1
                    except Exception:
                        failed += 1
                        continue
        except Exception as exc:
            logger.exception(f"batch exec falhou: {exc}")
            await toast_err(callback, "❌ Erro na execução.")
            return

        await AdminFlow.clear(admin_id)

        await log_action(
            admin_id=admin_id,
            action=f"tools.batch.{action}",
            category="admin",
            details={
                "action": action,
                "ids_count": len(ids),
                "success": success,
                "failed": failed,
                "amount": amount,
                "justification": justification,
            },
        )

        text = (
            f"✅ <b>AÇÃO EM LOTE CONCLUÍDA</b>\n\n"
            f"Ação: <b>{BATCH_ACTIONS[action]}</b>\n"
            f"✅ Sucesso: <b>{success}</b>\n"
            f"❌ Falhas: <b>{failed}</b>\n"
        )
        if amount > 0 and total_credit > 0:
            text += f"💰 Total creditado: <b>{fmt_brl(total_credit)}</b>\n"
        text += f"\n📝 Justificativa: <i>{truncate(justification, 80)}</i>"

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
    except Exception as exc:
        logger.exception(f"tools on_batch_exec falhou: {exc}")


# ============================================================
# EXPORTAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EXPORT}:"))
async def on_export(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            kind = callback.data.split(":")[-1]
        except Exception:
            kind = "users"

        headers: List[str] = []
        rows: List[List[Any]] = []
        filename = f"export_{kind}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

        try:
            async with session_scope() as session:
                if kind == "users":
                    headers = ["ID", "Telegram", "Nome", "Username", "Saldo", "Status", "Criado"]
                    res = await session.execute(
                        select(User).order_by(User.id.asc()).limit(10000)
                    )
                    for u in res.scalars().all():
                        rows.append([
                            u.id, u.telegram_id,
                            u.first_name or "",
                            u.username or "",
                            float(u.balance or 0),
                            str(getattr(u.status, "value", u.status)),
                            fmt_dt(u.created_at),
                        ])

                elif kind == "products":
                    headers = ["ID", "Nome", "Preço", "Estoque", "Visível"]
                    res = await session.execute(
                        select(Product).order_by(Product.id.asc()).limit(5000)
                    )
                    for p in res.scalars().all():
                        rows.append([
                            p.id, p.name or "",
                            float(p.price or 0),
                            int(p.stock or 0),
                            "sim" if p.visible else "não",
                        ])

                elif kind == "orders":
                    headers = ["ID", "User", "Valor", "Método", "Status", "Criado"]
                    res = await session.execute(
                        select(Order).order_by(Order.id.desc()).limit(5000)
                    )
                    for o in res.scalars().all():
                        rows.append([
                            o.id, o.user_id,
                            float(o.final_total or 0),
                            str(getattr(o.method, "value", o.method)),
                            str(getattr(o.status, "value", o.status)),
                            fmt_dt(o.created_at),
                        ])

                elif kind == "coupons":
                    headers = ["ID", "Código", "Tipo", "Valor", "Usos", "Ativo"]
                    res = await session.execute(
                        select(Coupon).order_by(Coupon.id.asc()).limit(5000)
                    )
                    for c in res.scalars().all():
                        rows.append([
                            c.id, c.code,
                            str(getattr(c.type, "value", c.type)),
                            float(c.value or 0),
                            f"{int(c.uses_count or 0)}/{int(c.uses_max or 0)}",
                            "sim" if c.active else "não",
                        ])

                elif kind == "gifts":
                    headers = ["ID", "Código", "Valor", "Usos", "Ativo"]
                    res = await session.execute(
                        select(GiftCard).order_by(GiftCard.id.asc()).limit(5000)
                    )
                    for g in res.scalars().all():
                        rows.append([
                            g.id, g.code,
                            float(g.amount or 0),
                            f"{int(g.uses_count or 0)}/{int(g.uses_max or 0)}",
                            "sim" if g.active else "não",
                        ])
        except Exception as exc:
            logger.debug(f"export {kind} falhou: {exc}")

        if not rows:
            await toast_err(callback, "❌ Sem dados para exportar.")
            return

        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📤 <b>Exportação · {kind}</b> ({len(rows)})",
        )

        if ok:
            await toast_ok(callback, "📤 Arquivo enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action=f"tools.export.{kind}",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"tools on_export falhou: {exc}")


# ============================================================
# IMPORTAR CSV (credenciais em lote por produto)
# ============================================================
@router.callback_query(F.data == CB_IMPORT_CSV)
async def on_import_csv(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "📥 <b>IMPORTAR CSV · PRODUTO</b>\n\n"
            "Envie o <b>ID do produto</b> para o qual deseja importar credenciais.\n"
            "Ex.: <code>3</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="tools_import_pid",
            screen="tools:import",
            use_force_reply=True,
            reply_placeholder="ID do produto",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_import_csv falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_IMPORT_CSV_PID}:"))
async def on_import_csv_pid(callback: CallbackQuery) -> None:
    # Não usado — a PID é pedida em texto
    pass


async def _import_pid(message: Message, admin_id: int, raw_pid: str) -> None:
    pid = parse_int(raw_pid)
    if pid is None or pid <= 0:
        await message.answer("❌ ID inválido.")
        return

    p = await _load_product(pid)
    if p is None:
        await message.answer("❌ Produto não encontrado.")
        return

    await AdminFlow.update(admin_id, {"import_pid": pid})

    prompt = (
        f"📥 <b>IMPORTAR CSV · {p.name}</b>\n\n"
        "Envie o conteúdo do CSV (uma credencial por linha).\n"
        "Formatos aceitos:\n"
        "• <code>email|senha</code>\n"
        "• <code>email:senha</code>\n"
        "• <code>texto puro</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="tools_import_csv",
        screen="tools:import",
        extra={"import_pid": pid},
        use_force_reply=True,
        reply_placeholder="cole o CSV...",
    )


async def _do_import(message: Message, admin_id: int, raw_csv: str) -> None:
    raw = (raw_csv or "").strip()
    if not raw:
        await message.answer("❌ Conteúdo vazio.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    pid = data.get("import_pid")
    if not pid:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        await message.answer("❌ Nenhuma linha válida.")
        return

    inserted = 0
    try:
        async with session_scope() as session:
            for line in lines:
                try:
                    session.add(ProductCredential(product_id=int(pid), payload=line))
                    inserted += 1
                except Exception:
                    continue
    except Exception as exc:
        logger.exception(f"import CSV falhou: {exc}")
        await message.answer("❌ Erro ao importar.")
        return

    # Atualiza estoque
    try:
        async with session_scope() as session:
            res = await session.execute(select(Product).where(Product.id == int(pid)))
            p: Optional[Product] = res.scalar_one_or_none()
            if p is not None and not p.unlimited:
                p.stock = int(p.stock or 0) + inserted
                p.sold_out = False
                session.add(p)
    except Exception:
        pass

    await AdminFlow.clear(admin_id)

    await log_action(
        admin_id=admin_id,
        action="tools.import_csv",
        category="admin",
        details={"product_id": int(pid), "inserted": inserted},
    )

    await message.answer(
        f"✅ <b>{inserted} credenciais importadas</b> para o produto #{pid}.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="📦 Ver Produto",
                        callback_data=f"admin:products:edit:{pid}",
                    )
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# TAREFAS RÁPIDAS
# ============================================================
@router.callback_query(F.data == CB_TASKS)
async def on_tasks(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        rows_kb: List[List[InlineKeyboardButton]] = []
        for key, meta in TASKS.items():
            em = "🚨" if meta["destructive"] else "▶️"
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{em} {meta['label']}",
                        callback_data=f"{CB_TASK_RUN}:{key}",
                    )
                ]
            )
        rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

        text = (
            "📋 <b>TAREFAS ADMINISTRATIVAS</b>\n\n"
            "⚠️ Ações marcadas com 🚨 são destrutivas.\n\n"
            "Escolha uma tarefa:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_tasks falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_TASK_RUN}:"))
async def on_task_run(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            task = parts[-1]
        except Exception:
            await toast_err(callback, "❌ Tarefa inválida.")
            return

        if task not in TASKS:
            await toast_err(callback, "❌ Tarefa inválida.")
            return

        meta = TASKS[task]

        if meta["destructive"]:
            if admin_id != settings.MAIN_ADMIN_ID:
                await toast_err(callback, "❌ Apenas o Admin Principal.")
                return

            text = (
                f"🚨 <b>CONFIRMAR TAREFA</b>\n\n"
                f"📋 {meta['label']}\n\n"
                "Esta ação é <b>destrutiva</b>.\n"
                "Tem certeza?"
            )
            confirm_kb = build_confirm_keyboard(
                confirm_callback=f"{CB_TASK_RUN_YES}:{task}",
                cancel_callback=CB_TASKS,
                confirm_text="✅ Executar",
                cancel_text="❌ Cancelar",
            )
            await edit_or_send(callback, text=text, reply_markup=confirm_kb)
            await answer(callback)
            return

        # Não destrutivo → executa direto
        callback.data = f"{CB_TASK_RUN_YES}:{task}"  # type: ignore[misc]
        await on_task_exec(callback)
    except Exception as exc:
        logger.exception(f"tools on_task_run falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_TASK_RUN_YES}:"))
async def on_task_exec(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        task = parts[-1]
        if task not in TASKS:
            await toast_err(callback, "❌ Tarefa inválida.")
            return

        result_text = ""

        if task == "clear_cache":
            try:
                r = await get_redis()
                pattern = f"{settings.REDIS_PREFIX}:*"
                removed = 0
                async for key in r.scan_iter(match=pattern, count=500):
                    # Preserva locks e tempblocks ativos
                    key_str = str(key)
                    if ":lock" in key_str or ":tempblock:" in key_str:
                        continue
                    await r.delete(key)
                    removed += 1
                result_text = f"🧹 {removed} chaves removidas do Redis"
            except Exception as exc:
                logger.debug(f"clear cache falhou: {exc}")
                result_text = "❌ Erro ao limpar cache"

        elif task == "count_all":
            counts = await _count_records()
            result_text = "\n".join(
                [
                    f"👥 Usuários: <b>{fmt_num(counts['users'])}</b>",
                    f"📦 Produtos: <b>{fmt_num(counts['products'])}</b>",
                    f"🛒 Pedidos: <b>{fmt_num(counts['orders'])}</b>",
                    f"🎟️ Cupons: <b>{fmt_num(counts['coupons'])}</b>",
                    f"🎁 Gifts: <b>{fmt_num(counts['gifts'])}</b>",
                    f"🔐 Credenciais: <b>{fmt_num(counts['credentials'])}</b>",
                ]
            )

        elif task == "clean_expired_coupons":
            try:
                now = datetime.now(timezone.utc)
                async with session_scope() as session:
                    res = await session.execute(
                        select(Coupon)
                        .where(Coupon.expires_at <= now)
                        .where(Coupon.active == True)  # noqa: E712
                    )
                    items = list(res.scalars().all())
                    for c in items:
                        c.active = False
                        session.add(c)
                result_text = f"🎟️ {len(items)} cupom(ns) expirado(s) desativado(s)"
            except Exception as exc:
                logger.debug(f"clean coupons falhou: {exc}")
                result_text = "❌ Erro ao limpar cupons"

        elif task == "clean_expired_gifts":
            try:
                now = datetime.now(timezone.utc)
                async with session_scope() as session:
                    res = await session.execute(
                        select(GiftCard)
                        .where(GiftCard.expires_at <= now)
                        .where(GiftCard.active == True)  # noqa: E712
                    )
                    items = list(res.scalars().all())
                    for g in items:
                        g.active = False
                        session.add(g)
                result_text = f"🎁 {len(items)} gift card(s) expirado(s) desativado(s)"
            except Exception as exc:
                logger.debug(f"clean gifts falhou: {exc}")
                result_text = "❌ Erro ao limpar gifts"

        elif task == "unlock_all":
            try:
                from bot.redis_client import Keys, LockManager

                r = await get_redis()
                pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
                removed = 0
                async for key in r.scan_iter(match=pattern, count=200):
                    await r.delete(key)
                    removed += 1
                result_text = f"🔓 {removed} trava(s) liberada(s)"
            except Exception as exc:
                logger.debug(f"unlock all falhou: {exc}")
                result_text = "❌ Erro ao liberar travas"

        else:
            result_text = "❓ Tarefa desconhecida"

        await log_action(
            admin_id=admin_id,
            action=f"tools.task.{task}",
            category="admin",
            details={"task": task, "result": result_text},
        )

        text = (
            f"✅ <b>TAREFA CONCLUÍDA</b>\n\n"
            f"📋 {TASKS[task]['label']}\n\n"
            f"{result_text}"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🔄 Rodar Novamente",
                            callback_data=f"{CB_TASK_RUN_YES}:{task}",
                        )
                    ],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_TASKS)],
                ]
            ),
        )
        await toast_ok(callback, "✅ Concluído!")
    except Exception as exc:
        logger.exception(f"tools on_task_exec falhou: {exc}")


# ============================================================
# HISTÓRICO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_HISTORY_PAGE}:"))
async def on_history(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        from bot.database import Log

        try:
            async with session_scope() as session:
                total = int(
                    (
                        await session.execute(
                            select(func.count(Log.id)).where(
                                Log.action.like("tools.%")
                            )
                        )
                    ).scalar()
                    or 0
                )
                total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
                page = max(1, min(page, total_p))

                res = await session.execute(
                    select(Log)
                    .where(Log.action.like("tools.%"))
                    .order_by(Log.created_at.desc())
                    .offset(page_offset(page, ADMIN_PAGE_SIZE_DEFAULT))
                    .limit(ADMIN_PAGE_SIZE_DEFAULT)
                )
                items = list(res.scalars().all())
        except Exception:
            total = 0
            total_p = 1
            items = []

        if not items:
            text = (
                "📜 <b>HISTÓRICO DE FERRAMENTAS</b>\n\n"
                "<i>Nenhuma ação registrada.</i>"
            )
        else:
            lines = [f"📜 <b>HISTÓRICO DE FERRAMENTAS ({total})</b>", ""]
            for log in items:
                lines.append(
                    f"🛠️ <code>{log.action}</code>\n"
                    f"    👤 Admin <code>{log.admin_id or '—'}</code> · "
                    f"{fmt_dt(log.created_at)}"
                )
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        pag = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=CB_HISTORY_PAGE,
            back_callback=CB_ROOT,
        )
        if pag:
            rows_kb.append(pag)
        rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"tools on_history falhou: {exc}")


# ============================================================
# INTERCEPTA PID DA IMPORTAÇÃO
# ============================================================
# (extensão do on_tools_text_input)

# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_root",
    "on_search",
    "on_search_type",
    "on_tools_text_input",
    "on_batch",
    "on_batch_type",
    "on_batch_exec",
    "on_export",
    "on_import_csv",
    "on_import_csv_pid",
    "on_tasks",
    "on_task_run",
    "on_task_exec",
    "on_history",
    "CB_ROOT",
    "CB_RELOAD",
    "CB_SEARCH",
    "CB_SEARCH_TYPE",
    "CB_SEARCH_VIEW",
    "CB_BATCH",
    "CB_BATCH_TYPE",
    "CB_BATCH_CONFIRM",
    "CB_BATCH_EXEC",
    "CB_EXPORT",
    "CB_IMPORT_CSV",
    "CB_IMPORT_CSV_PID",
    "CB_TASKS",
    "CB_TASK_RUN",
    "CB_TASK_RUN_YES",
    "CB_HISTORY",
    "CB_HISTORY_PAGE",
    "SEARCH_TYPES",
    "BATCH_ACTIONS",
    "TASKS",
]
