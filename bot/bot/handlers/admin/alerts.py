"""
Larizinha Store — Módulo Admin: Central de Alertas
Implementa a central de alertas conforme DIM.

Telas:
- Dashboard de alertas (por categoria: pagamentos, saques, bot, integração, estoque, crescimento, financeiro, segurança)
- Lista de alertas ativos (com paginação DIM)
- Lista de alertas resolvidos
- Detalhes do alerta (com ação de resolver)
- Resolver individual
- Resolver todos por categoria
- Histórico completo
- Estatísticas

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel.
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from bot.database import Alert, Product, session_scope
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
router = Router(name="admin_alerts")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_DASH = "admin_alerts_dashboard"
MSG_KEY_LIST = "admin_alerts_list"
MSG_KEY_EMPTY = "admin_alerts_empty"
MSG_KEY_DETAIL = "admin_alerts_detail"
MSG_KEY_STATS = "admin_alerts_stats"

SCREEN_DASH = "admin_alerts_dashboard"
SCREEN_LIST = "admin_alerts_list"

# Callbacks base
CB_ROOT = "admin:alerts"
CB_PAGE = "admin:alerts:page"               # :cat:status:N
CB_CATEGORY = "admin:alerts:cat"            # :key
CB_STATUS = "admin:alerts:status"           # :open|resolved
CB_VIEW = "admin:alerts:view"               # :ID
CB_RESOLVE = "admin:alerts:resolve"         # :ID
CB_RESOLVE_YES = "admin:alerts:resolve:yes" # :ID
CB_RESOLVE_ALL = "admin:alerts:resolveall"  # :category
CB_RESOLVE_ALL_YES = "admin:alerts:resolveall:yes"  # :category
CB_STATS = "admin:alerts:stats"
CB_RELOAD = "admin:alerts:reload"

# Categorias
CATEGORIES: Dict[str, str] = {
    "all": "📋 Todas",
    "payment": "💳 Pagamentos",
    "withdraw": "💸 Saques",
    "bot": "🤖 Bot",
    "integration": "🔌 Integração",
    "stock": "📦 Estoque",
    "growth": "👥 Crescimento",
    "finance": "💰 Financeiro",
    "security": "🛡️ Segurança",
}

# Severidades
SEV_EMOJI: Dict[str, str] = {
    "info": "🔵",
    "warning": "🟡",
    "error": "🔴",
    "critical": "🚨",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _sev_emoji(sev: Any) -> str:
    s = str(sev or "info").lower()
    return SEV_EMOJI.get(s, "⚪️")


def _cat_emoji(cat: Any) -> str:
    s = str(cat or "").lower()
    mapping = {
        "payment": "💳",
        "withdraw": "💸",
        "bot": "🤖",
        "integration": "🔌",
        "stock": "📦",
        "growth": "👥",
        "finance": "💰",
        "security": "🛡️",
    }
    return mapping.get(s, "⚠️")


# ============================================================
# DASHBOARD
# ============================================================
async def _collect_dashboard() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    out: Dict[str, Any] = {
        "total_open": 0,
        "total_resolved": 0,
        "today": 0,
        "critical": 0,
        "by_category": [],
    }

    try:
        async with session_scope() as session:
            out["total_open"] = int(
                (
                    await session.execute(
                        select(func.count(Alert.id)).where(
                            Alert.resolved == False  # noqa: E712
                        )
                    )
                ).scalar()
                or 0
            )
            out["total_resolved"] = int(
                (
                    await session.execute(
                        select(func.count(Alert.id)).where(
                            Alert.resolved == True  # noqa: E712
                        )
                    )
                ).scalar()
                or 0
            )
            out["today"] = int(
                (
                    await session.execute(
                        select(func.count(Alert.id)).where(
                            Alert.created_at >= start_today
                        )
                    )
                ).scalar()
                or 0
            )
            out["critical"] = int(
                (
                    await session.execute(
                        select(func.count(Alert.id))
                        .where(Alert.resolved == False)  # noqa: E712
                        .where(Alert.severity.in_(("error", "critical")))
                    )
                ).scalar()
                or 0
            )

            # Conta por categoria (só as abertas)
            res = await session.execute(
                select(Alert.category, func.count(Alert.id))
                .where(Alert.resolved == False)  # noqa: E712
                .group_by(Alert.category)
                .order_by(func.count(Alert.id).desc())
            )
            out["by_category"] = [(str(c), int(n or 0)) for c, n in res.all()]
    except Exception as exc:
        logger.debug(f"_collect_dashboard falhou: {exc}")

    return out


async def render_dashboard(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    stats = await _collect_dashboard()

    # Categorias conhecidas
    known_cats = [
        "payment", "withdraw", "bot", "integration",
        "stock", "growth", "finance", "security",
    ]

    cat_map = {c: n for c, n in stats["by_category"]}

    lines = ["📊 <b>CENTRAL DE ALERTAS</b>", ""]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"├ 🟡 Abertos: <b>{fmt_num(stats['total_open'])}</b>")
    lines.append(f"├ ✅ Resolvidos: <b>{fmt_num(stats['total_resolved'])}</b>")
    lines.append(f"├ 📅 Hoje: <b>{fmt_num(stats['today'])}</b>")
    lines.append(f"└ 🚨 Críticos: <b>{fmt_num(stats['critical'])}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏷️ <b>Por categoria (abertos):</b>")

    if not stats["by_category"]:
        lines.append("<i>Nenhum alerta aberto.</i>")
    else:
        for cat in known_cats:
            count = cat_map.get(cat, 0)
            emoji = _cat_emoji(cat)
            label = CATEGORIES.get(cat, cat)
            lines.append(f"{emoji} {label}: <b>{fmt_num(count)}</b>")

    text = "\n".join(lines)

    rendered = await message_engine.render(
        MSG_KEY_DASH,
        fallback=text,
        total=fmt_num(stats["total_open"]),
        resolvidos=fmt_num(stats["total_resolved"]),
        hoje=fmt_num(stats["today"]),
        criticos=fmt_num(stats["critical"]),
        lista=text,
    )

    engine_kb = await button_engine.inline(SCREEN_DASH)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="💳 Pagamentos", callback_data=f"{CB_CATEGORY}:payment"),
                    InlineKeyboardButton(text="💸 Saques", callback_data=f"{CB_CATEGORY}:withdraw"),
                ],
                [
                    InlineKeyboardButton(text="🤖 Bot", callback_data=f"{CB_CATEGORY}:bot"),
                    InlineKeyboardButton(text="🔌 Integração", callback_data=f"{CB_CATEGORY}:integration"),
                ],
                [
                    InlineKeyboardButton(text="📦 Estoque Baixo", callback_data=f"{CB_CATEGORY}:stock"),
                    InlineKeyboardButton(text="👥 Crescimento", callback_data=f"{CB_CATEGORY}:growth"),
                ],
                [
                    InlineKeyboardButton(text="💰 Financeiro", callback_data=f"{CB_CATEGORY}:finance"),
                    InlineKeyboardButton(text="🛡️ Segurança", callback_data=f"{CB_CATEGORY}:security"),
                ],
                [
                    InlineKeyboardButton(text="📜 Histórico", callback_data=f"{CB_STATUS}:resolved"),
                    InlineKeyboardButton(text="📊 Estatísticas", callback_data=CB_STATS),
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
        await render_dashboard(callback)
        if callback.data == CB_RELOAD:
            await toast_ok(callback, "🔄 Atualizado!")
        else:
            await answer(callback)
    except Exception as exc:
        logger.exception(f"alerts on_root falhou: {exc}")


# ============================================================
# LISTA
# ============================================================
async def _count_alerts(category: str = "all", status: str = "open") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Alert.id))
            if category and category != "all":
                stmt = stmt.where(Alert.category == category)
            if status == "open":
                stmt = stmt.where(Alert.resolved == False)  # noqa: E712
            elif status == "resolved":
                stmt = stmt.where(Alert.resolved == True)  # noqa: E712
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_alerts(
    page: int,
    category: str = "all",
    status: str = "open",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Alert]:
    try:
        async with session_scope() as session:
            stmt = select(Alert).order_by(Alert.created_at.desc())
            if category and category != "all":
                stmt = stmt.where(Alert.category == category)
            if status == "open":
                stmt = stmt.where(Alert.resolved == False)  # noqa: E712
            elif status == "resolved":
                stmt = stmt.where(Alert.resolved == True)  # noqa: E712
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    category: str = "all",
    status: str = "open",
) -> Optional[Message]:
    total = await _count_alerts(category, status)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    items = await _list_alerts(page, category, status)

    header_icon = "🟡 Abertos" if status == "open" else "✅ Resolvidos"
    cat_label = CATEGORIES.get(category, category)

    if not items:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                f"⚠️ <b>ALERTAS · {header_icon}</b>\n"
                f"🏷️ {cat_label}\n\n"
                "<i>Nenhum alerta encontrado.</i>"
            ),
            status=header_icon,
            categoria=cat_label,
        )
    else:
        rows: List[List[str]] = []
        for a in items:
            sev_em = _sev_emoji(a.severity)
            cat_em = _cat_emoji(a.category)
            rows.append(
                [
                    str(a.id),
                    f"{cat_em}{sev_em}",
                    truncate(a.title or "—", 24),
                    fmt_dt(a.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "", "Título", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"⚠️ <b>ALERTAS · {header_icon}</b> ({total})\n"
                f"🏷️ {cat_label}\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
            status=header_icon,
            categoria=cat_label,
        )

    rows_kb: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🟡 Abertos", callback_data=f"{CB_STATUS}:open"
            ),
            InlineKeyboardButton(
                text="✅ Resolvidos", callback_data=f"{CB_STATUS}:resolved"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📋 Todas", callback_data=f"{CB_CATEGORY}:all"
            ),
            InlineKeyboardButton(
                text="💳 Pagamentos", callback_data=f"{CB_CATEGORY}:payment"
            ),
        ],
        [
            InlineKeyboardButton(
                text="💸 Saques", callback_data=f"{CB_CATEGORY}:withdraw"
            ),
            InlineKeyboardButton(
                text="📦 Estoque", callback_data=f"{CB_CATEGORY}:stock"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🛡️ Segurança", callback_data=f"{CB_CATEGORY}:security"
            ),
            InlineKeyboardButton(
                text="💰 Financeiro", callback_data=f"{CB_CATEGORY}:finance"
            ),
        ],
    ]

    # Botões por alerta
    for a in items[:8]:
        sev_em = _sev_emoji(a.severity)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{sev_em} #{a.id} {truncate(a.title or '—', 24)}",
                    callback_data=f"{CB_VIEW}:{a.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{category}:{status}",
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    # Resolver todos (só se for open e categoria específica)
    if status == "open" and category != "all" and total > 0:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Resolver todos ({total})",
                    callback_data=f"{CB_RESOLVE_ALL}:{category}",
                )
            ]
        )

    rows_kb.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
    )

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # admin:alerts:page:cat:status:N
        parts = callback.data.split(":")
        category = "all"
        status = "open"
        page = 1
        if len(parts) >= 4:
            category = parts[3]
        if len(parts) >= 5:
            status = parts[4]
        if len(parts) >= 6:
            try:
                page = int(parts[5])
            except Exception:
                page = 1

        if category not in CATEGORIES:
            category = "all"
        if status not in ("open", "resolved"):
            status = "open"

        await render_list(callback, page=page, category=category, status=status)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"alerts on_page falhou: {exc}")


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
        await render_list(callback, page=1, category=cat, status="open")
        await toast_ok(callback, f"{CATEGORIES[cat]} selecionado.")
    except Exception as exc:
        logger.exception(f"alerts on_category falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_STATUS}:"))
async def on_status(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            status = callback.data.split(":")[-1]
        except Exception:
            status = "open"
        if status not in ("open", "resolved"):
            status = "open"
        await render_list(callback, page=1, category="all", status=status)
        await toast_ok(callback, f"🔄 {'Abertos' if status == 'open' else 'Resolvidos'}")
    except Exception as exc:
        logger.exception(f"alerts on_status falhou: {exc}")


# ============================================================
# DETALHE
# ============================================================
async def _load_alert(aid: int) -> Optional[Alert]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Alert).where(Alert.id == aid))
            return res.scalar_one_or_none()
    except Exception:
        return None


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            aid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        a = await _load_alert(aid)
        if a is None:
            await toast_err(callback, "❌ Alerta não encontrado.")
            return

        sev_em = _sev_emoji(a.severity)
        cat_em = _cat_emoji(a.category)

        resolvido_txt = (
            f"✅ Resolvido em {fmt_dt(a.resolved_at)}" if a.resolved else "🟡 Aberto"
        )

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"{cat_em}{sev_em} <b>ALERTA #{a.id}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"├ Categoria: <code>{a.category or '—'}</code>\n"
                f"├ Severidade: {a.severity or 'info'}\n"
                f"├ Título: <b>{a.title}</b>\n"
                f"├ Status: {resolvido_txt}\n"
                f"└ Criado: {fmt_dt(a.created_at)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📄 <b>Mensagem:</b>\n"
                f"{a.message}"
            ),
            id=a.id,
            categoria=a.category or "—",
            severidade=a.severity or "info",
            titulo=a.title or "—",
            mensagem=a.message or "—",
            status=resolvido_txt,
            data=fmt_dt(a.created_at),
            resolved=fmt_dt(a.resolved_at),
            created=fmt_dt(a.created_at),
        )

        rows_kb: List[List[InlineKeyboardButton]] = []

        if not a.resolved:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="✅ Marcar como resolvido",
                        callback_data=f"{CB_RESOLVE}:{a.id}",
                    )
                ]
            )

        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=f"{CB_PAGE}:all:open:1",
                )
            ]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"alerts on_view falhou: {exc}")


# ============================================================
# RESOLVER INDIVIDUAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESOLVE}:"))
async def on_resolve(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            aid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        a = await _load_alert(aid)
        if a is None:
            await toast_err(callback, "❌ Alerta não encontrado.")
            return

        if a.resolved:
            await toast_err(callback, "✅ Este alerta já foi resolvido.")
            return

        text = (
            f"✅ <b>RESOLVER ALERTA #{aid}</b>\n\n"
            f"📌 <b>{a.title}</b>\n\n"
            "Marcar como resolvido?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RESOLVE_YES}:{aid}",
            cancel_callback=f"{CB_VIEW}:{aid}",
            confirm_text="✅ Sim, resolver",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"alerts on_resolve falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RESOLVE_YES}:"))
async def on_resolve_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            aid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(select(Alert).where(Alert.id == aid))
                a = res.scalar_one_or_none()
                if a is None:
                    await toast_err(callback, "❌ Alerta não encontrado.")
                    return
                a.resolved = True
                a.resolved_at = datetime.now(timezone.utc)
                session.add(a)
        except Exception as exc:
            logger.exception(f"resolve alert falhou: {exc}")
            await toast_err(callback, "❌ Erro ao resolver.")
            return

        await log_action(
            admin_id=admin_id,
            action="alert.resolved",
            category="admin",
            details={"alert_id": aid},
        )

        await render_dashboard(callback)
        await toast_ok(callback, "✅ Resolvido.")
    except Exception as exc:
        logger.exception(f"alerts on_resolve_confirm falhou: {exc}")


# ============================================================
# RESOLVER TODOS (por categoria)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESOLVE_ALL}:"))
async def on_resolve_all(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        if len(parts) >= 6 and parts[5] == "yes":
            return
        try:
            category = parts[3]
        except Exception:
            await toast_err(callback, "❌ Categoria inválida.")
            return

        count = await _count_alerts(category, "open")

        text = (
            f"✅ <b>RESOLVER TODOS</b>\n\n"
            f"🏷️ Categoria: {CATEGORIES.get(category, category)}\n"
            f"📊 Abertos: <b>{fmt_num(count)}</b>\n\n"
            "Todos serão marcados como resolvidos.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RESOLVE_ALL_YES}:{category}",
            cancel_callback=CB_ROOT,
            confirm_text="✅ Sim, resolver todos",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"alerts on_resolve_all falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RESOLVE_ALL_YES}:"))
async def on_resolve_all_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:alerts:resolveall:yes:cat
        try:
            category = parts[-1]
        except Exception:
            await toast_err(callback, "❌ Categoria inválida.")
            return

        now = datetime.now(timezone.utc)
        resolved = 0
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Alert)
                    .where(Alert.category == category)
                    .where(Alert.resolved == False)  # noqa: E712
                )
                items = list(res.scalars().all())
                for a in items:
                    a.resolved = True
                    a.resolved_at = now
                    session.add(a)
                    resolved += 1
        except Exception as exc:
            logger.exception(f"resolve all falhou: {exc}")
            await toast_err(callback, "❌ Erro ao resolver.")
            return

        await log_action(
            admin_id=admin_id,
            action="alert.resolved_all",
            category="admin",
            details={"category": category, "count": resolved},
        )

        await render_dashboard(callback)
        await toast_ok(callback, f"✅ {resolved} alerta(s) resolvido(s).")
    except Exception as exc:
        logger.exception(f"alerts on_resolve_all_confirm falhou: {exc}")


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

        now = datetime.now(timezone.utc)
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        try:
            async with session_scope() as session:
                total = int(
                    (await session.execute(select(func.count(Alert.id)))).scalar() or 0
                )
                last7 = int(
                    (
                        await session.execute(
                            select(func.count(Alert.id)).where(
                                Alert.created_at >= since_7d
                            )
                        )
                    ).scalar()
                    or 0
                )
                last30 = int(
                    (
                        await session.execute(
                            select(func.count(Alert.id)).where(
                                Alert.created_at >= since_30d
                            )
                        )
                    ).scalar()
                    or 0
                )
                by_sev = (
                    await session.execute(
                        select(Alert.severity, func.count(Alert.id))
                        .group_by(Alert.severity)
                    )
                ).all()
                by_cat = (
                    await session.execute(
                        select(Alert.category, func.count(Alert.id))
                        .group_by(Alert.category)
                        .order_by(func.count(Alert.id).desc())
                        .limit(15)
                    )
                ).all()
        except Exception:
            total = last7 = last30 = 0
            by_sev = []
            by_cat = []

        lines = [
            "📊 <b>ESTATÍSTICAS DE ALERTAS</b>",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"📋 Total: <b>{fmt_num(total)}</b>",
            f"📅 Últimos 7d: <b>{fmt_num(last7)}</b>",
            f"📅 Últimos 30d: <b>{fmt_num(last30)}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🚨 <b>Por severidade:</b>",
        ]

        if not by_sev:
            lines.append("<i>Sem dados.</i>")
        else:
            for sev, count in by_sev:
                em = _sev_emoji(sev)
                lines.append(f"{em} {sev}: <b>{fmt_num(count)}</b>")

        lines.append("")
        lines.append("🏷️ <b>Top categorias:</b>")
        if not by_cat:
            lines.append("<i>Sem dados.</i>")
        else:
            for cat, count in by_cat[:10]:
                em = _cat_emoji(cat)
                lines.append(f"{em} {cat}: <b>{fmt_num(count)}</b>")

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
        logger.exception(f"alerts on_stats falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_dashboard",
    "render_list",
    "on_root",
    "on_page",
    "on_category",
    "on_status",
    "on_view",
    "on_resolve",
    "on_resolve_confirm",
    "on_resolve_all",
    "on_resolve_all_confirm",
    "on_stats",
    "CB_ROOT",
    "CB_PAGE",
    "CB_CATEGORY",
    "CB_STATUS",
    "CB_VIEW",
    "CB_RESOLVE",
    "CB_RESOLVE_YES",
    "CB_RESOLVE_ALL",
    "CB_RESOLVE_ALL_YES",
    "CB_STATS",
    "CB_RELOAD",
    "CATEGORIES",
    "SEV_EMOJI",
]
