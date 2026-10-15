"""
Larizinha Store — Módulo Admin: Disparos (Broadcast)
Implementa envio em massa conforme DIM.

Telas:
- Painel de disparos (fila, progresso, taxa entrega, falhas, campanhas)
- Novo disparo (individual/massa/segmentado)
- Escolher tipo de conteúdo (texto/imagem/vídeo/documento + botões)
- Escolher alvo/filtro (todos, ativos, com saldo, inativos, compradores, afiliados, canal)
- Agendamento (agora/agendar)
- Progresso em tempo real
- Histórico de disparos
- Cancelar disparo em andamento

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Throttling para evitar rate-limit do Telegram.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

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
    Broadcast,
    BroadcastStatus,
    Channel,
    Setting,
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
    SYM_CANCEL,
    SYM_DONE,
    SYM_ERR,
    SYM_PENDING,
    SYM_PROCESSING,
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
router = Router(name="admin_broadcast")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_broadcast_panel"
MSG_KEY_LIST = "admin_broadcast_list"
MSG_KEY_EMPTY = "admin_broadcast_empty"
MSG_KEY_DETAIL = "admin_broadcast_detail"
MSG_KEY_CONFIRM = "admin_broadcast_confirm"

SCREEN_PANEL = "admin_broadcast_panel"
SCREEN_LIST = "admin_broadcast_list"

# Callbacks base
CB_ROOT = "admin:broadcast"
CB_PAGE = "admin:broadcast:page"              # :N
CB_NEW = "admin:broadcast:new"
CB_NEW_TYPE = "admin:broadcast:newtype"       # :text|image|video|document
CB_TARGET = "admin:broadcast:target"          # :target
CB_TARGET_SEL = "admin:broadcast:targetsel"   # :target
CB_WHEN = "admin:broadcast:when"              # :now|later
CB_CONFIRM = "admin:broadcast:confirm"
CB_CANCEL_NEW = "admin:broadcast:cancelnew"
CB_VIEW = "admin:broadcast:view"              # :ID
CB_CANCEL = "admin:broadcast:cancel"          # :ID
CB_CANCEL_YES = "admin:broadcast:cancel:yes"  # :ID
CB_RESUME = "admin:broadcast:resume"          # :ID

# Alvos
TARGET_ALL = "all"
TARGET_ACTIVE = "active"
TARGET_BALANCE = "balance"
TARGET_INACTIVE = "inactive"
TARGET_BUYERS = "buyers"
TARGET_AFFILIATES = "affiliates"
TARGET_CHANNEL = "channel"

TARGETS: Dict[str, str] = {
    TARGET_ALL: "👥 Todos",
    TARGET_ACTIVE: "🟢 Ativos",
    TARGET_BALANCE: "💰 Com Saldo",
    TARGET_INACTIVE: "🚫 Inativos",
    TARGET_BUYERS: "🛒 Compradores",
    TARGET_AFFILIATES: "🤝 Afiliados",
    TARGET_CHANNEL: "📡 Canal",
}

# Tipos de conteúdo
TYPE_TEXT = "text"
TYPE_IMAGE = "image"
TYPE_VIDEO = "video"
TYPE_DOCUMENT = "document"

CONTENT_TYPES: Dict[str, str] = {
    TYPE_TEXT: "📝 Texto",
    TYPE_IMAGE: "🖼️ Imagem",
    TYPE_VIDEO: "🎥 Vídeo",
    TYPE_DOCUMENT: "📄 Documento",
}

# Rate limit (segundos entre envios)
SEND_DELAY = 0.05
BATCH_SIZE = 25
BATCH_DELAY = 1.0


# ============================================================
# 🧰 HELPERS
# ============================================================
def _status_emoji(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": SYM_PENDING,
        "RUNNING": SYM_PROCESSING,
        "DONE": SYM_DONE,
        "CANCELED": SYM_CANCEL,
        "FAILED": SYM_ERR,
    }.get(s, "⚪️")


def _status_label(status: Any) -> str:
    s = str(getattr(status, "value", status) or "").upper()
    return {
        "PENDING": "Pendente",
        "RUNNING": "Enviando",
        "DONE": "Concluído",
        "CANCELED": "Cancelado",
        "FAILED": "Falhou",
    }.get(s, str(status))


async def _load_broadcast(bid: int) -> Optional[Broadcast]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Broadcast).where(Broadcast.id == bid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_broadcasts() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Broadcast.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_broadcasts(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Broadcast]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Broadcast)
                .order_by(Broadcast.created_at.desc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _collect_targets(target: str) -> List[int]:
    """Retorna a lista de telegram_ids para o alvo."""
    try:
        async with session_scope() as session:
            if target == TARGET_CHANNEL:
                res = await session.execute(select(Channel.chat_id))
                return [int(c) for (c,) in res.all() if c]

            stmt = select(User.telegram_id).where(
                User.status == UserStatus.ACTIVE
            )

            if target == TARGET_ACTIVE:
                stmt = stmt.where(User.status == UserStatus.ACTIVE)
            elif target == TARGET_BALANCE:
                stmt = stmt.where(User.balance > 0)
            elif target == TARGET_INACTIVE:
                stmt = stmt.where(User.status != UserStatus.ACTIVE)
            elif target == TARGET_BUYERS:
                stmt = stmt.where(User.purchases_count > 0)
            elif target == TARGET_AFFILIATES:
                stmt = stmt.where(User.affiliate_active == True)  # noqa: E712

            res = await session.execute(stmt)
            return [int(tg) for (tg,) in res.all() if tg]
    except Exception as exc:
        logger.debug(f"_collect_targets falhou: {exc}")
        return []


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
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Estatísticas
    try:
        async with session_scope() as session:
            # Em andamento
            res = await session.execute(
                select(func.count(Broadcast.id)).where(
                    Broadcast.status == BroadcastStatus.RUNNING
                )
            )
            running = int(res.scalar() or 0)

            # Pendentes (agendados)
            res = await session.execute(
                select(func.count(Broadcast.id)).where(
                    Broadcast.status == BroadcastStatus.PENDING
                )
            )
            pending = int(res.scalar() or 0)

            # Total entregues e falhas
            res = await session.execute(
                select(
                    func.coalesce(func.sum(Broadcast.delivered), 0),
                    func.coalesce(func.sum(Broadcast.failed), 0),
                )
            )
            row = res.first()
            total_delivered = int(row[0] or 0)
            total_failed = int(row[1] or 0)

            total_sent = total_delivered + total_failed
            taxa = (
                round((total_delivered / total_sent) * 100, 1)
                if total_sent > 0
                else 100.0
            )
    except Exception:
        running = pending = 0
        total_delivered = total_failed = 0
        taxa = 100.0

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "📨 <b>DISPAROS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Em andamento: <b>{fmt_num(running)}</b>\n"
            f"├ Agendados: <b>{fmt_num(pending)}</b>\n"
            f"├ Total entregues: <b>{fmt_num(total_delivered)}</b>\n"
            f"├ Total falhas: <b>{fmt_num(total_failed)}</b>\n"
            f"└ Taxa de entrega: <b>{taxa}%</b>"
        ),
        fila=fmt_num(running + pending),
        prog="—",
        taxa=taxa,
        falhas=fmt_num(total_failed),
        campanhas=fmt_num(total_delivered),
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="➕ Novo Disparo",
                        callback_data=CB_NEW,
                    ),
                    InlineKeyboardButton(
                        text="📜 Histórico",
                        callback_data=f"{CB_PAGE}:1",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Atualizar",
                        callback_data=CB_ROOT,
                    ),
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data="admin:menu",
                    ),
                ],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data == CB_ROOT)
async def on_panel(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_panel(callback)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_panel falhou: {exc}")


# ============================================================
# HISTÓRICO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
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

        total = await _count_broadcasts()
        total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
        page = max(1, min(page, total_p))

        items = await _list_broadcasts(page)

        if not items:
            rendered = await message_engine.render(
                MSG_KEY_EMPTY,
                fallback=(
                    "📜 <b>HISTÓRICO DE DISPAROS</b>\n\n"
                    "<i>Nenhum disparo registrado.</i>"
                ),
            )
        else:
            rows: List[List[str]] = []
            for b in items:
                emoji = _status_emoji(b.status)
                rows.append(
                    [
                        str(b.id),
                        truncate(b.target_filter or "—", 16),
                        f"{b.delivered or 0}/{b.total or 0}",
                        emoji,
                        fmt_dt(b.created_at, with_time=False),
                    ]
                )

            lista = table(rows, ["ID", "Alvo", "OK/Total", "S", "Data"])
            rendered = await message_engine.render(
                MSG_KEY_LIST,
                fallback=(
                    f"📜 <b>HISTÓRICO DE DISPAROS</b> ({total})\n\n"
                    f"<code>{lista}</code>"
                ),
                total=total,
                lista=lista,
            )

        rows_kb: List[List[InlineKeyboardButton]] = []
        for b in items[:8]:
            emoji = _status_emoji(b.status)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{emoji} #{b.id} · {truncate(b.target_filter or '—', 16)} · "
                        f"{b.delivered or 0}/{b.total or 0}",
                        callback_data=f"{CB_VIEW}:{b.id}",
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

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_history falhou: {exc}")


# ============================================================
# NOVO DISPARO
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await AdminFlow.clear(admin_id)

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="📝 Texto",
                    callback_data=f"{CB_NEW_TYPE}:{TYPE_TEXT}",
                ),
                InlineKeyboardButton(
                    text="🖼️ Imagem",
                    callback_data=f"{CB_NEW_TYPE}:{TYPE_IMAGE}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎥 Vídeo",
                    callback_data=f"{CB_NEW_TYPE}:{TYPE_VIDEO}",
                ),
                InlineKeyboardButton(
                    text="📄 Documento",
                    callback_data=f"{CB_NEW_TYPE}:{TYPE_DOCUMENT}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                )
            ],
        ]

        text = (
            "➕ <b>NOVO DISPARO · PASSO 1/4</b>\n\n"
            "Escolha o tipo de conteúdo:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_new falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NEW_TYPE}:"))
async def on_new_type(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            ctype = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Tipo inválido.")
            return
        if ctype not in CONTENT_TYPES:
            await toast_err(callback, "❌ Tipo inválido.")
            return

        await AdminFlow.update(admin_id, {"content_type": ctype})

        # Pede o conteúdo (texto OU url da mídia)
        if ctype == TYPE_TEXT:
            hint = (
                "Envie o texto do disparo.\n"
                "Aceita HTML: <b>negrito</b>, <i>itálico</i>, <a href='...'>link</a>"
            )
        else:
            hint = (
                f"Envie o link público da mídia.\n"
                f"Ex.: <code>https://i.imgur.com/xxxxx.jpg</code>"
            )

        prompt = (
            f"➕ <b>NOVO DISPARO · PASSO 2/4</b>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="broadcast_content",
            screen="broadcast:new:content",
            extra={"content_type": ctype},
            use_force_reply=True,
            reply_placeholder="Digite aqui...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_new_type falhou: {exc}")


# ============================================================
# RECEBER CONTEÚDO → ESCOLHER ALVO
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_broadcast_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Conteúdo
    if awaiting == "broadcast_content":
        value, _extra = await consume_input(message, admin_id, expected_field="broadcast_content")
        if value is None:
            return
        await _handle_content_received(message, admin_id, value)
        return

    # Agendamento
    if awaiting == "broadcast_when":
        value, _extra = await consume_input(message, admin_id, expected_field="broadcast_when")
        if value is None:
            return
        await _handle_schedule(message, admin_id, value)
        return


async def _handle_content_received(
    message: Message,
    admin_id: int,
    value: str,
) -> None:
    value = (value or "").strip()
    if not value:
        await message.answer("❌ Conteúdo vazio.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    ctype = data.get("content_type", TYPE_TEXT)

    # Valida URL para mídia
    if ctype != TYPE_TEXT:
        if not (value.startswith("http://") or value.startswith("https://")):
            await message.answer("❌ Link inválido (deve começar com https://).")
            return

    await AdminFlow.update(admin_id, {"content": value[:4000]})

    # Escolher alvo
    rows_kb: List[List[InlineKeyboardButton]] = []
    buf: List[InlineKeyboardButton] = []
    for key, label in TARGETS.items():
        buf.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{CB_TARGET}:{key}",
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
        f"➕ <b>NOVO DISPARO · PASSO 3/4</b>\n\n"
        f"✅ Conteúdo recebido.\n\n"
        "Agora escolha o <b>alvo</b>:"
    )
    await message.answer(text=text, parse_mode="HTML", reply_markup=kb(rows_kb))


# ============================================================
# ESCOLHER ALVO → QUANDO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TARGET}:"))
async def on_target(callback: CallbackQuery) -> None:
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
        if target not in TARGETS:
            await toast_err(callback, "❌ Alvo inválido.")
            return

        await AdminFlow.update(admin_id, {"target": target})

        # Conta alvos
        recipients = await _collect_targets(target)
        count = len(recipients)

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="🚀 Enviar Agora",
                    callback_data=f"{CB_WHEN}:now",
                ),
                InlineKeyboardButton(
                    text="📅 Agendar",
                    callback_data=f"{CB_WHEN}:later",
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
            f"➕ <b>NOVO DISPARO · PASSO 4/4</b>\n\n"
            f"🎯 Alvo: <b>{TARGETS[target]}</b>\n"
            f"👥 Destinatários: <b>{fmt_num(count)}</b>\n\n"
            "Escolha quando enviar:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_target falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_WHEN}:"))
async def on_when(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            when = callback.data.split(":")[-1]
        except Exception:
            when = "now"

        if when == "now":
            await _confirm_broadcast(callback, admin_id, scheduled_at=None)
            return

        # Pedir data/hora
        prompt = (
            "📅 <b>AGENDAR DISPARO</b>\n\n"
            "Envie a data e hora no formato:\n"
            "<code>DD/MM/AAAA HH:MM</code>\n\n"
            "Exemplo: <code>25/12/2025 20:00</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="broadcast_when",
            screen="broadcast:new:when",
            use_force_reply=True,
            reply_placeholder="DD/MM/AAAA HH:MM",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_when falhou: {exc}")


async def _handle_schedule(
    message: Message,
    admin_id: int,
    value: str,
) -> None:
    dt = parse_datetime(value)
    if dt is None:
        await message.answer("❌ Data inválida. Use DD/MM/AAAA HH:MM.")
        return

    # Precisa ser no futuro
    if dt <= datetime.now(timezone.utc):
        await message.answer("❌ A data precisa ser no futuro.")
        return

    await _confirm_broadcast(message, admin_id, scheduled_at=dt)


# ============================================================
# CONFIRMAR
# ============================================================
async def _confirm_broadcast(
    target: Union[CallbackQuery, Message],
    admin_id: int,
    scheduled_at: Optional[datetime],
) -> None:
    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}

    ctype = data.get("content_type", TYPE_TEXT)
    content = data.get("content", "")
    target_key = data.get("target", TARGET_ALL)

    recipients = await _collect_targets(target_key)
    count = len(recipients)

    preview = truncate(content, 150)

    when_lbl = "🚀 Agora" if scheduled_at is None else f"📅 {fmt_dt(scheduled_at)}"

    text = (
        "📨 <b>CONFIRMAR DISPARO</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Tipo: {CONTENT_TYPES.get(ctype, ctype)}\n"
        f"🎯 Alvo: {TARGETS.get(target_key, target_key)}\n"
        f"👥 Destinatários: <b>{fmt_num(count)}</b>\n"
        f"🕐 Quando: {when_lbl}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👁️ <b>Preview:</b>\n{preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Confirma o envio?"
    )

    confirm_kb = build_confirm_keyboard(
        confirm_callback=CB_CONFIRM,
        cancel_callback=CB_CANCEL_NEW,
        confirm_text="✅ Enviar",
        cancel_text="❌ Cancelar",
    )

    if isinstance(target, CallbackQuery):
        await edit_or_send(target, text=text, reply_markup=confirm_kb)
        await answer(target)
    else:
        await target.answer(text=text, parse_mode="HTML", reply_markup=confirm_kb)


@router.callback_query(F.data == CB_CONFIRM)
async def on_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        state = await AdminFlow.get(admin_id)
        data = (state or {}).get("data", {}) if state else {}

        ctype = data.get("content_type", TYPE_TEXT)
        content = data.get("content", "")
        target_key = data.get("target", TARGET_ALL)
        scheduled_at_str = data.get("scheduled_at")

        if not content:
            await toast_err(callback, "❌ Conteúdo perdido. Recomece.")
            await AdminFlow.clear(admin_id)
            return

        # Cria o Broadcast no banco
        try:
            async with session_scope() as session:
                b = Broadcast(
                    message_key=None,
                    content={
                        "type": ctype,
                        "text": content if ctype == TYPE_TEXT else "",
                        "url": content if ctype != TYPE_TEXT else "",
                    },
                    target_filter=target_key,
                    status=BroadcastStatus.PENDING,
                    total=0,
                    delivered=0,
                    failed=0,
                    scheduled_at=None,
                    created_by_id=admin_id,
                )
                session.add(b)
                await session.flush()
                bid = int(b.id)
        except Exception as exc:
            logger.exception(f"criar broadcast falhou: {exc}")
            await toast_err(callback, "❌ Erro ao criar disparo.")
            return

        await AdminFlow.clear(admin_id)

        # Envia agora (assíncrono) ou deixa agendado
        if scheduled_at_str:
            # Agenda
            try:
                dt = datetime.fromisoformat(scheduled_at_str)
            except Exception:
                dt = None
            if dt is not None:
                try:
                    async with session_scope() as session:
                        r = await session.execute(
                            select(Broadcast).where(Broadcast.id == bid)
                        )
                        b2 = r.scalar_one_or_none()
                        if b2 is not None:
                            b2.scheduled_at = dt
                            session.add(b2)
                except Exception:
                    pass
                text = (
                    f"✅ <b>Disparo agendado!</b>\n\n"
                    f"🎫 ID: <code>#{bid}</code>\n"
                    f"🕐 Quando: {fmt_dt(dt)}\n\n"
                    "O worker enviará no horário programado."
                )
            else:
                text = (
                    f"✅ <b>Disparo criado!</b>\n\n"
                    f"🎫 ID: <code>#{bid}</code>"
                )
        else:
            # Enviar imediatamente em background
            asyncio.create_task(
                _run_broadcast(callback.bot, bid, ctype, content, target_key)
            )
            text = (
                f"🚀 <b>Disparo iniciado!</b>\n\n"
                f"🎫 ID: <code>#{bid}</code>\n"
                f"🎯 Alvo: {TARGETS.get(target_key, target_key)}\n\n"
                "Use 📜 Histórico para acompanhar o progresso."
            )

        await log_action(
            admin_id=admin_id,
            action="broadcast.created",
            category="admin",
            details={"broadcast_id": bid, "target": target_key, "type": ctype},
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="📜 Ver Histórico",
                            callback_data=f"{CB_PAGE}:1",
                        )
                    ],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
                ]
            ),
        )
    except Exception as exc:
        logger.exception(f"broadcast on_confirm falhou: {exc}")


@router.callback_query(F.data == CB_CANCEL_NEW)
async def on_cancel_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        await AdminFlow.clear(admin_id)
        await render_panel(callback)
        await toast_ok(callback, "❌ Cancelado.")
    except Exception as exc:
        logger.exception(f"broadcast on_cancel_new falhou: {exc}")


# ============================================================
# EXECUÇÃO DO DISPARO
# ============================================================
async def _run_broadcast(
    bot,
    bid: int,
    ctype: str,
    content: str,
    target_key: str,
) -> None:
    """Executa o envio em background."""
    try:
        # Marca como RUNNING
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Broadcast).where(Broadcast.id == bid)
                )
                b = r.scalar_one_or_none()
                if b is None:
                    return
                b.status = BroadcastStatus.RUNNING
                b.started_at = datetime.now(timezone.utc)
                session.add(b)
        except Exception as exc:
            logger.debug(f"marcar running falhou: {exc}")

        recipients = await _collect_targets(target_key)
        total = len(recipients)

        # Atualiza total
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Broadcast).where(Broadcast.id == bid)
                )
                b = r.scalar_one_or_none()
                if b is not None:
                    b.total = total
                    session.add(b)
        except Exception:
            pass

        delivered = 0
        failed = 0

        for i, chat_id in enumerate(recipients):
            try:
                if ctype == TYPE_TEXT:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=content,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                elif ctype == TYPE_IMAGE:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=content,
                        parse_mode="HTML",
                    )
                elif ctype == TYPE_VIDEO:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=content,
                        parse_mode="HTML",
                    )
                elif ctype == TYPE_DOCUMENT:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=content,
                        parse_mode="HTML",
                    )
                delivered += 1
            except Exception as exc:
                failed += 1
                logger.debug(f"disparo falha chat={chat_id}: {exc}")

            # Throttle
            await asyncio.sleep(SEND_DELAY)

            # Atualiza progresso a cada BATCH_SIZE
            if (i + 1) % BATCH_SIZE == 0:
                try:
                    async with session_scope() as session:
                        r = await session.execute(
                            select(Broadcast).where(Broadcast.id == bid)
                        )
                        b = r.scalar_one_or_none()
                        if b is not None:
                            b.delivered = delivered
                            b.failed = failed
                            session.add(b)
                except Exception:
                    pass

                await asyncio.sleep(BATCH_DELAY)

        # Finaliza
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Broadcast).where(Broadcast.id == bid)
                )
                b = r.scalar_one_or_none()
                if b is not None:
                    b.delivered = delivered
                    b.failed = failed
                    b.status = BroadcastStatus.DONE
                    b.finished_at = datetime.now(timezone.utc)
                    session.add(b)
        except Exception as exc:
            logger.debug(f"finalizar broadcast falhou: {exc}")

        logger.info(
            f"Disparo #{bid} finalizado: entregues={delivered} falhas={failed}"
        )
    except Exception as exc:
        logger.exception(f"_run_broadcast falhou: {exc}")


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
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_broadcast(bid)
        if b is None:
            await toast_err(callback, "❌ Disparo não encontrado.")
            return

        total = int(b.total or 0)
        delivered = int(b.delivered or 0)
        failed = int(b.failed or 0)
        processed = delivered + failed
        progress = (
            round((processed / total) * 100, 1) if total > 0 else 0.0
        )
        taxa = (
            round((delivered / processed) * 100, 1) if processed > 0 else 100.0
        )

        content = b.content or {}
        preview_text = ""
        try:
            if isinstance(content, dict):
                preview_text = str(
                    content.get("text") or content.get("url") or ""
                )[:200]
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"📨 <b>DISPARO #{b.id}</b>\n\n"
                f"├ Alvo: {TARGETS.get(b.target_filter or '', b.target_filter or '—')}\n"
                f"├ Status: {_status_emoji(b.status)} {_status_label(b.status)}\n"
                f"├ Progresso: <b>{progress}%</b> "
                f"({processed}/{total})\n"
                f"├ Entregues: <b>{delivered}</b>\n"
                f"├ Falhas: <b>{failed}</b>\n"
                f"├ Taxa: <b>{taxa}%</b>\n"
                f"├ Criado em: {fmt_dt(b.created_at)}\n"
                f"├ Iniciado em: {fmt_dt(b.started_at)}\n"
                f"└ Finalizado em: {fmt_dt(b.finished_at)}\n\n"
                f"👁️ <b>Preview:</b>\n{truncate(preview_text, 150)}"
            ),
            id=b.id,
            alvo=TARGETS.get(b.target_filter or "", b.target_filter or "—"),
            status=f"{_status_emoji(b.status)} {_status_label(b.status)}",
            progresso=progress,
            entregues=fmt_num(delivered),
            falhas=fmt_num(failed),
            taxa=taxa,
            total=fmt_num(total),
            created=fmt_dt(b.created_at),
            started=fmt_dt(b.started_at),
            finished=fmt_dt(b.finished_at),
            preview=truncate(preview_text, 150),
        )

        rows_kb: List[List[InlineKeyboardButton]] = []

        status_str = str(getattr(b.status, "value", b.status)).upper()
        if status_str in ("PENDING", "RUNNING"):
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="🔄 Atualizar",
                        callback_data=f"{CB_VIEW}:{b.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data=f"{CB_CANCEL}:{b.id}",
                    ),
                ]
            )
        else:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="🔄 Atualizar",
                        callback_data=f"{CB_VIEW}:{b.id}",
                    )
                ]
            )

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_PAGE}:1")]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_view falhou: {exc}")


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
            bid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_broadcast(bid)
        if b is None:
            await toast_err(callback, "❌ Disparo não encontrado.")
            return

        status_str = str(getattr(b.status, "value", b.status)).upper()
        if status_str in ("DONE", "CANCELED"):
            await toast_err(callback, "❌ Este disparo já foi finalizado.")
            return

        text = (
            f"❌ <b>CANCELAR DISPARO #{bid}</b>\n\n"
            "Os envios pendentes serão interrompidos.\n"
            "Os já entregues permanecem.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CANCEL_YES}:{bid}",
            cancel_callback=f"{CB_VIEW}:{bid}",
            confirm_text="❌ Sim, cancelar",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"broadcast on_cancel falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CANCEL_YES}:"))
async def on_cancel_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(Broadcast).where(Broadcast.id == bid)
                )
                b = r.scalar_one_or_none()
                if b is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return
                b.status = BroadcastStatus.CANCELED
                b.finished_at = datetime.now(timezone.utc)
                session.add(b)
        except Exception as exc:
            logger.exception(f"cancel broadcast falhou: {exc}")
            await toast_err(callback, "❌ Erro ao cancelar.")
            return

        await log_action(
            admin_id=admin_id,
            action="broadcast.canceled",
            category="admin",
            details={"broadcast_id": bid},
        )

        callback.data = f"{CB_VIEW}:{bid}"  # type: ignore[misc]
        await on_view(callback)
        await toast_ok(callback, "❌ Cancelado.")
    except Exception as exc:
        logger.exception(f"broadcast on_cancel_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_panel",
    "on_history",
    "on_new",
    "on_new_type",
    "on_broadcast_text_input",
    "on_target",
    "on_when",
    "on_confirm",
    "on_cancel_new",
    "on_view",
    "on_cancel",
    "on_cancel_confirm",
    "_run_broadcast",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_NEW_TYPE",
    "CB_TARGET",
    "CB_TARGET_SEL",
    "CB_WHEN",
    "CB_CONFIRM",
    "CB_CANCEL_NEW",
    "CB_VIEW",
    "CB_CANCEL",
    "CB_CANCEL_YES",
    "CB_RESUME",
    "TARGETS",
    "CONTENT_TYPES",
    "TARGET_ALL",
    "TARGET_ACTIVE",
    "TARGET_BALANCE",
    "TARGET_INACTIVE",
    "TARGET_BUYERS",
    "TARGET_AFFILIATES",
    "TARGET_CHANNEL",
]
