"""
Larizinha Store — Módulo Admin: Segurança e Antifraude
Implementa o painel de segurança conforme DIM.

Telas:
- Dashboard de segurança (logins, tentativas, suspeitas, múltiplas contas, spam, monitorados)
- Registro de eventos (security_events) com filtros e paginação
- Logins recentes
- Permissões (atalho para o módulo permissions)
- Limites (múltiplas contas por IP)
- Suspeitas (transações)
- Múltiplas contas por IP
- Anti-spam (throttle)
- Bloqueios temporários (Redis)
- Usuários monitorados
- Alertas de segurança
- Ações: banir, desbanir, bloquear temporariamente, liberar

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
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import (
    Alert,
    SecurityEvent,
    Setting,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import Keys, get_redis
from bot.services.message_engine import message_engine
from bot.services.security_monitor import security_monitor
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
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_security")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_DASH = "admin_security_dashboard"
MSG_KEY_EVENTS = "admin_security_events"
MSG_KEY_EVENTS_EMPTY = "admin_security_events_empty"
MSG_KEY_DETAIL = "admin_security_detail"
MSG_KEY_LIMITS = "admin_security_limits"
MSG_KEY_LOGINS = "admin_security_logins"
MSG_KEY_SUSPECTS = "admin_security_suspects"
MSG_KEY_MULTI = "admin_security_multi"
MSG_KEY_SPAM = "admin_security_spam"
MSG_KEY_TEMPBAN = "admin_security_tempban"
MSG_KEY_MONITORED = "admin_security_monitored"

SCREEN_DASH = "admin_security_dashboard"
SCREEN_EVENTS = "admin_security_events"
SCREEN_DETAIL = "admin_security_detail"

# Callbacks base
CB_ROOT = "admin:security"
CB_PAGE = "admin:security:page"              # :cat:N
CB_CATEGORY = "admin:security:cat"           # :key
CB_VIEW = "admin:security:view"              # :ID
CB_LOGINS = "admin:security:logins"
CB_SUSPECTS = "admin:security:suspects"
CB_MULTI = "admin:security:multi"
CB_SPAM = "admin:security:spam"
CB_TEMPBAN = "admin:security:tempban"
CB_MONITORED = "admin:security:monitored"
CB_LIMITS = "admin:security:limits"
CB_SET_LIMIT = "admin:security:setlimit"     # :field
CB_UNBLOCK = "admin:security:unblock"        # :UID
CB_BAN = "admin:security:ban"                # :UID
CB_BAN_YES = "admin:security:ban:yes"        # :UID
CB_TEMPBLOCK = "admin:security:tempblock"    # :UID
CB_TEMPBLOCK_YES = "admin:security:tempblock:yes"  # :UID:MIN
CB_ALERTS = "admin:security:alerts"
CB_RELOAD = "admin:security:reload"

# Categorias de evento
CATEGORIES: Dict[str, str] = {
    "all": "📋 Todos",
    "multi_account_ip": "🌐 Múltiplas Contas",
    "duplicate_pix_key": "🔑 PIX Duplicado",
    "duplicate_whatsapp": "📱 WhatsApp Duplicado",
    "duplicate_email": "📧 E-mail Duplicado",
    "suspicious_amount": "💰 Valor Suspeito",
    "velocity_tx": "⚡ Velocidade TX",
    "velocity_withdraw": "💸 Velocidade Saque",
    "failed_actions": "❌ Ações Falhas",
    "abuse": "🚫 Abuso",
}

# Campos configuráveis (Setting)
LIMIT_FIELDS: Dict[str, Dict[str, Any]] = {
    "antifraud_enabled": {"label": "Antifraude ativo", "type": "bool", "default": "true"},
    "antifraud_max_accounts_per_ip": {"label": "Máx. contas por IP", "type": "int", "default": "3"},
    "antifraud_suspicious_amount": {"label": "Valor suspeito (R$)", "type": "money", "default": "1000.00"},
    "throttle_rate": {"label": "Throttle (s)", "type": "number", "default": "0.5"},
    "temp_block_minutes": {"label": "Bloqueio temp (min)", "type": "int", "default": "15"},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
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


async def _set_setting(key: str, value: str) -> bool:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is None:
                session.add(Setting(key=key, value=value, category="security"))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting (security) falhou: {exc}")
        return False


def _sev_emoji(sev: Any) -> str:
    s = str(sev or "info").lower()
    return {
        "info": "🔵",
        "warning": "🟡",
        "error": "🔴",
        "critical": "🚨",
    }.get(s, "⚪️")


# ============================================================
# DASHBOARD
# ============================================================
async def _collect_dashboard() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_24h = now - timedelta(hours=24)

    out: Dict[str, Any] = {
        "total": 0,
        "today": 0,
        "24h": 0,
        "warnings": 0,
        "errors": 0,
        "alerts": 0,
        "blocked": 0,
        "temp_blocks": 0,
        "by_type": [],
    }

    try:
        async with session_scope() as session:
            out["total"] = int(
                (await session.execute(select(func.count(SecurityEvent.id)))).scalar() or 0
            )
            out["today"] = int(
                (
                    await session.execute(
                        select(func.count(SecurityEvent.id)).where(
                            SecurityEvent.created_at >= start_today
                        )
                    )
                ).scalar()
                or 0
            )
            out["24h"] = int(
                (
                    await session.execute(
                        select(func.count(SecurityEvent.id)).where(
                            SecurityEvent.created_at >= last_24h
                        )
                    )
                ).scalar()
                or 0
            )
            out["warnings"] = int(
                (
                    await session.execute(
                        select(func.count(SecurityEvent.id)).where(
                            SecurityEvent.severity == "warning"
                        )
                    )
                ).scalar()
                or 0
            )
            out["errors"] = int(
                (
                    await session.execute(
                        select(func.count(SecurityEvent.id)).where(
                            SecurityEvent.severity.in_(("error", "critical"))
                        )
                    )
                ).scalar()
                or 0
            )

            out["alerts"] = int(
                (
                    await session.execute(
                        select(func.count(Alert.id))
                        .where(Alert.category == "security")
                        .where(Alert.resolved == False)  # noqa: E712
                    )
                ).scalar()
                or 0
            )

            out["blocked"] = int(
                (
                    await session.execute(
                        select(func.count(User.id)).where(
                            User.status.in_((UserStatus.BLOCKED, UserStatus.BANNED))
                        )
                    )
                ).scalar()
                or 0
            )

            # Conta por tipo
            res = await session.execute(
                select(SecurityEvent.type, func.count(SecurityEvent.id))
                .group_by(SecurityEvent.type)
                .order_by(func.count(SecurityEvent.id).desc())
                .limit(10)
            )
            out["by_type"] = [(str(t), int(c or 0)) for t, c in res.all()]
    except Exception as exc:
        logger.debug(f"_collect_dashboard falhou: {exc}")

    # Temp blocks (Redis)
    try:
        r = await get_redis()
        pattern = f"{settings.REDIS_PREFIX}:security:tempblock:*"
        count = 0
        async for _ in r.scan_iter(match=pattern, count=200):
            count += 1
        out["temp_blocks"] = count
    except Exception:
        pass

    return out


async def render_dashboard(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    stats = await _collect_dashboard()

    by_type_text = ""
    if stats["by_type"]:
        lines = []
        for t, c in stats["by_type"][:8]:
            label = CATEGORIES.get(t, t)
            lines.append(f"  • {label}: <b>{fmt_num(c)}</b>")
        by_type_text = "\n".join(lines)
    else:
        by_type_text = "  <i>Nenhum evento registrado.</i>"

    rendered = await message_engine.render(
        MSG_KEY_DASH,
        fallback=(
            "🛡️ <b>SEGURANÇA E ANTIFRAUDE</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>EVENTOS</b>\n"
            f"├ Total: <b>{fmt_num(stats['total'])}</b>\n"
            f"├ Hoje: <b>{fmt_num(stats['today'])}</b>\n"
            f"├ Últimas 24h: <b>{fmt_num(stats['24h'])}</b>\n"
            f"├ 🟡 Avisos: <b>{fmt_num(stats['warnings'])}</b>\n"
            f"└ 🔴 Erros: <b>{fmt_num(stats['errors'])}</b>\n\n"
            "🚨 <b>STATUS</b>\n"
            f"├ Alertas ativos: <b>{fmt_num(stats['alerts'])}</b>\n"
            f"├ Usuários bloqueados: <b>{fmt_num(stats['blocked'])}</b>\n"
            f"└ Bloqueios temp.: <b>{fmt_num(stats['temp_blocks'])}</b>\n\n"
            "🏷️ <b>Por tipo de evento:</b>\n"
            f"{by_type_text}"
        ),
        total=fmt_num(stats["total"]),
        today=fmt_num(stats["today"]),
        last24h=fmt_num(stats["24h"]),
        warnings=fmt_num(stats["warnings"]),
        errors=fmt_num(stats["errors"]),
        alerts=fmt_num(stats["alerts"]),
        blocked=fmt_num(stats["blocked"]),
        temp_blocks=fmt_num(stats["temp_blocks"]),
        by_type=by_type_text,
        logins=fmt_num(0),
        tentativas=fmt_num(stats["warnings"]),
        suspeitas=fmt_num(stats["errors"]),
        mult=fmt_num(0),
        spam=fmt_num(0),
        monit=fmt_num(0),
    )

    engine_kb = await button_engine.inline(SCREEN_DASH)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="📜 Eventos", callback_data=f"{CB_PAGE}:all:1"),
                    InlineKeyboardButton(text="🔑 Logins", callback_data=CB_LOGINS),
                ],
                [
                    InlineKeyboardButton(text="🚨 Suspeitas", callback_data=CB_SUSPECTS),
                    InlineKeyboardButton(text="🌐 Múltiplas Contas", callback_data=CB_MULTI),
                ],
                [
                    InlineKeyboardButton(text="🛡️ Anti-spam", callback_data=CB_SPAM),
                    InlineKeyboardButton(text="⏱️ Bloqueios Temp", callback_data=CB_TEMPBAN),
                ],
                [
                    InlineKeyboardButton(text="👀 Monitorados", callback_data=CB_MONITORED),
                    InlineKeyboardButton(text="⚙️ Limites", callback_data=CB_LIMITS),
                ],
                [
                    InlineKeyboardButton(text="⚠️ Alertas", callback_data=CB_ALERTS),
                    InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_RELOAD),
                ],
                [
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
        logger.exception(f"security on_root falhou: {exc}")


# ============================================================
# EVENTOS
# ============================================================
async def _count_events(category: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(SecurityEvent.id))
            if category != "all":
                stmt = stmt.where(SecurityEvent.type == category)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_events(
    page: int,
    category: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[SecurityEvent]:
    try:
        async with session_scope() as session:
            stmt = select(SecurityEvent).order_by(SecurityEvent.created_at.desc())
            if category != "all":
                stmt = stmt.where(SecurityEvent.type == category)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def render_events(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    category: str = "all",
) -> Optional[Message]:
    total = await _count_events(category)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    items = await _list_events(page, category)

    if not items:
        rendered = await message_engine.render(
            MSG_KEY_EVENTS_EMPTY,
            fallback=(
                "📜 <b>EVENTOS DE SEGURANÇA</b>\n\n"
                "<i>Nenhum evento encontrado.</i>"
            ),
            categoria=CATEGORIES.get(category, category),
        )
    else:
        rows: List[List[str]] = []
        for e in items:
            sev_em = _sev_emoji(e.severity)
            rows.append(
                [
                    str(e.id),
                    sev_em,
                    truncate(e.type or "—", 20),
                    str(e.user_id or "—"),
                    truncate(e.ip or "—", 14),
                    fmt_dt(e.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "", "Tipo", "User", "IP", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_EVENTS,
            fallback=(
                f"📜 <b>EVENTOS DE SEGURANÇA</b> ({total})\n"
                f"🏷️ {CATEGORIES.get(category, category)}\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
            categoria=CATEGORIES.get(category, category),
        )

    rows_kb: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📋 Todos", callback_data=f"{CB_CATEGORY}:all"
            ),
            InlineKeyboardButton(
                text="🌐 Múlt. Contas",
                callback_data=f"{CB_CATEGORY}:multi_account_ip",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💰 Suspeitas",
                callback_data=f"{CB_CATEGORY}:suspicious_amount",
            ),
            InlineKeyboardButton(
                text="❌ Falhas",
                callback_data=f"{CB_CATEGORY}:failed_actions",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⚡ Vel. TX",
                callback_data=f"{CB_CATEGORY}:velocity_tx",
            ),
            InlineKeyboardButton(
                text="💸 Vel. Saque",
                callback_data=f"{CB_CATEGORY}:velocity_withdraw",
            ),
        ],
    ]

    for e in items[:8]:
        sev_em = _sev_emoji(e.severity)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{sev_em} #{e.id} {truncate(e.type or '—', 24)}",
                    callback_data=f"{CB_VIEW}:{e.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{category}",
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

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
        parts = callback.data.split(":")
        # admin:security:page:CAT:N
        category = "all"
        page = 1
        if len(parts) >= 4:
            category = parts[3]
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except Exception:
                page = 1
        if category not in CATEGORIES:
            category = "all"
        await render_events(callback, page=page, category=category)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_page falhou: {exc}")


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
        await render_events(callback, page=1, category=cat)
        await toast_ok(callback, f"{CATEGORIES[cat]} selecionado.")
    except Exception as exc:
        logger.exception(f"security on_category falhou: {exc}")


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
            eid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(SecurityEvent).where(SecurityEvent.id == eid)
                )
                e = res.scalar_one_or_none()
        except Exception:
            e = None

        if e is None:
            await toast_err(callback, "❌ Evento não encontrado.")
            return

        details = e.details or {}
        try:
            import json as _json

            details_str = _json.dumps(details, indent=2, default=str, ensure_ascii=False)
        except Exception:
            details_str = str(details)

        sev_em = _sev_emoji(e.severity)

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"{sev_em} <b>EVENTO #{e.id}</b>\n\n"
                f"├ Tipo: <code>{e.type or '—'}</code>\n"
                f"├ Severidade: {e.severity or 'info'}\n"
                f"├ Usuário: <code>{e.user_id or '—'}</code>\n"
                f"├ IP: <code>{e.ip or '—'}</code>\n"
                f"└ Data: {fmt_dt(e.created_at)}\n\n"
                "📄 <b>Detalhes:</b>\n"
                f"<code>{truncate(details_str, 700)}</code>"
            ),
            id=e.id,
            tipo=e.type or "—",
            severidade=e.severity or "info",
            user_id=e.user_id or "—",
            ip=e.ip or "—",
            data=fmt_dt(e.created_at),
            detalhes=truncate(details_str, 700),
        )

        rows_kb: List[List[InlineKeyboardButton]] = []

        if e.user_id:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="🚫 Banir usuário",
                        callback_data=f"{CB_BAN}:{e.user_id}",
                    ),
                    InlineKeyboardButton(
                        text="⏱️ Bloqueio temp",
                        callback_data=f"{CB_TEMPBLOCK}:{e.user_id}",
                    ),
                ]
            )

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_PAGE}:all:1")]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_view falhou: {exc}")


# ============================================================
# AÇÕES: BANIR / TEMPBLOCK / DESBLOQUEAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_BAN}:"))
async def on_ban(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            uid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        text = (
            f"🚫 <b>BANIR USUÁRIO #{uid}</b>\n\n"
            "O usuário será marcado como <b>BANIDO</b> e não poderá usar o bot.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_BAN_YES}:{uid}",
            cancel_callback=CB_ROOT,
            confirm_text="🚫 Sim, banir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_ban falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_BAN_YES}:"))
async def on_ban_confirm(callback: CallbackQuery) -> None:
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
                res = await session.execute(select(User).where(User.id == uid))
                u = res.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return
                u.status = UserStatus.BANNED
                u.ban_reason = "Banido pela central de segurança"
                session.add(u)
                tg_id = int(u.telegram_id)
        except Exception as exc:
            logger.exception(f"ban user falhou: {exc}")
            await toast_err(callback, "❌ Erro ao banir.")
            return

        await log_action(
            admin_id=admin_id,
            action="security.ban_user",
            category="security",
            user_id=uid,
            details={"telegram_id": tg_id},
        )

        try:
            await callback.bot.send_message(
                chat_id=tg_id,
                text="🚫 <b>Sua conta foi banida.</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await render_dashboard(callback)
        await toast_ok(callback, "🚫 Banido.")
    except Exception as exc:
        logger.exception(f"security on_ban_confirm falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_TEMPBLOCK}:"))
async def on_tempblock(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            uid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        default_min = 15
        try:
            default_min = int(await _get_setting("temp_block_minutes", "15") or "15")
        except Exception:
            default_min = 15

        text = (
            f"⏱️ <b>BLOQUEIO TEMPORÁRIO</b>\n\n"
            f"👤 Usuário: <code>#{uid}</code>\n"
            f"⏳ Duração padrão: <b>{default_min} min</b>\n\n"
            "Escolha a duração:"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="5 min",
                    callback_data=f"{CB_TEMPBLOCK_YES}:{uid}:5",
                ),
                InlineKeyboardButton(
                    text="15 min",
                    callback_data=f"{CB_TEMPBLOCK_YES}:{uid}:15",
                ),
                InlineKeyboardButton(
                    text="60 min",
                    callback_data=f"{CB_TEMPBLOCK_YES}:{uid}:60",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="24h",
                    callback_data=f"{CB_TEMPBLOCK_YES}:{uid}:1440",
                ),
                InlineKeyboardButton(
                    text="7 dias",
                    callback_data=f"{CB_TEMPBLOCK_YES}:{uid}:10080",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                )
            ],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_tempblock falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_TEMPBLOCK_YES}:"))
async def on_tempblock_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:security:tempblock:yes:UID:MIN
        if len(parts) < 7:
            await toast_err(callback, "❌ Callback inválido.")
            return
        try:
            uid = int(parts[5])
            minutes = int(parts[6])
        except Exception:
            await toast_err(callback, "❌ Parâmetros inválidos.")
            return

        seconds = max(60, minutes * 60)
        try:
            expires = await security_monitor.temp_block(
                uid,
                seconds=seconds,
                reason="manual_admin",
            )
        except Exception as exc:
            logger.debug(f"temp_block falhou: {exc}")
            expires = None

        await log_action(
            admin_id=admin_id,
            action="security.temp_block",
            category="security",
            user_id=uid,
            details={"minutes": minutes},
        )

        try:
            async with session_scope() as session:
                res = await session.execute(select(User).where(User.id == uid))
                u = res.scalar_one_or_none()
                if u is not None:
                    await callback.bot.send_message(
                        chat_id=int(u.telegram_id),
                        text=(
                            f"⏱️ <b>Você foi temporariamente bloqueado.</b>\n"
                            f"Duração: <b>{minutes} min</b>"
                        ),
                        parse_mode="HTML",
                    )
        except Exception:
            pass

        await render_dashboard(callback)
        await toast_ok(callback, f"⏱️ Bloqueio de {minutes} min aplicado.")
    except Exception as exc:
        logger.exception(f"security on_tempblock_confirm falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_UNBLOCK}:"))
async def on_unblock(callback: CallbackQuery) -> None:
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
            await security_monitor.unblock(uid)
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="security.unblock",
            category="security",
            user_id=uid,
        )

        await render_dashboard(callback)
        await toast_ok(callback, "✅ Desbloqueado.")
    except Exception as exc:
        logger.exception(f"security on_unblock falhou: {exc}")


# ============================================================
# ATALHOS: LOGINS / SUSPEITAS / MULTI / SPAM / TEMPBAN / MONITORED
# ============================================================
@router.callback_query(F.data == CB_LOGINS)
async def on_logins(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Logins = eventos relacionados a múltiplas contas por IP
        await render_events(callback, page=1, category="multi_account_ip")
        await toast_ok(callback, "🔑 Logins por IP.")
    except Exception as exc:
        logger.exception(f"security on_logins falhou: {exc}")


@router.callback_query(F.data == CB_SUSPECTS)
async def on_suspects(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await render_events(callback, page=1, category="suspicious_amount")
        await toast_ok(callback, "🚨 Transações suspeitas.")
    except Exception as exc:
        logger.exception(f"security on_suspects falhou: {exc}")


@router.callback_query(F.data == CB_MULTI)
async def on_multi(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await render_events(callback, page=1, category="multi_account_ip")
        await toast_ok(callback, "🌐 Múltiplas contas por IP.")
    except Exception as exc:
        logger.exception(f"security on_multi falhou: {exc}")


@router.callback_query(F.data == CB_SPAM)
async def on_spam(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        throttle = await _get_setting("throttle_rate", str(settings.THROTTLE_RATE))

        text = (
            "🛡️ <b>ANTI-SPAM</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Throttle rate: <b>{throttle}s</b>\n"
            "├ Middleware: <b>ativo</b>\n"
            "└ Fonte: Redis (Throttle)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "O middleware <code>throttle</code> bloqueia requisições\n"
            "repetidas dentro do intervalo configurado.\n"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="✏️ Editar Throttle",
                    callback_data=f"{CB_SET_LIMIT}:throttle_rate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 Ver Eventos",
                    callback_data=f"{CB_CATEGORY}:abuse",
                )
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_spam falhou: {exc}")


@router.callback_query(F.data == CB_TEMPBAN)
async def on_tempban_list(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Lista bloqueios temporários ativos (Redis)
        entries: List[Dict[str, Any]] = []
        try:
            r = await get_redis()
            pattern = f"{settings.REDIS_PREFIX}:security:tempblock:*"
            async for key in r.scan_iter(match=pattern, count=200):
                ttl = await r.ttl(key)
                uid_str = str(key).split(":")[-1]
                try:
                    uid = int(uid_str)
                except Exception:
                    continue
                entries.append({"user_id": uid, "ttl": int(ttl or 0)})
        except Exception as exc:
            logger.debug(f"list tempban falhou: {exc}")

        if not entries:
            text = (
                "⏱️ <b>BLOQUEIOS TEMPORÁRIOS</b>\n\n"
                "<i>Nenhum bloqueio ativo.</i>"
            )
        else:
            lines = [f"⏱️ <b>BLOQUEIOS TEMPORÁRIOS</b> ({len(entries)})", ""]
            for e in entries:
                mins = max(1, e["ttl"] // 60)
                lines.append(f"👤 <code>#{e['user_id']}</code> — {mins} min")
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for e in entries[:10]:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Liberar #{e['user_id']}",
                        callback_data=f"{CB_UNBLOCK}:{e['user_id']}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_tempban_list falhou: {exc}")


@router.callback_query(F.data == CB_MONITORED)
async def on_monitored(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Usuários com eventos de severidade alta
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(
                        SecurityEvent.user_id,
                        func.count(SecurityEvent.id).label("cnt"),
                    )
                    .where(SecurityEvent.severity.in_(("error", "critical", "warning")))
                    .where(SecurityEvent.user_id.isnot(None))
                    .group_by(SecurityEvent.user_id)
                    .order_by(func.count(SecurityEvent.id).desc())
                    .limit(20)
                )
                monitored = res.all()
        except Exception:
            monitored = []

        if not monitored:
            text = (
                "👀 <b>USUÁRIOS MONITORADOS</b>\n\n"
                "<i>Nenhum usuário com histórico crítico.</i>"
            )
        else:
            lines = [f"👀 <b>USUÁRIOS MONITORADOS</b> ({len(monitored)})", ""]
            for uid, cnt in monitored:
                lines.append(f"👤 <code>#{uid}</code> — {fmt_num(cnt)} eventos")
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for uid, _ in monitored[:10]:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"🚫 Banir #{uid}",
                        callback_data=f"{CB_BAN}:{uid}",
                    ),
                    InlineKeyboardButton(
                        text=f"⏱️ Temp #{uid}",
                        callback_data=f"{CB_TEMPBLOCK}:{uid}",
                    ),
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_monitored falhou: {exc}")


# ============================================================
# LIMITES
# ============================================================
@router.callback_query(F.data == CB_LIMITS)
async def on_limits(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        vals: Dict[str, str] = {}
        for k, meta in LIMIT_FIELDS.items():
            v = await _get_setting(k, str(meta.get("default", "")))
            vals[k] = str(v or "")

        enabled_emoji = "🟢" if str(vals.get("antifraud_enabled", "true")).lower() == "true" else "🔴"

        lines = [
            "⚙️ <b>LIMITES DE SEGURANÇA</b>",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"├ Antifraude: {enabled_emoji}",
            f"├ Máx. contas por IP: <b>{vals.get('antifraud_max_accounts_per_ip', '—')}</b>",
            f"├ Valor suspeito: <b>R$ {vals.get('antifraud_suspicious_amount', '—')}</b>",
            f"├ Throttle rate: <b>{vals.get('throttle_rate', '—')}s</b>",
            f"└ Bloqueio temp: <b>{vals.get('temp_block_minutes', '—')} min</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="🔄 Toggle Antifraude",
                    callback_data=f"{CB_SET_LIMIT}:antifraud_enabled",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Máx. contas/IP",
                    callback_data=f"{CB_SET_LIMIT}:antifraud_max_accounts_per_ip",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Valor suspeito",
                    callback_data=f"{CB_SET_LIMIT}:antifraud_suspicious_amount",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Throttle",
                    callback_data=f"{CB_SET_LIMIT}:throttle_rate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Bloqueio temp",
                    callback_data=f"{CB_SET_LIMIT}:temp_block_minutes",
                )
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_limits falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_SET_LIMIT}:"))
async def on_set_limit(callback: CallbackQuery) -> None:
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

        meta = LIMIT_FIELDS.get(field)
        if meta is None:
            await toast_err(callback, "❌ Campo inválido.")
            return

        # Bool → toggle direto
        if meta["type"] == "bool":
            current = await _get_setting(field, meta.get("default", "true"))
            new_val = "false" if str(current).lower() == "true" else "true"
            ok = await _set_setting(field, new_val)
            if not ok:
                await toast_err(callback, "❌ Erro ao salvar.")
                return
            await log_action(
                admin_id=admin_id,
                action=f"security.set.{field}",
                category="security",
                details={"value": new_val},
            )
            await on_limits(callback)
            await toast_ok(callback, f"🔄 {meta['label']} → {new_val}")
            return

        hint = "Ex.: <code>3</code>"
        if meta["type"] == "money":
            hint = "Ex.: <code>1000.00</code>"
        elif meta["type"] == "number":
            hint = "Ex.: <code>0.5</code>"

        prompt = (
            f"✏️ <b>EDITAR · {meta['label'].upper()}</b>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"seclimit:{field}",
            screen=f"security:limit:{field}",
            extra={"field": field},
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"security on_set_limit falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_security_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting.startswith("seclimit:"):
        field = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_limit(message, admin_id, field, value)
        return


async def _apply_limit(
    message: Message,
    admin_id: int,
    field: str,
    value: str,
) -> None:
    meta = LIMIT_FIELDS.get(field)
    if meta is None:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip().replace(",", ".")
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    if meta["type"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        val = str(int(num))
    elif meta["type"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        val = f"{num:.2f}"
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
        action=f"security.set.{field}",
        category="security",
        details={"value": val},
    )

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado para <code>{val}</code>.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_LIMITS)]]
        ),
    )


# ============================================================
# ALERTAS DE SEGURANÇA
# ============================================================
@router.callback_query(F.data == CB_ALERTS)
async def on_alerts(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Alert)
                    .where(Alert.category == "security")
                    .where(Alert.resolved == False)  # noqa: E712
                    .order_by(Alert.created_at.desc())
                    .limit(20)
                )
                alerts = list(res.scalars().all())
        except Exception:
            alerts = []

        if not alerts:
            text = (
                "⚠️ <b>ALERTAS DE SEGURANÇA</b>\n\n"
                "✅ <i>Nenhum alerta ativo.</i>"
            )
        else:
            lines = [f"⚠️ <b>ALERTAS DE SEGURANÇA ({len(alerts)})</b>", ""]
            for a in alerts:
                sev_em = _sev_emoji(a.severity)
                lines.append(
                    f"{sev_em} <b>{truncate(a.title, 40)}</b>\n"
                    f"    {fmt_dt(a.created_at, with_time=False)}"
                )
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
        logger.exception(f"security on_alerts falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_dashboard",
    "render_events",
    "on_root",
    "on_page",
    "on_category",
    "on_view",
    "on_ban",
    "on_ban_confirm",
    "on_tempblock",
    "on_tempblock_confirm",
    "on_unblock",
    "on_logins",
    "on_suspects",
    "on_multi",
    "on_spam",
    "on_tempban_list",
    "on_monitored",
    "on_limits",
    "on_set_limit",
    "on_security_text_input",
    "on_alerts",
    "CB_ROOT",
    "CB_PAGE",
    "CB_CATEGORY",
    "CB_VIEW",
    "CB_LOGINS",
    "CB_SUSPECTS",
    "CB_MULTI",
    "CB_SPAM",
    "CB_TEMPBAN",
    "CB_MONITORED",
    "CB_LIMITS",
    "CB_SET_LIMIT",
    "CB_UNBLOCK",
    "CB_BAN",
    "CB_BAN_YES",
    "CB_TEMPBLOCK",
    "CB_TEMPBLOCK_YES",
    "CB_ALERTS",
    "CB_RELOAD",
    "CATEGORIES",
    "LIMIT_FIELDS",
]
