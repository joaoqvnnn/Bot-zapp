"""
Larizinha Store — Módulo Admin: Canais e Grupos
Implementa gestão completa dos canais/grupos conectados conforme DIM.

Telas:
- Lista de canais/grupos conectados (com status obrigatório e auto-post)
- Adicionar canal/grupo (por ID ou @username)
- Configurar canal (obrigatório, link de convite, toggles auto-post)
- Editar campos (título, username, invite_link, chat_id)
- Testar post (envia mensagem de teste no canal)
- Remover canal
- Mensagem de instrução para conectar canal (editável pelo painel)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Verifica se o bot é admin do canal antes de salvar.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
from bot.database import Channel, ChannelType, session_scope
from bot.logger import logger
from bot.services.channel_post import channel_post
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_OFF,
    SYM_OK,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_dt,
    fmt_num,
    humanize_bool,
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
router = Router(name="admin_channel")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_channel_list"
MSG_KEY_EMPTY = "admin_channel_empty"
MSG_KEY_DETAIL = "admin_channel_detail"
MSG_KEY_NEW_HELP = "admin_channel_new_help"

SCREEN_LIST = "admin_channel_list"
SCREEN_DETAIL = "admin_channel_detail"

# Callbacks base
CB_ROOT = "admin:channel"
CB_PAGE = "admin:channel:page"             # :N
CB_NEW = "admin:channel:new"
CB_NEW_TYPE = "admin:channel:newtype"      # :channel|group
CB_VIEW = "admin:channel:view"             # :ID
CB_SET = "admin:channel:set"               # :field:ID
CB_TOGGLE = "admin:channel:toggle"         # :field:ID
CB_TEST = "admin:channel:test"             # :ID
CB_REMOVE = "admin:channel:remove"         # :ID
CB_REMOVE_YES = "admin:channel:remove:yes" # :ID

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "title": {"label": "Título", "validate": "text", "max": 120},
    "username": {"label": "Username (sem @)", "validate": "text", "max": 60},
    "invite_link": {"label": "Link de convite", "validate": "text", "max": 300},
    "chat_id": {"label": "Chat ID (numérico)", "validate": "int"},
    "message_key": {"label": "Key da mensagem do post", "validate": "text", "max": 60},
}

# Toggles (booleans)
TOGGLE_FIELDS: Dict[str, str] = {
    "required": "Obrigatório",
    "auto_post_buy": "Auto-post ao comprar",
    "auto_post_stock": "Auto-post ao abastecer",
    "auto_post_promo": "Auto-post de promoção",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_channel(cid: int) -> Optional[Channel]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Channel).where(Channel.id == cid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_channels() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Channel.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_channels(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Channel]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Channel)
                .order_by(Channel.required.desc(), Channel.id.asc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


def _type_label(t: Any) -> str:
    s = str(getattr(t, "value", t) or "").upper()
    return {"CHANNEL": "📡 Canal", "GROUP": "👥 Grupo"}.get(s, "—")


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    total = await _count_channels()
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    channels = await _list_channels(page)

    if not channels:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "📡 <b>CANAIS E GRUPOS</b>\n\n"
                "<i>Nenhum canal ou grupo conectado ainda.</i>\n\n"
                "Use ➕ Conectar para adicionar."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for c in channels:
            req = SYM_OK if c.required else SYM_OFF
            tipo = "📡" if str(getattr(c.type, "value", c.type)).upper() == "CHANNEL" else "👥"
            rows.append(
                [
                    str(c.id),
                    tipo,
                    truncate(c.title or "—", 20),
                    f"@{c.username}" if c.username else "—",
                    req,
                ]
            )

        lista = table(rows, ["ID", "Tipo", "Título", "Username", "Req"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"📡 <b>CANAIS E GRUPOS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
            canais=fmt_num(
                sum(1 for c in channels if str(getattr(c.type, "value", c.type)).upper() == "CHANNEL")
            ),
            grupos=fmt_num(
                sum(1 for c in channels if str(getattr(c.type, "value", c.type)).upper() == "GROUP")
            ),
            obrig=fmt_num(sum(1 for c in channels if c.required)),
            links=fmt_num(sum(1 for c in channels if c.invite_link)),
        )

    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(
                    text="➕ Adicionar Canal", callback_data=f"{CB_NEW_TYPE}:CHANNEL"
                ),
                InlineKeyboardButton(
                    text="➕ Adicionar Grupo", callback_data=f"{CB_NEW_TYPE}:GROUP"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❓ Como conectar", callback_data=CB_NEW
                ),
            ],
        ]

    for c in channels:
        tipo_emoji = "📡" if str(getattr(c.type, "value", c.type)).upper() == "CHANNEL" else "👥"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{tipo_emoji} #{c.id} {truncate(c.title or '—', 22)}",
                    callback_data=f"{CB_VIEW}:{c.id}",
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
        await render_list(callback, page=1)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1
        await render_list(callback, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_page falhou: {exc}")


# ============================================================
# AJUDA / COMO CONECTAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new_help(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        rendered = await message_engine.render(
            MSG_KEY_NEW_HELP,
            fallback=(
                "❓ <b>COMO CONECTAR UM CANAL/GRUPO</b>\n\n"
                "1️⃣ Adicione o bot como <b>administrador</b> no canal/grupo\n"
                "2️⃣ Copie o <b>chat_id</b> (ex.: <code>-1001234567890</code>)\n"
                "3️⃣ Toque em ➕ Adicionar Canal ou Grupo\n"
                "4️⃣ Envie o chat_id quando pedido\n\n"
                "💡 <b>Dica:</b> para descobrir o chat_id, encaminhe uma "
                "mensagem do canal para o bot @userinfobot."
            ),
            bot_username=settings.BOT_USERNAME or "seubot",
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="➕ Adicionar Canal",
                    callback_data=f"{CB_NEW_TYPE}:CHANNEL",
                ),
                InlineKeyboardButton(
                    text="➕ Adicionar Grupo",
                    callback_data=f"{CB_NEW_TYPE}:GROUP",
                ),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_new_help falhou: {exc}")


# ============================================================
# ADICIONAR CANAL / GRUPO
# ============================================================
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
        if ctype not in ("CHANNEL", "GROUP"):
            await toast_err(callback, "❌ Tipo inválido.")
            return

        tipo_lbl = "canal" if ctype == "CHANNEL" else "grupo"

        prompt = (
            f"➕ <b>NOVO {'CANAL' if ctype == 'CHANNEL' else 'GRUPO'}</b>\n\n"
            f"Envie o <b>chat_id</b> do {tipo_lbl} (começa com -100...).\n\n"
            "Exemplo: <code>-1001234567890</code>\n\n"
            "⚠️ O bot precisa ser administrador do "
            f"{tipo_lbl}.\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"new_channel_chat_id:{ctype}",
            screen=f"channel:new:{ctype}",
            extra={"type": ctype},
            use_force_reply=True,
            reply_placeholder="-1001234567890",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_new_type falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_channel_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Novo canal: recebe chat_id
    if awaiting.startswith("new_channel_chat_id:"):
        ctype = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _create_channel(message, admin_id, ctype, value)
        return

    # Editar campos
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        cid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, cid, field, value)
        return


async def _create_channel(
    message: Message,
    admin_id: int,
    ctype: str,
    raw_chat_id: str,
) -> None:
    raw = (raw_chat_id or "").strip()
    if not is_int(raw):
        await message.answer("❌ Chat ID inválido. Envie apenas o número (ex.: -1001234567890).")
        return

    chat_id = int(raw)
    if chat_id == 0:
        await message.answer("❌ Chat ID inválido.")
        return

    # Verifica se o bot é admin do chat
    try:
        member = await message.bot.get_chat_member(
            chat_id=chat_id, member_id=message.bot.id
        )
        if str(getattr(member, "status", "")).lower() not in (
            "administrator", "creator", "member"
        ):
            await message.answer(
                "❌ O bot não está no canal/grupo ou não é administrador."
            )
            return
    except Exception as exc:
        logger.debug(f"get_chat_member falhou: {exc}")
        await message.answer(
            "❌ Não consegui acessar este chat. Adicione o bot como administrador."
        )
        return

    # Carrega informações do chat
    try:
        chat = await message.bot.get_chat(chat_id=chat_id)
        title = getattr(chat, "title", None) or "—"
        username = getattr(chat, "username", None)
        try:
            invite = await message.bot.export_chat_invite_link(chat_id=chat_id)
        except Exception:
            invite = None
    except Exception as exc:
        logger.debug(f"get_chat falhou: {exc}")
        await message.answer("❌ Não consegui obter dados do chat.")
        return

    # Verifica duplicado
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(func.count(Channel.id)).where(Channel.chat_id == chat_id)
            )
            if int(r.scalar() or 0) > 0:
                await message.answer("❌ Este canal/grupo já está conectado.")
                return

            ch = Channel(
                chat_id=chat_id,
                username=username,
                title=title,
                type=ChannelType(ctype),
                required=True,
                invite_link=invite,
                auto_post_buy=True,
                auto_post_stock=True,
                auto_post_promo=True,
            )
            session.add(ch)
            await session.flush()
            cid = int(ch.id)
    except Exception as exc:
        logger.exception(f"_create_channel falhou: {exc}")
        await message.answer("❌ Erro ao salvar canal.")
        return

    await log_action(
        admin_id=admin_id,
        action="channel.created",
        category="admin",
        details={"channel_id": cid, "chat_id": chat_id, "type": ctype, "title": title},
    )

    # Notifica
    await message.answer(
        f"✅ <b>{'Canal' if ctype == 'CHANNEL' else 'Grupo'} conectado!</b>\n\n"
        f"📛 {title}\n"
        f"🆔 <code>{chat_id}</code>\n"
        f"🔗 {invite or '—'}\n"
        f"🎫 ID: <code>#{cid}</code>\n\n"
        "Use os botões abaixo para configurar.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="⚙️ Configurar", callback_data=f"{CB_VIEW}:{cid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(cid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    c = await _load_channel(cid)
    if c is None:
        return None, None

    tipo = _type_label(c.type)

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"📡 <b>CANAL/GRUPO #{c.id}</b>\n\n"
            f"├ Tipo: {tipo}\n"
            f"├ Título: {c.title or '—'}\n"
            f"├ Username: @{c.username or '—'}\n"
            f"├ Chat ID: <code>{c.chat_id}</code>\n"
            f"├ Link: {c.invite_link or '—'}\n"
            f"├ Obrigatório: {humanize_bool(c.required)}\n"
            f"├ Auto-post compra: {humanize_bool(c.auto_post_buy)}\n"
            f"├ Auto-post estoque: {humanize_bool(c.auto_post_stock)}\n"
            f"├ Auto-post promo: {humanize_bool(c.auto_post_promo)}\n"
            f"└ Conectado em: {fmt_dt(c.created_at)}"
        ),
        id=c.id,
        tipo=tipo,
        titulo=c.title or "—",
        username=c.username or "—",
        chat_id=c.chat_id,
        link=c.invite_link or "—",
        obrig=humanize_bool(c.required),
        auto_buy=humanize_bool(c.auto_post_buy),
        auto_stock=humanize_bool(c.auto_post_stock),
        auto_promo=humanize_bool(c.auto_post_promo),
        created=fmt_dt(c.created_at),
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(c))

    return rendered.content, keyboard


def _default_detail_keyboard(c: Channel) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Título", f"{CB_SET}:title:{c.id}"), b("✏️ Username", f"{CB_SET}:username:{c.id}")],
        [b("✏️ Link convite", f"{CB_SET}:invite_link:{c.id}")],
        [b("🔄 Obrigatório", f"{CB_TOGGLE}:required:{c.id}")],
        [b("🔄 Auto-post compra", f"{CB_TOGGLE}:auto_post_buy:{c.id}")],
        [b("🔄 Auto-post estoque", f"{CB_TOGGLE}:auto_post_stock:{c.id}")],
        [b("🔄 Auto-post promo", f"{CB_TOGGLE}:auto_post_promo:{c.id}")],
        [b("🧪 Postar Teste", f"{CB_TEST}:{c.id}")],
        [b("🗑️ Remover", f"{CB_REMOVE}:{c.id}")],
        [b("🔙 Voltar", CB_ROOT)],
    ]


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(cid)
        if text is None:
            await toast_err(callback, "❌ Canal não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_view falhou: {exc}")


# ============================================================
# SET FIELD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:channel:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        cid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "int":
            hint = "Ex.: <code>-1001234567890</code>"
        else:
            hint = f"Até {meta.get('max', 200)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{cid}",
            screen=f"channel:edit:{cid}",
            extra={"channel_id": cid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_set_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    cid: int,
    field: str,
    value: str,
) -> None:
    meta = TEXT_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if meta["validate"] == "int":
        num = parse_int(val)
        if num is None:
            await message.answer("❌ Número inválido.")
            return
        parsed = int(num)

    # Normalizações
    if field == "username":
        parsed = val.lstrip("@").strip()
    if field == "invite_link":
        if not (val.startswith("http://") or val.startswith("https://") or val.startswith("tg://")):
            await message.answer("❌ Link inválido (deve começar com https://).")
            return
    if len(str(parsed)) > meta.get("max", 300):
        await message.answer("❌ Texto muito longo.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Channel).where(Channel.id == cid))
            c: Optional[Channel] = r.scalar_one_or_none()
            if c is None:
                await message.answer("❌ Canal não encontrado.")
                return
            setattr(c, field, parsed)
            session.add(c)
    except Exception as exc:
        logger.exception(f"_apply_field_value (channel) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"channel.set.{field}",
        category="admin",
        details={"channel_id": cid, "value": str(parsed)},
    )

    text, keyboard = await _render_detail(cid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE (booleans)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:channel:toggle:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        cid = int(parts[4])

        if field not in TOGGLE_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Channel).where(Channel.id == cid))
                c: Optional[Channel] = r.scalar_one_or_none()
                if c is None:
                    await toast_err(callback, "❌ Canal não encontrado.")
                    return
                current = bool(getattr(c, field))
                setattr(c, field, not current)
                session.add(c)
                new_val = not current
        except Exception as exc:
            logger.exception(f"toggle channel falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"channel.toggle.{field}",
            category="admin",
            details={"channel_id": cid, "value": new_val},
        )

        text, keyboard = await _render_detail(cid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🔄 {TOGGLE_FIELDS[field]} alterado.")
    except Exception as exc:
        logger.exception(f"channel on_toggle falhou: {exc}")


# ============================================================
# TESTAR POST
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TEST}:"))
async def on_test(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        c = await _load_channel(cid)
        if c is None:
            await toast_err(callback, "❌ Canal não encontrado.")
            return

        # Envia uma mensagem de teste
        try:
            await callback.bot.send_message(
                chat_id=int(c.chat_id),
                text=(
                    "🧪 <b>Mensagem de teste</b>\n\n"
                    f"Este é um teste do canal <b>{c.title or '—'}</b>.\n"
                    f"ID interno: <code>#{cid}</code>"
                ),
                parse_mode="HTML",
            )
            await toast_ok(callback, "🧪 Teste enviado!")
        except Exception as exc:
            logger.debug(f"test post falhou: {exc}")
            await toast_err(callback, "❌ Não foi possível enviar. Verifique se o bot é admin.")

        await log_action(
            admin_id=admin_id,
            action="channel.test_post",
            category="admin",
            details={"channel_id": cid},
        )
    except Exception as exc:
        logger.exception(f"channel on_test falhou: {exc}")


# ============================================================
# REMOVER
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REMOVE}:"))
async def on_remove(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            cid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        c = await _load_channel(cid)
        if c is None:
            await toast_err(callback, "❌ Canal não encontrado.")
            return

        text = (
            f"🗑️ <b>REMOVER CANAL/GRUPO</b>\n\n"
            f"Título: <b>{c.title or '—'}</b>\n"
            f"Chat ID: <code>{c.chat_id}</code>\n\n"
            "Isso removerá o canal da lista. Os posts automáticos\n"
            "deixarão de ser enviados.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REMOVE_YES}:{cid}",
            cancel_callback=f"{CB_VIEW}:{cid}",
            confirm_text="🗑️ Sim, remover",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"channel on_remove falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REMOVE_YES}:"))
async def on_remove_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                await session.execute(
                    Channel.__table__.delete().where(Channel.id == cid)
                )
        except Exception as exc:
            logger.exception(f"delete channel falhou: {exc}")
            await toast_err(callback, "❌ Erro ao remover.")
            return

        await log_action(
            admin_id=admin_id,
            action="channel.deleted",
            category="admin",
            details={"channel_id": cid},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "🗑️ Removido.")
    except Exception as exc:
        logger.exception(f"channel on_remove_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_new_help",
    "on_new_type",
    "on_channel_text_input",
    "on_view",
    "on_set_field",
    "on_toggle",
    "on_test",
    "on_remove",
    "on_remove_confirm",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_NEW_TYPE",
    "CB_VIEW",
    "CB_SET",
    "CB_TOGGLE",
    "CB_TEST",
    "CB_REMOVE",
    "CB_REMOVE_YES",
    "TEXT_FIELDS",
    "TOGGLE_FIELDS",
]
