"""
Larizinha Store — Módulo Admin: Logs e Auditoria
Implementa a auditoria completa conforme DIM.

Telas:
- Lista de logs (com filtros por categoria + paginação DIM)
- Filtros: bot, painel, financeiro, pagamentos, saques, admins, erros, alterações
- Filtro por data (hoje/7d/30d/personalizado)
- Buscar por admin / ação / detalhe
- Ver detalhes de um log (JSON + metadados)
- Exportação CSV
- Limpeza de logs antigos (por retenção)
- Estatísticas de logs

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
from bot.database import Log, Setting, session_scope
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_DONE,
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
    fmt_dt,
    fmt_num,
    is_date,
    kb,
    log_action,
    page_offset,
    parse_date,
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
router = Router(name="admin_logs")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_logs_list"
MSG_KEY_EMPTY = "admin_logs_empty"
MSG_KEY_DETAIL = "admin_logs_detail"
MSG_KEY_STATS = "admin_logs_stats"

SCREEN_LIST = "admin_logs_list"
SCREEN_DETAIL = "admin_logs_detail"

# Callbacks base
CB_ROOT = "admin:logs"
CB_PAGE = "admin:logs:page"                # :cat:period:N
CB_CATEGORY = "admin:logs:cat"             # :key
CB_PERIOD = "admin:logs:period"            # :key
CB_SEARCH = "admin:logs:search"
CB_VIEW = "admin:logs:view"                # :ID
CB_EXPORT = "admin:logs:export"
CB_CLEAN = "admin:logs:clean"
CB_CLEAN_YES = "admin:logs:clean:yes"
CB_STATS = "admin:logs:stats"

# Categorias conhecidas
CATEGORIES: Dict[str, str] = {
    "all": "📋 Todas",
    "admin": "🛡️ Painel",
    "payments": "💳 Pagamentos",
    "finance": "💰 Financeiro",
    "withdraws": "💸 Saques",
    "security": "🔐 Segurança",
    "bot": "🤖 Bot",
    "error": "⚠️ Erros",
    "audit": "📝 Alterações",
}

# Períodos
PERIODS: Dict[str, str] = {
    "all": "📋 Tudo",
    "today": "📅 Hoje",
    "7d": "📅 7 dias",
    "30d": "📅 30 dias",
    "custom": "📅 Personalizado",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _cat_emoji(cat: Any) -> str:
    s = str(cat or "").lower()
    return {
        "admin": "🛡️",
        "payments": "💳",
        "finance": "💰",
        "withdraws": "💸",
        "security": "🔐",
        "bot": "🤖",
        "error": "⚠️",
        "audit": "📝",
    }.get(s, "🔹")


def _period_range(period: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, None
    if period == "7d":
        return now - timedelta(days=7), None
    if period == "30d":
        return now - timedelta(days=30), None
    return None, None


def _apply_filters(stmt, category: str = "all", start=None, end=None):
    if category and category != "all":
        stmt = stmt.where(Log.category == category)
    if start is not None:
        stmt = stmt.where(Log.created_at >= start)
    if end is not None:
        stmt = stmt.where(Log.created_at <= end)
    return stmt


async def _count_logs(category: str = "all", period: str = "all") -> int:
    start, end = _period_range(period)
    try:
        async with session_scope() as session:
            stmt = select(func.count(Log.id))
            stmt = _apply_filters(stmt, category, start, end)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_logs(
    page: int,
    category: str = "all",
    period: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Log]:
    start, end = _period_range(period)
    try:
        async with session_scope() as session:
            stmt = select(Log).order_by(Log.created_at.desc())
            stmt = _apply_filters(stmt, category, start, end)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_log(lid: int) -> Optional[Log]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Log).where(Log.id == lid))
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


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    category: str = "all",
    period: str = "all",
) -> Optional[Message]:
    total = await _count_logs(category, period)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    items = await _list_logs(page, category, period)

    if not items:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🧾 <b>LOGS E AUDITORIA</b>\n\n"
                "<i>Nenhum log encontrado com estes filtros.</i>"
            ),
            categoria=CATEGORIES.get(category, category),
            periodo=PERIODS.get(period, period),
        )
    else:
        rows: List[Sequence[str]] = []
        for log in items:
            emoji = _cat_emoji(log.category)
            rows.append(
                [
                    str(log.id),
                    emoji,
                    truncate(log.action or "—", 22),
                    str(log.admin_id or "—"),
                    str(log.user_id or "—"),
                    fmt_dt(log.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "", "Ação", "Admin", "User", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🧾 <b>LOGS E AUDITORIA</b> ({total})\n"
                f"🏷️ Categoria: {CATEGORIES.get(category, category)}\n"
                f"📅 Período: {PERIODS.get(period, period)}\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
            categoria=CATEGORIES.get(category, category),
            periodo=PERIODS.get(period, period),
        )

    # ---------- Botões ----------
    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="🤖 Bot", callback_data=f"{CB_CATEGORY}:bot"),
                InlineKeyboardButton(text="🛡️ Painel", callback_data=f"{CB_CATEGORY}:admin"),
            ],
            [
                InlineKeyboardButton(text="💰 Financeiro", callback_data=f"{CB_CATEGORY}:finance"),
                InlineKeyboardButton(text="💳 Pagamentos", callback_data=f"{CB_CATEGORY}:payments"),
            ],
            [
                InlineKeyboardButton(text="💸 Saques", callback_data=f"{CB_CATEGORY}:withdraws"),
                InlineKeyboardButton(text="🔐 Segurança", callback_data=f"{CB_CATEGORY}:security"),
            ],
            [
                InlineKeyboardButton(text="⚠️ Erros", callback_data=f"{CB_CATEGORY}:error"),
                InlineKeyboardButton(text="📝 Alterações", callback_data=f"{CB_CATEGORY}:audit"),
            ],
            [
                InlineKeyboardButton(text="📅 Hoje", callback_data=f"{CB_PERIOD}:today"),
                InlineKeyboardButton(text="📅 7d", callback_data=f"{CB_PERIOD}:7d"),
                InlineKeyboardButton(text="📅 30d", callback_data=f"{CB_PERIOD}:30d"),
            ],
            [
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_CATEGORY}:all"),
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
            ],
            [
                InlineKeyboardButton(text="📊 Estatísticas", callback_data=CB_STATS),
                InlineKeyboardButton(text="📥 Exportar", callback_data=CB_EXPORT),
            ],
            [
                InlineKeyboardButton(text="🧹 Limpar Antigos", callback_data=CB_CLEAN),
            ],
        ]

    # ---------- Últimos 8 logs como botões ----------
    for log in items[:8]:
        emoji = _cat_emoji(log.category)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{log.id} · {truncate(log.action or '—', 30)}",
                    callback_data=f"{CB_VIEW}:{log.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{category}:{period}",
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
        await render_list(callback, page=1, category="all", period="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # admin:logs:page:CAT:PERIOD:N
        parts = callback.data.split(":")
        category = "all"
        period = "all"
        page = 1
        if len(parts) >= 4:
            category = parts[3]
        if len(parts) >= 5:
            period = parts[4]
        if len(parts) >= 6:
            try:
                page = int(parts[5])
            except Exception:
                page = 1
        if category not in CATEGORIES:
            category = "all"
        if period not in PERIODS:
            period = "all"
        await render_list(callback, page=page, category=category, period=period)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_page falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CATEGORY}:"))
async def on_category(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cat = callback.data.split(":")[-1]
        except Exception:
            cat = "all"
        if cat not in CATEGORIES:
            cat = "all"
        await render_list(callback, page=1, category=cat, period="all")
        await toast_ok(callback, f"{CATEGORIES[cat]} selecionado.")
    except Exception as exc:
        logger.exception(f"logs on_category falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PERIOD}:"))
async def on_period(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            period = callback.data.split(":")[-1]
        except Exception:
            period = "all"
        if period not in PERIODS:
            period = "all"
        await render_list(callback, page=1, category="all", period=period)
        await toast_ok(callback, f"{PERIODS[period]} selecionado.")
    except Exception as exc:
        logger.exception(f"logs on_period falhou: {exc}")


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
            "🔍 <b>BUSCAR NOS LOGS</b>\n\n"
            "Envie um termo para buscar em: ação, admin_id ou user_id.\n"
            "Ex.: <code>product.set</code> ou <code>123456789</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="logs_search_query",
            screen="logs:search",
            use_force_reply=True,
            reply_placeholder="Termo de busca",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_logs_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting == "logs_search_query":
        value, _extra = await consume_input(message, admin_id, expected_field="logs_search_query")
        if value is None:
            return
        await _do_search(message, admin_id, value)
        return

    # Período personalizado
    if awaiting == "logs_custom_start":
        value, _extra = await consume_input(message, admin_id, expected_field="logs_custom_start")
        if value is None:
            return
        dt = parse_date(value)
        if dt is None:
            await message.answer("❌ Data inválida. Use DD/MM/AAAA.")
            return
        await AdminFlow.update(admin_id, {"logs_start": dt.isoformat()})
        prompt = (
            "📅 <b>PERÍODO PERSONALIZADO</b>\n\n"
            "Agora envie a data final (<code>DD/MM/AAAA</code>).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field="logs_custom_end",
            screen="logs:custom",
            use_force_reply=True,
            reply_placeholder="DD/MM/AAAA",
        )
        return

    if awaiting == "logs_custom_end":
        value, _extra = await consume_input(message, admin_id, expected_field="logs_custom_end")
        if value is None:
            return
        dt_end = parse_date(value)
        if dt_end is None:
            await message.answer("❌ Data inválida. Use DD/MM/AAAA.")
            return
        dt_end = dt_end.replace(hour=23, minute=59, second=59)

        state2 = await AdminFlow.get(admin_id)
        data2 = (state2 or {}).get("data", {}) if state2 else {}
        start_str = data2.get("logs_start")
        if not start_str:
            await message.answer("❌ Fluxo incompleto.")
            await AdminFlow.clear(admin_id)
            return

        try:
            dt_start = datetime.fromisoformat(start_str)
        except Exception:
            await message.answer("❌ Data inválida.")
            await AdminFlow.clear(admin_id)
            return

        await AdminFlow.clear(admin_id)
        await _render_custom_range(message, admin_id, dt_start, dt_end)
        return


async def _do_search(message: Message, admin_id: int, query: str) -> None:
    query = (query or "").strip()
    if not query:
        await message.answer("❌ Busca vazia.")
        return

    like = f"%{query.lower()}%"
    try:
        async with session_scope() as session:
            stmt = (
                select(Log)
                .where(
                    or_(
                        func.lower(func.coalesce(Log.action, "")).like(like),
                        func.cast(Log.admin_id, func.TEXT).like(f"%{query}%"),
                        func.cast(Log.user_id, func.TEXT).like(f"%{query}%"),
                    )
                )
                .order_by(Log.created_at.desc())
                .limit(30)
            )
            res = await session.execute(stmt)
            items = list(res.scalars().all())
    except Exception:
        items = []

    if not items:
        await message.answer(
            text="🔍 <b>NENHUM RESULTADO</b>",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    lines = [f"🔍 <b>RESULTADOS ({len(items)})</b>", ""]
    rows_kb: List[List[InlineKeyboardButton]] = []
    for log in items:
        emoji = _cat_emoji(log.category)
        lines.append(
            f"{emoji} #{log.id} · <code>{truncate(log.action or '—', 40)}</code>"
        )
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{log.id} {truncate(log.action or '—', 20)}",
                    callback_data=f"{CB_VIEW}:{log.id}",
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


async def _render_custom_range(
    message: Message,
    admin_id: int,
    start: datetime,
    end: datetime,
) -> None:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Log)
                .where(Log.created_at >= start)
                .where(Log.created_at <= end)
                .order_by(Log.created_at.desc())
                .limit(50)
            )
            items = list(res.scalars().all())

            total = int(
                (
                    await session.execute(
                        select(func.count(Log.id))
                        .where(Log.created_at >= start)
                        .where(Log.created_at <= end)
                    )
                ).scalar()
                or 0
            )
    except Exception:
        items = []
        total = 0

    text = (
        f"📅 <b>LOGS · PERÍODO PERSONALIZADO</b>\n"
        f"🕐 {fmt_dt(start, with_time=False)} → {fmt_dt(end, with_time=False)}\n"
        f"📊 Total: <b>{fmt_num(total)}</b>\n\n"
    )

    rows_kb: List[List[InlineKeyboardButton]] = []
    if not items:
        text += "<i>Nenhum log neste período.</i>"
    else:
        for log in items[:25]:
            emoji = _cat_emoji(log.category)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{emoji} #{log.id} · {truncate(log.action or '—', 26)}",
                        callback_data=f"{CB_VIEW}:{log.id}",
                    )
                ]
            )

    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# DETALHE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            lid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        log = await _load_log(lid)
        if log is None:
            await toast_err(callback, "❌ Log não encontrado.")
            return

        details = log.details or {}
        try:
            details_str = json.dumps(details, indent=2, default=str, ensure_ascii=False)
        except Exception:
            details_str = str(details)

        emoji = _cat_emoji(log.category)

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"{emoji} <b>LOG #{log.id}</b>\n\n"
                f"├ Categoria: <code>{log.category or '—'}</code>\n"
                f"├ Ação: <code>{log.action or '—'}</code>\n"
                f"├ Admin: <code>{log.admin_id or '—'}</code>\n"
                f"├ Usuário: <code>{log.user_id or '—'}</code>\n"
                f"├ IP: <code>{log.ip or '—'}</code>\n"
                f"└ Data: {fmt_dt(log.created_at)}\n\n"
                "📄 <b>Detalhes:</b>\n"
                f"<code>{truncate(details_str, 800)}</code>"
            ),
            id=log.id,
            categoria=log.category or "—",
            acao=log.action or "—",
            admin=log.admin_id or "—",
            user=log.user_id or "—",
            ip=log.ip or "—",
            data=fmt_dt(log.created_at),
            detalhes=truncate(details_str, 800),
        )

        await edit_or_send(
            callback,
            text=rendered.content,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_view falhou: {exc}")


# ============================================================
# ESTATÍSTICAS
# ============================================================
@router.callback_query(F.data == CB_STATS)
async def on_stats(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Conta logs por categoria
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Log.category, func.count(Log.id))
                    .group_by(Log.category)
                    .order_by(func.count(Log.id).desc())
                )
                cat_rows = res.all()

                # Total nos últimos 30d
                since = datetime.now(timezone.utc) - timedelta(days=30)
                total_30d = int(
                    (
                        await session.execute(
                            select(func.count(Log.id)).where(Log.created_at >= since)
                        )
                    ).scalar()
                    or 0
                )

                total_all = int(
                    (await session.execute(select(func.count(Log.id)))).scalar() or 0
                )
        except Exception:
            cat_rows = []
            total_30d = total_all = 0

        lines = [
            "📊 <b>ESTATÍSTICAS DE LOGS</b>",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📋 Total geral: <b>{fmt_num(total_all)}</b>",
            f"📅 Últimos 30 dias: <b>{fmt_num(total_30d)}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🏷️ <b>Por categoria:</b>",
        ]

        for cat, count in cat_rows[:15]:
            emoji = _cat_emoji(cat)
            lines.append(f"{emoji} {cat or '—'}: <b>{fmt_num(count)}</b>")

        text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_stats falhou: {exc}")


# ============================================================
# EXPORTAR CSV
# ============================================================
@router.callback_query(F.data == CB_EXPORT)
async def on_export(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Exporta últimos 5000
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Log)
                    .order_by(Log.created_at.desc())
                    .limit(5000)
                )
                items = list(res.scalars().all())
        except Exception:
            items = []

        if not items:
            await toast_err(callback, "❌ Sem logs para exportar.")
            return

        headers = ["ID", "Categoria", "Ação", "Admin ID", "User ID", "IP", "Data", "Detalhes"]
        rows: List[List[Any]] = []
        for log in items:
            try:
                det = json.dumps(log.details or {}, default=str, ensure_ascii=False)
            except Exception:
                det = str(log.details)
            rows.append(
                [
                    log.id,
                    log.category or "",
                    log.action or "",
                    log.admin_id or "",
                    log.user_id or "",
                    log.ip or "",
                    fmt_dt(log.created_at),
                    det,
                ]
            )

        filename = f"logs_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📥 <b>Logs</b> ({len(rows)})",
        )

        if ok:
            await toast_ok(callback, "📥 CSV enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action="logs.export_csv",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"logs on_export falhou: {exc}")


# ============================================================
# LIMPAR ANTIGOS
# ============================================================
@router.callback_query(F.data == CB_CLEAN)
async def on_clean(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 4 and parts[3] == "yes":
            return

        # Retenção
        retention = await _get_setting("logs_retention_days", "90")
        try:
            retention_days = int(retention or 90)
        except Exception:
            retention_days = 90

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        try:
            async with session_scope() as session:
                total = int(
                    (
                        await session.execute(
                            select(func.count(Log.id)).where(Log.created_at < cutoff)
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total = 0

        text = (
            f"🧹 <b>LIMPAR LOGS ANTIGOS</b>\n\n"
            f"📅 Retenção: <b>{retention_days} dias</b>\n"
            f"🗑️ Logs a remover: <b>{fmt_num(total)}</b>\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CLEAN_YES}:yes",
            cancel_callback=CB_ROOT,
            confirm_text="🧹 Sim, limpar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"logs on_clean falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CLEAN_YES}:"))
async def on_clean_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        retention = await _get_setting("logs_retention_days", "90")
        try:
            retention_days = int(retention or 90)
        except Exception:
            retention_days = 90

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        removed = 0
        try:
            async with session_scope() as session:
                # Conta antes
                res = await session.execute(
                    select(func.count(Log.id)).where(Log.created_at < cutoff)
                )
                removed = int(res.scalar() or 0)

                # Apaga
                await session.execute(
                    Log.__table__.delete().where(Log.created_at < cutoff)
                )
        except Exception as exc:
            logger.exception(f"clean logs falhou: {exc}")
            await toast_err(callback, "❌ Erro ao limpar.")
            return

        await log_action(
            admin_id=admin_id,
            action="logs.cleaned",
            category="admin",
            details={"removed": removed, "retention_days": retention_days},
        )

        await render_list(callback, page=1, category="all", period="all")
        await toast_ok(callback, f"🧹 {removed} log(s) removido(s).")
    except Exception as exc:
        logger.exception(f"logs on_clean_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_category",
    "on_period",
    "on_search",
    "on_logs_text_input",
    "on_view",
    "on_stats",
    "on_export",
    "on_clean",
    "on_clean_confirm",
    "CB_ROOT",
    "CB_PAGE",
    "CB_CATEGORY",
    "CB_PERIOD",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_EXPORT",
    "CB_CLEAN",
    "CB_CLEAN_YES",
    "CB_STATS",
    "CATEGORIES",
    "PERIODS",
]
