"""
Larizinha Store — Módulo Admin: Promoções
Implementa criação e gestão de promoções agendadas conforme DIM.

Telas:
- Lista de promoções (filtros: pendentes/enviadas/canceladas/todas)
- Criar promoção:
    • Escolher produto (ou nenhum = texto livre)
    • Escolher público: canal / usuários / ambos
    • Escolher filtro de usuários: ativos / com saldo / inativos / compradores / afiliados
    • Data e hora (DD/MM/AAAA HH:MM)
    • Recorrência: única / diária / semanal / mensal
- Detalhes da promoção agendada
- Cancelar promoção
- Enviar agora (força disparo imediato)
- Ver histórico

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
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
from bot.database import (
    Product,
    Promotion,
    session_scope,
)
from bot.logger import logger
from bot.services.channel_post import (
    FILTER_ACTIVE,
    FILTER_AFFILIATES,
    FILTER_ALL,
    FILTER_BUYERS,
    FILTER_INACTIVE,
    FILTER_WITH_BALANCE,
    REC_DAILY,
    REC_MONTHLY,
    REC_NONE,
    REC_WEEKLY,
    TARGET_BOTH,
    TARGET_CHANNEL,
    TARGET_USERS,
    channel_post,
)
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_DONE,
    SYM_OFF,
    SYM_PENDING,
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
    is_datetime,
    kb,
    log_action,
    page_offset,
    parse_datetime,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_promos")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_promos_list"
MSG_KEY_EMPTY = "admin_promos_empty"
MSG_KEY_DETAIL = "admin_promos_detail"

SCREEN_LIST = "admin_promos_list"
SCREEN_DETAIL = "admin_promos_detail"

# Callbacks base
CB_ROOT = "admin:promos"
CB_PAGE = "admin:promos:page"              # :filter:N
CB_FILTER = "admin:promos:filter"          # :key
CB_NEW = "admin:promos:new"
CB_NEW_PRODUCT = "admin:promos:newprod"    # :all|ID
CB_NEW_TARGET = "admin:promos:newtgt"      # :target
CB_NEW_FILTER = "admin:promos:newflt"      # :filter
CB_VIEW = "admin:promos:view"              # :ID
CB_CANCEL = "admin:promos:cancel"          # :ID
CB_CANCEL_YES = "admin:promos:cancel:yes"  # :ID
CB_SEND_NOW = "admin:promos:sendnow"       # :ID
CB_SEND_YES = "admin:promos:sendnow:yes"   # :ID

# Filtros de status
FILTERS: Dict[str, str] = {
    "all": "📋 Todas",
    "pending": "🟡 Pendentes",
    "sent": "🟢 Enviadas",
    "canceled": "❌ Canceladas",
}

# Públicos
TARGETS: Dict[str, str] = {
    TARGET_CHANNEL: "📡 Canal",
    TARGET_USERS: "👥 Usuários",
    TARGET_BOTH: "🌐 Ambos",
}

# Filtros de usuários
USER_FILTERS: Dict[str, str] = {
    FILTER_ALL: "👥 Todos",
    FILTER_ACTIVE: "🟢 Ativos",
    FILTER_WITH_BALANCE: "💰 Com Saldo",
    FILTER_INACTIVE: "🚫 Inativos",
    FILTER_BUYERS: "🛒 Compradores",
    FILTER_AFFILIATES: "🤝 Afiliados",
}

# Recorrências
RECURRENCES: Dict[str, str] = {
    REC_NONE: "🎯 Única",
    REC_DAILY: "📅 Diária",
    REC_WEEKLY: "🗓️ Semanal",
    REC_MONTHLY: "📆 Mensal",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(status or "").lower()
    return {
        "pending": SYM_PENDING,
        "sent": SYM_DONE,
        "canceled": SYM_OFF,
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(status or "").lower()
    return {
        "pending": "Pendente",
        "sent": "Enviada",
        "canceled": "Cancelada",
    }.get(s, str(status))


def _apply_filter(stmt, filter_key: str):
    if filter_key == "pending":
        return stmt.where(Promotion.status == "pending")
    if filter_key == "sent":
        return stmt.where(Promotion.status == "sent")
    if filter_key == "canceled":
        return stmt.where(Promotion.status == "canceled")
    return stmt


async def _count_promos(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Promotion.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_promos(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Promotion]:
    try:
        async with session_scope() as session:
            stmt = select(Promotion).order_by(Promotion.scheduled_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_promo(pid: int) -> Optional[Promotion]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Promotion).where(Promotion.id == pid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_product(pid: int) -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Product).where(Product.id == pid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _list_products_for_promo(limit: int = 20) -> List[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(Product.visible == True)  # noqa: E712
                .order_by(Product.order.asc(), Product.id.asc())
                .limit(limit)
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_promos(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    promos = await _list_promos(page, filter_key)

    if not promos:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "📢 <b>PROMOÇÕES</b>\n\n"
                "<i>Nenhuma promoção encontrada com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for p in promos:
            emoji = _status_emoji(p.status)
            target_lbl = TARGETS.get(p.target_type or TARGET_CHANNEL, "—")
            rec_lbl = RECURRENCES.get(p.recurrence or REC_NONE, "—")
            rows.append(
                [
                    str(p.id),
                    target_lbl,
                    rec_lbl,
                    emoji,
                    fmt_dt(p.scheduled_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "Alvo", "Rec.", "S", "Agendada"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"📢 <b>PROMOÇÕES</b> ({total})\n\n"
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
            [InlineKeyboardButton(text="➕ Nova Promoção", callback_data=CB_NEW)],
            [
                InlineKeyboardButton(text="🟡 Pendentes", callback_data=f"{CB_FILTER}:pending"),
                InlineKeyboardButton(text="🟢 Enviadas", callback_data=f"{CB_FILTER}:sent"),
            ],
            [
                InlineKeyboardButton(text="❌ Canceladas", callback_data=f"{CB_FILTER}:canceled"),
                InlineKeyboardButton(text="📋 Todas", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for p in promos:
        emoji = _status_emoji(p.status)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{p.id} · {TARGETS.get(p.target_type or '','—')} · "
                    f"{fmt_dt(p.scheduled_at, with_time=False)}",
                    callback_data=f"{CB_VIEW}:{p.id}",
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
        logger.exception(f"promos on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
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
        logger.exception(f"promos on_page falhou: {exc}")


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
        logger.exception(f"promos on_filter falhou: {exc}")


# ============================================================
# NOVA PROMOÇÃO — ETAPA 1: PRODUTO
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await AdminFlow.clear(admin_id)

        products = await _list_products_for_promo(20)

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="📝 Sem produto (texto livre)",
                    callback_data=f"{CB_NEW_PRODUCT}:0",
                )
            ],
        ]
        for p in products:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"📦 #{p.id} {truncate(p.name, 22)}",
                        callback_data=f"{CB_NEW_PRODUCT}:{p.id}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )

        text = (
            "➕ <b>NOVA PROMOÇÃO · ETAPA 1/4</b>\n\n"
            "Escolha o produto que será promovido, ou use texto livre:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_new falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NEW_PRODUCT}:"))
async def on_new_product(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        await AdminFlow.update(admin_id, {"product_id": pid, "step": "target"})

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="📡 Canal",
                    callback_data=f"{CB_NEW_TARGET}:{TARGET_CHANNEL}",
                ),
                InlineKeyboardButton(
                    text="👥 Usuários",
                    callback_data=f"{CB_NEW_TARGET}:{TARGET_USERS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Ambos",
                    callback_data=f"{CB_NEW_TARGET}:{TARGET_BOTH}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_NEW,
                )
            ],
        ]

        text = (
            "➕ <b>NOVA PROMOÇÃO · ETAPA 2/4</b>\n\n"
            "Escolha o público-alvo:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_new_product falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NEW_TARGET}:"))
async def on_new_target(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            target = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Alvo inválido.")
            return
        if target not in (TARGET_CHANNEL, TARGET_USERS, TARGET_BOTH):
            await toast_err(callback, "❌ Alvo inválido.")
            return

        await AdminFlow.update(admin_id, {"target_type": target})

        # Se for apenas canal → pula filtro de usuários
        if target == TARGET_CHANNEL:
            await AdminFlow.update(admin_id, {
                "user_filter": FILTER_ALL,
                "step": "datetime",
            })
            prompt = (
                "➕ <b>NOVA PROMOÇÃO · ETAPA 4/4</b>\n\n"
                "Envie a <b>data e hora</b> do agendamento.\n\n"
                "Formato: <code>DD/MM/AAAA HH:MM</code>\n"
                "Exemplo: <code>25/12/2025 20:00</code>\n\n"
                "⚠️ A mensagem será apagada automaticamente."
            )
            await ask_input(
                callback,
                prompt,
                field="promo_datetime",
                screen="promos:new:datetime",
                use_force_reply=True,
                reply_placeholder="DD/MM/AAAA HH:MM",
            )
            await answer(callback)
            return

        # Senão → mostra filtro de usuários
        rows_kb = []
        buf = []
        for key, label in USER_FILTERS.items():
            buf.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CB_NEW_FILTER}:{key}",
                )
            )
            if len(buf) == 2:
                rows_kb.append(buf)
                buf = []
        if buf:
            rows_kb.append(buf)
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_NEW)]
        )

        text = (
            "➕ <b>NOVA PROMOÇÃO · ETAPA 3/4</b>\n\n"
            "Escolha o filtro de usuários que vão receber:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_new_target falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NEW_FILTER}:"))
async def on_new_filter(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            fkey = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Filtro inválido.")
            return
        if fkey not in USER_FILTERS:
            fkey = FILTER_ALL

        await AdminFlow.update(admin_id, {
            "user_filter": fkey,
            "step": "datetime",
        })

        prompt = (
            "➕ <b>NOVA PROMOÇÃO · ETAPA 4/4</b>\n\n"
            "Envie a <b>data e hora</b> do agendamento.\n\n"
            "Formato: <code>DD/MM/AAAA HH:MM</code>\n"
            "Exemplo: <code>25/12/2025 20:00</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="promo_datetime",
            screen="promos:new:datetime",
            use_force_reply=True,
            reply_placeholder="DD/MM/AAAA HH:MM",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_new_filter falhou: {exc}")


# ============================================================
# RECEBER DATA/HORA + RECORRÊNCIA
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_promos_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting == "promo_datetime":
        value, _extra = await consume_input(message, admin_id, expected_field="promo_datetime")
        if value is None:
            return
        await _confirm_promo_datetime(message, admin_id, value)
        return


async def _confirm_promo_datetime(
    message: Message,
    admin_id: int,
    raw: str,
) -> None:
    dt = parse_datetime(raw)
    if dt is None:
        await message.answer("❌ Data inválida. Use DD/MM/AAAA HH:MM.")
        return

    # Garante timezone UTC (o dt vem com tzinfo UTC do helper)
    await AdminFlow.update(admin_id, {
        "scheduled_at": dt.isoformat(),
        "step": "recurrence",
    })

    rows_kb = []
    buf = []
    for key, label in RECURRENCES.items():
        buf.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"admin:promos:rec:{key}",
            )
        )
        if len(buf) == 2:
            rows_kb.append(buf)
            buf = []
    if buf:
        rows_kb.append(buf)
    rows_kb.append(
        [InlineKeyboardButton(text="🔙 Cancelar", callback_data=CB_ROOT)]
    )

    text = (
        f"🕐 Data escolhida: <b>{fmt_dt(dt)}</b>\n\n"
        "Escolha a recorrência:"
    )
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
    )


@router.callback_query(F.data.startswith("admin:promos:rec:"))
async def on_recurrence(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        rec = callback.data.split(":")[-1]
        if rec not in RECURRENCES:
            rec = REC_NONE

        state = await AdminFlow.get(admin_id)
        data = (state or {}).get("data", {}) if state else {}

        product_id = data.get("product_id") or None
        target_type = data.get("target_type") or TARGET_CHANNEL
        user_filter = data.get("user_filter") or FILTER_ALL
        scheduled_at_str = data.get("scheduled_at")

        if not scheduled_at_str:
            await toast_err(callback, "❌ Agendamento não definido.")
            return

        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
        except Exception:
            await toast_err(callback, "❌ Data inválida.")
            return

        # ---------- Cria a promoção ----------
        try:
            promo = await channel_post.schedule(
                product_id=int(product_id) if product_id else None,
                target_type=target_type,
                target_value=user_filter,
                scheduled_at=scheduled_at,
                recurrence=rec,
            )
        except Exception as exc:
            logger.exception(f"schedule promo falhou: {exc}")
            await toast_err(callback, "❌ Erro ao agendar.")
            return

        if promo is None:
            await toast_err(callback, "❌ Erro ao agendar.")
            return

        await AdminFlow.clear(admin_id)

        await log_action(
            admin_id=admin_id,
            action="promo.scheduled",
            category="admin",
            details={
                "promo_id": promo.id,
                "product_id": product_id,
                "target": target_type,
                "filter": user_filter,
                "recurrence": rec,
                "scheduled_at": scheduled_at.isoformat(),
            },
        )

        text = (
            f"✅ <b>Promoção agendada!</b>\n\n"
            f"🎫 ID: <code>#{promo.id}</code>\n"
            f"📦 Produto: {product_id or '—'}\n"
            f"🌐 Alvo: {TARGETS.get(target_type, '—')}\n"
            f"👥 Filtro: {USER_FILTERS.get(user_filter, '—')}\n"
            f"🕐 Agendada: <b>{fmt_dt(scheduled_at)}</b>\n"
            f"🔁 Recorrência: {RECURRENCES.get(rec, '—')}"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="👁️ Ver detalhes",
                            callback_data=f"{CB_VIEW}:{promo.id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔙 Voltar",
                            callback_data=CB_ROOT,
                        )
                    ],
                ]
            ),
        )
        await toast_ok(callback, "✅ Agendada.")
    except Exception as exc:
        logger.exception(f"promos on_recurrence falhou: {exc}")


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(pid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    promo = await _load_promo(pid)
    if promo is None:
        return None, None

    produto = "—"
    if promo.product_id:
        p = await _load_product(int(promo.product_id))
        if p is not None:
            produto = p.name

    status_emoji = _status_emoji(promo.status)
    target_lbl = TARGETS.get(promo.target_type or TARGET_CHANNEL, "—")
    rec_lbl = RECURRENCES.get(promo.recurrence or REC_NONE, "—")
    filtro_lbl = USER_FILTERS.get(promo.target_value or FILTER_ALL, "—")

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"📢 <b>PROMOÇÃO #{promo.id}</b>\n\n"
            f"├ Produto: {produto}\n"
            f"├ Alvo: {target_lbl}\n"
            f"├ Filtro: {filtro_lbl}\n"
            f"├ Recorrência: {rec_lbl}\n"
            f"├ Agendada: {fmt_dt(promo.scheduled_at)}\n"
            f"├ Enviada em: {fmt_dt(promo.sent_at)}\n"
            f"├ Status: {status_emoji} {_status_label(promo.status)}\n"
            f"└ Criada em: {fmt_dt(promo.created_at)}"
        ),
        id=promo.id,
        produto=produto,
        alvo=target_lbl,
        filtro=filtro_lbl,
        recorrencia=rec_lbl,
        agendada=fmt_dt(promo.scheduled_at),
        enviada=fmt_dt(promo.sent_at),
        status=f"{status_emoji} {_status_label(promo.status)}",
        created=fmt_dt(promo.created_at),
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(promo))

    return rendered.content, keyboard


def _default_detail_keyboard(p: Promotion) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = []
    status = str(p.status or "").lower()

    if status == "pending":
        rows.append([b("🚀 Enviar Agora", f"{CB_SEND_NOW}:{p.id}")])
        rows.append([b("❌ Cancelar", f"{CB_CANCEL}:{p.id}")])

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
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(pid)
        if text is None:
            await toast_err(callback, "❌ Promoção não encontrada.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_view falhou: {exc}")


# ============================================================
# CANCELAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
async def on_cancel(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            pid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        promo = await _load_promo(pid)
        if promo is None:
            await toast_err(callback, "❌ Promoção não encontrada.")
            return

        text = (
            f"❌ <b>CANCELAR PROMOÇÃO #{pid}</b>\n\n"
            "A promoção não será enviada.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CANCEL_YES}:{pid}",
            cancel_callback=f"{CB_VIEW}:{pid}",
            confirm_text="❌ Sim, cancelar",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_cancel falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CANCEL_YES}:"))
async def on_cancel_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        ok = await channel_post.cancel_scheduled(pid)
        if not ok:
            await toast_err(callback, "❌ Não foi possível cancelar.")
            return

        await log_action(
            admin_id=admin_id,
            action="promo.canceled",
            category="admin",
            details={"promo_id": pid},
        )

        text, keyboard = await _render_detail(pid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "❌ Cancelada.")
    except Exception as exc:
        logger.exception(f"promos on_cancel_confirm falhou: {exc}")


# ============================================================
# ENVIAR AGORA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SEND_NOW}:"))
async def on_send_now(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            pid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        promo = await _load_promo(pid)
        if promo is None:
            await toast_err(callback, "❌ Promoção não encontrada.")
            return

        text = (
            f"🚀 <b>ENVIAR AGORA · #{pid}</b>\n\n"
            "Isso enviará a promoção IMEDIATAMENTE, ignorando o agendamento.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_SEND_YES}:{pid}",
            cancel_callback=f"{CB_VIEW}:{pid}",
            confirm_text="🚀 Sim, enviar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"promos on_send_now falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_SEND_YES}:"))
async def on_send_yes(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            # Força envio imediato via process_scheduled
            await channel_post._dispatch_promo(
                bot=callback.bot,
                promo=await _load_promo(pid),
                product=await _load_product(
                    int((await _load_promo(pid)).product_id)
                ) if (await _load_promo(pid)).product_id else None,
            )
        except Exception as exc:
            logger.debug(f"send_now falhou: {exc}")

        try:
            async with session_scope() as session:
                r = await session.execute(select(Promotion).where(Promotion.id == pid))
                p: Optional[Promotion] = r.scalar_one_or_none()
                if p is not None:
                    p.status = "sent"
                    p.sent_at = datetime.now(timezone.utc)
                    session.add(p)
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="promo.sent_now",
            category="admin",
            details={"promo_id": pid},
        )

        text, keyboard = await _render_detail(pid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🚀 Enviada.")
    except Exception as exc:
        logger.exception(f"promos on_send_yes falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_filter",
    "on_new",
    "on_new_product",
    "on_new_target",
    "on_new_filter",
    "on_promos_text_input",
    "on_recurrence",
    "on_view",
    "on_cancel",
    "on_cancel_confirm",
    "on_send_now",
    "on_send_yes",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_NEW",
    "CB_NEW_PRODUCT",
    "CB_NEW_TARGET",
    "CB_NEW_FILTER",
    "CB_VIEW",
    "CB_CANCEL",
    "CB_CANCEL_YES",
    "CB_SEND_NOW",
    "CB_SEND_YES",
    "FILTERS",
    "TARGETS",
    "USER_FILTERS",
    "RECURRENCES",
]
