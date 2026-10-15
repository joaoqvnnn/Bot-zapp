"""
Larizinha Store — Módulo Admin: Afiliados
Implementa gestão completa do programa de afiliados conforme DIM.

Telas:
- Painel geral (totais, comissões, pendentes, pagas)
- Lista de afiliados (filtros: ativos/bloqueados/top, paginação DIM)
- Detalhes do afiliado (indicações, ganhos, nível, saldo)
- Configurar comissões globais (% compra, % depósito, mínimo saque)
- Gerenciar níveis (Iniciante, Bronze, Prata, Ouro) — meta + %
- Aprovar / Bloquear afiliado
- Pagar comissões (aprova todas as pendentes)
- Ver comissões de um afiliado (pendentes/aprovadas/pagas)
- Config de níveis editável via texto

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

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
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import (
    Commission,
    CommissionStatus,
    Setting,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_ACTIVE,
    SYM_BANNED,
    SYM_BLOCKED,
    SYM_DONE,
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
    humanize_bool,
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
router = Router(name="admin_affiliates")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_OVERVIEW = "admin_affiliates_overview"
MSG_KEY_LIST = "admin_affiliates_list"
MSG_KEY_EMPTY = "admin_affiliates_empty"
MSG_KEY_DETAIL = "admin_affiliates_detail"
MSG_KEY_LEVELS = "admin_affiliates_levels"
MSG_KEY_CONFIG = "admin_affiliates_config"

SCREEN_OVERVIEW = "admin_affiliates_overview"
SCREEN_LIST = "admin_affiliates_list"
SCREEN_DETAIL = "admin_affiliates_detail"
SCREEN_LEVELS = "admin_affiliates_levels"
SCREEN_CONFIG = "admin_affiliates_config"

# Callbacks base
CB_ROOT = "admin:affiliates"
CB_OVERVIEW = "admin:affiliates:overview"
CB_PAGE = "admin:affiliates:page"           # :filter:N
CB_FILTER = "admin:affiliates:filter"       # :key
CB_SEARCH = "admin:affiliates:search"
CB_VIEW = "admin:affiliates:view"           # :ID
CB_APPROVE = "admin:affiliates:approve"     # :ID
CB_BLOCK = "admin:affiliates:block"         # :ID
CB_UNBLOCK = "admin:affiliates:unblock"     # :ID
CB_PAY = "admin:affiliates:pay"             # :ID
CB_PAY_YES = "admin:affiliates:pay:yes"     # :ID
CB_COMMS = "admin:affiliates:comms"         # :ID
CB_LEVELS = "admin:affiliates:levels"
CB_LEVEL_SET = "admin:affiliates:levelset"  # :name:field
CB_CONFIG = "admin:affiliates:config"
CB_CONFIG_SET = "admin:affiliates:cfgset"   # :field
CB_SET_LEVEL = "admin:affiliates:setlevel"  # :ID

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "active": "🟢 Ativos",
    "inactive": "🔴 Inativos",
    "blocked": "🚫 Bloqueados",
    "top": "🏆 Top Ganhos",
}

# Níveis padrão (metas e %)
DEFAULT_LEVELS: Dict[str, Dict[str, Any]] = {
    "iniciante": {"label": "🌱 Iniciante", "meta": 0, "pct": 5},
    "bronze": {"label": "🥉 Bronze", "meta": 10, "pct": 10},
    "prata": {"label": "🥈 Prata", "meta": 50, "pct": 15},
    "ouro": {"label": "🥇 Ouro", "meta": 100, "pct": 20},
}

# Campos de config (Setting keys)
CONFIG_FIELDS: Dict[str, Dict[str, Any]] = {
    "affiliate_commission_percent": {
        "label": "% por compra",
        "type": "number",
        "default": str(settings.AFFILIATE_COMMISSION_PERCENT),
    },
    "affiliate_commission_deposit_percent": {
        "label": "% por depósito",
        "type": "number",
        "default": str(settings.AFFILIATE_COMMISSION_DEPOSIT_PERCENT),
    },
    "affiliate_min_withdraw": {
        "label": "Saque mínimo",
        "type": "money",
        "default": str(settings.AFFILIATE_MIN_WITHDRAW),
    },
    "affiliate_enabled": {
        "label": "Programa ativo",
        "type": "bool",
        "default": "true",
    },
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
    }.get(s, "⚪️")


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


async def _set_setting(key: str, value: str, category: str = "affiliates") -> bool:
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
        logger.debug(f"_set_setting (aff) falhou: {exc}")
        return False


async def _level_config() -> Dict[str, Dict[str, Any]]:
    """Retorna a configuração dos níveis (com override via Setting)."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, meta in DEFAULT_LEVELS.items():
        meta_k = await _get_setting(f"affiliate_level_{name}_meta", str(meta["meta"]))
        pct_k = await _get_setting(f"affiliate_level_{name}_pct", str(meta["pct"]))
        try:
            meta_v = int(meta_k or meta["meta"])
        except Exception:
            meta_v = int(meta["meta"])
        try:
            pct_v = float(pct_k or meta["pct"])
        except Exception:
            pct_v = float(meta["pct"])
        out[name] = {
            "label": meta["label"],
            "meta": meta_v,
            "pct": pct_v,
        }
    return out


def _apply_filter(stmt, filter_key: str):
    if filter_key == "active":
        return stmt.where(User.affiliate_active == True)  # noqa: E712
    if filter_key == "inactive":
        return stmt.where(User.affiliate_active == False)  # noqa: E712
    if filter_key == "blocked":
        return stmt.where(User.status == UserStatus.BLOCKED)
    if filter_key == "top":
        return stmt.order_by(User.affiliate_balance.desc())
    return stmt


async def _count_affiliates(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(User.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_affiliates(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[User]:
    try:
        async with session_scope() as session:
            stmt = select(User)
            if filter_key == "top":
                stmt = stmt.order_by(User.affiliate_balance.desc())
            else:
                stmt = stmt.order_by(User.id.asc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_referrals(uid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(User.id)).where(User.referred_by_id == uid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _commission_totals(uid: int) -> Dict[str, float]:
    """Retorna os totais de comissões por status para um usuário."""
    out = {"pending": 0.0, "approved": 0.0, "paid": 0.0, "reversed": 0.0}
    try:
        async with session_scope() as session:
            for status, key in (
                (CommissionStatus.PENDING, "pending"),
                (CommissionStatus.APPROVED, "approved"),
                (CommissionStatus.PAID, "paid"),
                (CommissionStatus.REVERSED, "reversed"),
            ):
                res = await session.execute(
                    select(func.coalesce(func.sum(Commission.amount), 0))
                    .where(Commission.earner_id == uid)
                    .where(Commission.status == status)
                )
                out[key] = round(float(res.scalar() or 0), 2)
    except Exception:
        pass
    return out


# ============================================================
# PAINEL GERAL
# ============================================================
async def _collect_overview() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "total": 0,
        "ativos": 0,
        "inativos": 0,
        "pendentes": 0.0,
        "aprovadas": 0.0,
        "pagas": 0.0,
        "estornos": 0.0,
        "comissoes_hoje": 0.0,
    }
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        async with session_scope() as session:
            # Total de afiliados
            res = await session.execute(
                select(func.count(User.id)).where(
                    User.affiliate_code.isnot(None)
                )
            )
            out["total"] = int(res.scalar() or 0)

            # Ativos
            res = await session.execute(
                select(func.count(User.id)).where(User.affiliate_active == True)  # noqa: E712
            )
            out["ativos"] = int(res.scalar() or 0)

            out["inativos"] = max(0, out["total"] - out["ativos"])

            # Comissões por status
            for status, key in (
                (CommissionStatus.PENDING, "pendentes"),
                (CommissionStatus.APPROVED, "aprovadas"),
                (CommissionStatus.PAID, "pagas"),
                (CommissionStatus.REVERSED, "estornos"),
            ):
                res = await session.execute(
                    select(func.coalesce(func.sum(Commission.amount), 0))
                    .where(Commission.status == status)
                )
                out[key] = round(float(res.scalar() or 0), 2)

            # Comissões de hoje
            res = await session.execute(
                select(func.coalesce(func.sum(Commission.amount), 0))
                .where(Commission.created_at >= start_today)
            )
            out["comissoes_hoje"] = round(float(res.scalar() or 0), 2)
    except Exception as exc:
        logger.debug(f"_collect_overview falhou: {exc}")

    return out


async def render_overview(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    stats = await _collect_overview()

    rendered = await message_engine.render(
        MSG_KEY_OVERVIEW,
        fallback=(
            "👥 <b>AFILIADOS · PAINEL</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total: <b>{fmt_num(stats['total'])}</b>\n"
            f"🟢 Ativos: <b>{fmt_num(stats['ativos'])}</b>\n"
            f"🔴 Inativos: <b>{fmt_num(stats['inativos'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟡 Pendentes: <b>{fmt_brl(stats['pendentes'])}</b>\n"
            f"🟢 Aprovadas: <b>{fmt_brl(stats['aprovadas'])}</b>\n"
            f"✅ Pagas: <b>{fmt_brl(stats['pagas'])}</b>\n"
            f"♻️ Estornos: <b>{fmt_brl(stats['estornos'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 Hoje: <b>{fmt_brl(stats['comissoes_hoje'])}</b>"
        ),
        total=fmt_num(stats["total"]),
        ativos=fmt_num(stats["ativos"]),
        inativos=fmt_num(stats["inativos"]),
        pendentes=fmt_brl(stats["pendentes"]),
        aprovadas=fmt_brl(stats["aprovadas"]),
        pagas=fmt_brl(stats["pagas"]),
        estornos=fmt_brl(stats["estornos"]),
        hoje=fmt_brl(stats["comissoes_hoje"]),
    )

    engine_kb = await button_engine.inline(SCREEN_OVERVIEW)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="👥 Lista de Afiliados",
                        callback_data=f"{CB_PAGE}:all:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🏆 Níveis",
                        callback_data=CB_LEVELS,
                    ),
                    InlineKeyboardButton(
                        text="⚙️ Configurações",
                        callback_data=CB_CONFIG,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data="admin:menu",
                    )
                ],
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
        logger.exception(f"affiliates on_overview falhou: {exc}")


# ============================================================
# LISTA DE AFILIADOS
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_affiliates(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    users = await _list_affiliates(page, filter_key)

    if not users:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "👥 <b>AFILIADOS</b>\n\n"
                "<i>Nenhum afiliado encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for u in users:
            refs = await _count_referrals(int(u.id))
            status = _status_emoji(u.status)
            rows.append(
                [
                    str(u.id),
                    truncate(u.first_name or "—", 14),
                    truncate(u.affiliate_level or "—", 10),
                    str(refs),
                    fmt_brl(u.affiliate_balance),
                    status,
                ]
            )

        lista = table(rows, ["ID", "Nome", "Nível", "Refs", "Comissões", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"👥 <b>AFILIADOS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="🟢 Ativos", callback_data=f"{CB_FILTER}:active"),
                InlineKeyboardButton(text="🔴 Inativos", callback_data=f"{CB_FILTER}:inactive"),
            ],
            [
                InlineKeyboardButton(text="🚫 Bloqueados", callback_data=f"{CB_FILTER}:blocked"),
                InlineKeyboardButton(text="🏆 Top", callback_data=f"{CB_FILTER}:top"),
            ],
            [
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for u in users:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"👤 #{u.id} {truncate(u.first_name or '—', 20)} · "
                    f"{fmt_brl(u.affiliate_balance)}",
                    callback_data=f"{CB_VIEW}:{u.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{filter_key}",
        back_callback=CB_OVERVIEW,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(build_back_row(CB_OVERVIEW))

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # admin:affiliates:page:FILTER:N
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
        logger.exception(f"affiliates on_page falhou: {exc}")


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
        logger.exception(f"affiliates on_filter falhou: {exc}")


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
            "🔍 <b>BUSCAR AFILIADO</b>\n\n"
            "Envie o ID interno do usuário.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="affiliate_search_id",
            screen="affiliates:search",
            use_force_reply=True,
            reply_placeholder="ID do usuário",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_affiliates_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Buscar
    if awaiting == "affiliate_search_id":
        value, _extra = await consume_input(message, admin_id, expected_field="affiliate_search_id")
        if value is None:
            return
        try:
            uid = int(value.strip())
        except Exception:
            await message.answer("❌ ID inválido.")
            return
        u = await _load_user(uid)
        if u is None:
            await message.answer("❌ Usuário não encontrado.")
            return
        text, keyboard = await _render_detail(uid)
        if text:
            await message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        return

    # Config
    if awaiting.startswith("affcfg:"):
        field = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_config_field(message, admin_id, field, value)
        return

    # Níveis (meta / pct)
    if awaiting.startswith("lvlset:"):
        # lvlset:name:field
        parts = awaiting.split(":")
        name = parts[1]
        field = parts[2]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_level_field(message, admin_id, name, field, value)
        return


# ============================================================
# DETALHE DO AFILIADO
# ============================================================
async def _render_detail(uid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    u = await _load_user(uid)
    if u is None:
        return None, None

    refs = await _count_referrals(uid)
    totals = await _commission_totals(uid)

    status_emoji = _status_emoji(u.status)
    nivel = str(u.affiliate_level or "iniciante").lower()
    levels = await _level_config()
    nivel_lbl = levels.get(nivel, {}).get("label", nivel)

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"👤 <b>AFILIADO #{u.id}</b>\n\n"
            f"├ Nome: {u.first_name or '—'}\n"
            f"├ Username: @{u.username or '—'}\n"
            f"├ Ativo: {humanize_bool(u.affiliate_active)}\n"
            f"├ Nível: {nivel_lbl}\n"
            f"├ Código: {u.affiliate_code or '—'}\n"
            f"├ Indicações: {refs}\n"
            f"├ Saldo comissões: <b>{fmt_brl(u.affiliate_balance)}</b>\n"
            "├ ─────\n"
            f"├ 🟡 Pendentes: {fmt_brl(totals['pending'])}\n"
            f"├ 🟢 Aprovadas: {fmt_brl(totals['approved'])}\n"
            f"├ ✅ Pagas: {fmt_brl(totals['paid'])}\n"
            f"├ ♻️ Estornos: {fmt_brl(totals['reversed'])}\n"
            f"└ Status: {status_emoji}"
        ),
        id=u.id,
        nome=u.first_name or "—",
        username=u.username or "—",
        ativo=humanize_bool(u.affiliate_active),
        nivel=nivel_lbl,
        codigo=u.affiliate_code or "—",
        ind=refs,
        saldo=fmt_brl(u.affiliate_balance),
        pend=fmt_brl(totals["pending"]),
        aprov=fmt_brl(totals["approved"]),
        pagas=fmt_brl(totals["paid"]),
        estornos=fmt_brl(totals["reversed"]),
        status=status_emoji,
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(u))

    return rendered.content, keyboard


def _default_detail_keyboard(u: User) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = []

    if bool(u.affiliate_active):
        rows.append([b("🔴 Desativar", f"{CB_BLOCK}:{u.id}")])
    else:
        rows.append([b("✅ Aprovar/Ativar", f"{CB_APPROVE}:{u.id}")])

    s = str(getattr(u.status, "value", u.status) or "").upper()
    if s == "BLOCKED":
        rows.append([b("✅ Desbloquear", f"{CB_UNBLOCK}:{u.id}")])
    else:
        rows.append([b("🚫 Bloquear", f"{CB_BLOCK}:{u.id}")])

    rows.extend([
        [b("🏆 Definir Nível", f"{CB_SET_LEVEL}:{u.id}")],
        [b("💸 Pagar Comissões", f"{CB_PAY}:{u.id}")],
        [b("📜 Ver Comissões", f"{CB_COMMS}:{u.id}")],
        [b("🔙 Voltar", f"{CB_PAGE}:all:1")],
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
        text, keyboard = await _render_detail(uid)
        if text is None:
            await toast_err(callback, "❌ Afiliado não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_view falhou: {exc}")


# ============================================================
# APROVAR / BLOQUEAR / DESBLOQUEAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_APPROVE}:"))
async def on_approve(callback: CallbackQuery) -> None:
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
                u.affiliate_active = True
                if not u.affiliate_code:
                    import secrets
                    import string

                    alphabet = string.ascii_uppercase + string.digits
                    u.affiliate_code = "".join(
                        secrets.choice(alphabet) for _ in range(8)
                    )
                if not u.affiliate_level:
                    u.affiliate_level = "iniciante"
                session.add(u)
        except Exception as exc:
            logger.exception(f"approve aff falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="affiliate.approved",
            category="admin",
            details={"user_id": uid},
        )

        text, keyboard = await _render_detail(uid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "✅ Afiliado ativado.")
    except Exception as exc:
        logger.exception(f"affiliates on_approve falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_BLOCK}:"))
async def on_block(callback: CallbackQuery) -> None:
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
                u.affiliate_active = False
                session.add(u)
        except Exception as exc:
            logger.exception(f"block aff falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="affiliate.deactivated",
            category="admin",
            details={"user_id": uid},
        )

        text, keyboard = await _render_detail(uid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔴 Desativado.")
    except Exception as exc:
        logger.exception(f"affiliates on_block falhou: {exc}")


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
            async with session_scope() as session:
                r = await session.execute(select(User).where(User.id == uid))
                u: Optional[User] = r.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return
                if u.status == UserStatus.BLOCKED:
                    u.status = UserStatus.ACTIVE
                session.add(u)
        except Exception as exc:
            logger.exception(f"unblock aff falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="affiliate.unblocked",
            category="admin",
            details={"user_id": uid},
        )

        text, keyboard = await _render_detail(uid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "✅ Desbloqueado.")
    except Exception as exc:
        logger.exception(f"affiliates on_unblock falhou: {exc}")


# ============================================================
# DEFINIR NÍVEL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET_LEVEL}:"))
async def on_set_level(callback: CallbackQuery) -> None:
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

        levels = await _level_config()

        rows_kb = []
        for name, meta in levels.items():
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{meta['label']} · {meta['pct']}%",
                        callback_data=f"admin:affiliates:lvlapply:{uid}:{name}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{uid}")]
        )

        text = "🏆 <b>DEFINIR NÍVEL</b>\n\nEscolha o novo nível:"
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_set_level falhou: {exc}")


@router.callback_query(F.data.startswith("admin:affiliates:lvlapply:"))
async def on_apply_level(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        parts = callback.data.split(":")
        # admin:affiliates:lvlapply:ID:NAME
        uid = int(parts[3])
        name = parts[4].lower()

        levels = await _level_config()
        if name not in levels:
            await toast_err(callback, "❌ Nível inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(User).where(User.id == uid))
                u: Optional[User] = r.scalar_one_or_none()
                if u is None:
                    await toast_err(callback, "❌ Usuário não encontrado.")
                    return
                u.affiliate_level = name
                session.add(u)
        except Exception as exc:
            logger.exception(f"apply level falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="affiliate.level.set",
            category="admin",
            details={"user_id": uid, "level": name},
        )

        text, keyboard = await _render_detail(uid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🏆 Nível {name} aplicado.")
    except Exception as exc:
        logger.exception(f"affiliates on_apply_level falhou: {exc}")


# ============================================================
# PAGAR COMISSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PAY}:"))
async def on_pay(callback: CallbackQuery) -> None:
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

        u = await _load_user(uid)
        if u is None:
            await toast_err(callback, "❌ Usuário não encontrado.")
            return

        totals = await _commission_totals(uid)

        text = (
            f"💸 <b>PAGAR COMISSÕES · #{uid}</b>\n\n"
            f"🟡 Pendentes: <b>{fmt_brl(totals['pending'])}</b>\n"
            f"🟢 Aprovadas: <b>{fmt_brl(totals['approved'])}</b>\n\n"
            "Isso marcará TODAS as comissões pendentes e aprovadas como <b>PAGAS</b>.\n"
            "O saldo de comissões do usuário continuará o mesmo\n"
            "(o saque é feito pelo programa de afiliados).\n\n"
            "Tem certeza?"
        )

        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_PAY_YES}:{uid}",
            cancel_callback=f"{CB_VIEW}:{uid}",
            confirm_text="💸 Sim, pagar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_pay falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAY_YES}:"))
async def on_pay_confirm(callback: CallbackQuery) -> None:
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

        now = datetime.now(timezone.utc)
        paid_total = 0.0
        try:
            async with session_scope() as session:
                # Busca todas pendentes/aprovadas
                res = await session.execute(
                    select(Commission)
                    .where(Commission.earner_id == uid)
                    .where(
                        Commission.status.in_(
                            (CommissionStatus.PENDING, CommissionStatus.APPROVED)
                        )
                    )
                )
                comms = list(res.scalars().all())
                for c in comms:
                    paid_total += float(c.amount or 0)
                    c.status = CommissionStatus.PAID
                    c.paid_at = now
                    session.add(c)
        except Exception as exc:
            logger.exception(f"pay commissions falhou: {exc}")
            await toast_err(callback, "❌ Erro ao pagar.")
            return

        await log_action(
            admin_id=admin_id,
            action="affiliate.paid",
            category="payments",
            details={"user_id": uid, "total": paid_total},
        )

        # Notifica
        try:
            u = await _load_user(uid)
            if u is not None:
                await callback.bot.send_message(
                    chat_id=int(u.telegram_id),
                    text=(
                        f"💸 <b>Comissões pagas!</b>\n\n"
                        f"💵 Total: <b>{fmt_brl(paid_total)}</b>\n"
                        "O valor já está disponível para saque."
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass

        text, keyboard = await _render_detail(uid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"💸 Pago: {fmt_brl(paid_total)}")
    except Exception as exc:
        logger.exception(f"affiliates on_pay_confirm falhou: {exc}")


# ============================================================
# VER COMISSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_COMMS}:"))
async def on_comms(callback: CallbackQuery) -> None:
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
                    .limit(30)
                )
                comms = list(res.scalars().all())
        except Exception:
            comms = []

        if not comms:
            text = "📜 <i>Nenhuma comissão registrada.</i>"
        else:
            lines = [f"📜 <b>COMISSÕES · #{uid}</b>", ""]
            for c in comms:
                st = str(getattr(c.status, "value", c.status)).upper()
                emoji = {
                    "PENDING": SYM_PENDING,
                    "APPROVED": SYM_DONE,
                    "PAID": SYM_DONE,
                    "REVERSED": SYM_WARN,
                }.get(st, "⚪️")
                lines.append(
                    f"{emoji} {fmt_brl(c.amount)} · {st} · "
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
        logger.exception(f"affiliates on_comms falhou: {exc}")


# ============================================================
# NÍVEIS
# ============================================================
@router.callback_query(F.data == CB_LEVELS)
async def on_levels(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        levels = await _level_config()

        lines = ["🏆 <b>NÍVEIS DE AFILIADO</b>", ""]
        for name, meta in levels.items():
            lines.append(
                f"{meta['label']} — meta: <b>{meta['meta']}</b> indicações — "
                f"<b>{meta['pct']}%</b>"
            )
        text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for name, meta in levels.items():
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{meta['label']} · {meta['meta']} · {meta['pct']}%",
                        callback_data=f"{CB_LEVEL_SET}:{name}:menu",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_levels falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_LEVEL_SET}:"))
async def on_level_set(callback: CallbackQuery) -> None:
    """
    Formato:
      admin:affiliates:levelset:name:menu         → mostra opções
      admin:affiliates:levelset:name:meta|pct     → pede input
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:affiliates:levelset:NAME:FIELD
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        name = parts[4].lower()
        field = parts[5].lower()

        if name not in DEFAULT_LEVELS:
            await toast_err(callback, "❌ Nível inválido.")
            return

        if field == "menu":
            levels = await _level_config()
            meta = levels[name]
            rows_kb = [
                [
                    InlineKeyboardButton(
                        text=f"✏️ Meta ({meta['meta']})",
                        callback_data=f"{CB_LEVEL_SET}:{name}:meta",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"✏️ % comissão ({meta['pct']}%)",
                        callback_data=f"{CB_LEVEL_SET}:{name}:pct",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_LEVELS,
                    )
                ],
            ]
            text = (
                f"🏆 <b>{meta['label']}</b>\n\n"
                f"├ Meta: <b>{meta['meta']}</b> indicações\n"
                f"└ Comissão: <b>{meta['pct']}%</b>\n\n"
                "Escolha o campo que deseja alterar:"
            )
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        if field not in ("meta", "pct"):
            await toast_err(callback, "❌ Campo inválido.")
            return

        label = "meta" if field == "meta" else "percentual"
        hint = "Ex.: <code>10</code>" if field == "meta" else "Ex.: <code>15</code>"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"lvlset:{name}:{field}",
            screen=f"affiliates:levelset:{name}:{field}",
            extra={"level": name, "field": field},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_level_set falhou: {exc}")


async def _apply_level_field(
    message: Message,
    admin_id: int,
    name: str,
    field: str,
    value: str,
) -> None:
    if name not in DEFAULT_LEVELS:
        await message.answer("❌ Nível inválido.")
        return

    val = (value or "").strip().replace(",", ".")
    if field == "meta":
        try:
            n = int(val)
            if n < 0:
                raise ValueError
        except Exception:
            await message.answer("❌ Número inválido.")
            return
        key = f"affiliate_level_{name}_meta"
        ok = await _set_setting(key, str(n))
    else:
        try:
            n = float(val)
            if n < 0 or n > 100:
                raise ValueError
        except Exception:
            await message.answer("❌ Percentual inválido (0-100).")
            return
        key = f"affiliate_level_{name}_pct"
        ok = await _set_setting(key, f"{n:g}")

    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"affiliate.level.set.{name}.{field}",
        category="admin",
        details={"level": name, "field": field, "value": val},
    )

    await message.answer(
        f"✅ <b>{DEFAULT_LEVELS[name]['label']} · {field}</b> atualizado.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🏆 Ver Níveis", callback_data=CB_LEVELS)]]
        ),
    )


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

        vals: Dict[str, str] = {}
        for k, meta in CONFIG_FIELDS.items():
            v = await _get_setting(k, meta.get("default"))
            vals[k] = str(v or "")

        rendered = await message_engine.render(
            MSG_KEY_CONFIG,
            fallback=(
                "⚙️ <b>CONFIG AFILIADOS</b>\n\n"
                f"├ % por compra: {vals['affiliate_commission_percent']}%\n"
                f"├ % por depósito: {vals['affiliate_commission_deposit_percent']}%\n"
                f"├ Saque mínimo: R$ {vals['affiliate_min_withdraw']}\n"
                f"└ Programa ativo: "
                f"{'🟢' if vals['affiliate_enabled'] == 'true' else '🔴'}"
            ),
            pct_compra=vals["affiliate_commission_percent"],
            pct_dep=vals["affiliate_commission_deposit_percent"],
            min_saque=vals["affiliate_min_withdraw"],
            ativo="🟢" if vals["affiliate_enabled"] == "true" else "🔴",
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="✏️ % compra",
                    callback_data=f"{CB_CONFIG_SET}:affiliate_commission_percent",
                ),
                InlineKeyboardButton(
                    text="✏️ % depósito",
                    callback_data=f"{CB_CONFIG_SET}:affiliate_commission_deposit_percent",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Saque mínimo",
                    callback_data=f"{CB_CONFIG_SET}:affiliate_min_withdraw",
                ),
                InlineKeyboardButton(
                    text="🔄 Ativar/Desativar",
                    callback_data=f"{CB_CONFIG_SET}:affiliate_enabled",
                ),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_config falhou: {exc}")


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

        if meta["type"] == "bool":
            current = await _get_setting(field, meta.get("default", "true"))
            new_val = "false" if str(current).lower() == "true" else "true"
            ok = await _set_setting(field, new_val)
            if ok:
                await toast_ok(callback, f"🔄 {meta['label']} → {new_val}")
            else:
                await toast_err(callback, "❌ Erro.")
            await on_config(callback)
            return

        hint = "Ex.: <code>10</code>" if meta["type"] == "number" else "Ex.: <code>20.00</code>"
        prompt = (
            f"✏️ <b>EDITAR {meta['label'].upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"affcfg:{field}",
            screen=f"affiliates:config:{field}",
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"affiliates on_config_set falhou: {exc}")


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

    val = (value or "").strip().replace(",", ".")
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    if meta["type"] == "money":
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
        action=f"affiliate.config.set.{field}",
        category="admin",
        details={"field": field, "value": val},
    )

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado para <code>{val}</code>.",
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
    "render_overview",
    "render_list",
    "on_overview",
    "on_page",
    "on_filter",
    "on_search",
    "on_affiliates_text_input",
    "on_view",
    "on_approve",
    "on_block",
    "on_unblock",
    "on_set_level",
    "on_apply_level",
    "on_pay",
    "on_pay_confirm",
    "on_comms",
    "on_levels",
    "on_level_set",
    "on_config",
    "on_config_set",
    "CB_ROOT",
    "CB_OVERVIEW",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_APPROVE",
    "CB_BLOCK",
    "CB_UNBLOCK",
    "CB_PAY",
    "CB_PAY_YES",
    "CB_COMMS",
    "CB_LEVELS",
    "CB_LEVEL_SET",
    "CB_CONFIG",
    "CB_CONFIG_SET",
    "CB_SET_LEVEL",
    "FILTERS",
    "CONFIG_FIELDS",
    "DEFAULT_LEVELS",
]
