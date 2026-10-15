"""
Larizinha Store — Módulo Admin: Editor de Botões
Implementa o editor completo de TODOS os botões editáveis do bot.

Telas:
- Lista de telas (screens) com contagem de botões
- Lista de botões por tela (com posição, tipo, visibilidade)
- Editor individual (texto, emoji, tipo, callback, url, webapp, row, position, order, visible)
- Criar novo botão
- Remover botão
- Toggle visível
- Preview do teclado (inline)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
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
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import ButtonTemplate, session_scope
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import (
    ALL_TYPES as BUTTON_TYPES,
    TYPE_CALLBACK,
    TYPE_COPY,
    TYPE_REPLY,
    TYPE_URL,
    TYPE_WEBAPP,
    button_engine,
    invalidate_button_cache,
)

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
router = Router(name="admin_buttons")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_SCREENS = "admin_buttons_screens"
MSG_KEY_LIST = "admin_buttons_list"
MSG_KEY_EMPTY = "admin_buttons_empty"
MSG_KEY_EDITOR = "admin_buttons_editor"
MSG_KEY_PREVIEW = "admin_buttons_preview"

SCREEN_SCREENS = "admin_buttons_screens"
SCREEN_LIST = "admin_buttons_list"
SCREEN_EDITOR = "admin_buttons_editor"

# Callbacks base
CB_ROOT = "admin:buttons"
CB_SCREEN = "admin:buttons:screen"           # :screen
CB_SCREEN_PAGE = "admin:buttons:spage"       # :screen:N
CB_VIEW = "admin:buttons:view"               # :ID
CB_SET = "admin:buttons:set"                 # :field:ID
CB_TYPE_SET = "admin:buttons:typeset"        # :ID:type
CB_TOGGLE = "admin:buttons:toggle"           # :ID
CB_DELETE = "admin:buttons:delete"           # :ID
CB_DELETE_YES = "admin:buttons:delete:yes"   # :ID
CB_NEW = "admin:buttons:new"                 # :screen
CB_PREVIEW = "admin:buttons:preview"         # :ID
CB_NAV = "admin:buttons:nav"                 # :screen

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "text": {"label": "Texto", "validate": "text", "max": 60},
    "emoji": {"label": "Emoji", "validate": "text", "max": 8},
    "callback_data": {"label": "Callback", "validate": "text", "max": 64},
    "url": {"label": "URL", "validate": "url", "max": 500},
    "webapp_url": {"label": "URL do WebApp", "validate": "url", "max": 500},
    "key": {"label": "Key (única por tela)", "validate": "text", "max": 40},
    "row": {"label": "Linha (row)", "validate": "int"},
    "position": {"label": "Posição", "validate": "int"},
    "order": {"label": "Ordem", "validate": "int"},
}

# Tipos disponíveis
TYPE_LABELS: Dict[str, str] = {
    TYPE_CALLBACK: "🔗 Callback",
    TYPE_URL: "🌐 URL",
    TYPE_WEBAPP: "🖥️ WebApp",
    TYPE_REPLY: "⌨️ Reply",
    TYPE_COPY: "📋 Copiar texto",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _type_label(t: Any) -> str:
    return TYPE_LABELS.get(str(t or "").lower(), f"❓ {t}")


async def _load_button(bid: int) -> Optional[ButtonTemplate]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(ButtonTemplate).where(ButtonTemplate.id == bid)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _list_screens() -> List[Tuple[str, int]]:
    """Retorna lista de (screen, qtd) ordenada por qtd desc."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(ButtonTemplate.screen, func.count(ButtonTemplate.id))
                .group_by(ButtonTemplate.screen)
                .order_by(func.count(ButtonTemplate.id).desc())
            )
            return [(str(s), int(c or 0)) for (s, c) in res.all()]
    except Exception:
        return []


async def _count_buttons(screen: str) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(ButtonTemplate.id)).where(
                    ButtonTemplate.screen == screen
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_buttons(
    screen: str,
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[ButtonTemplate]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(ButtonTemplate)
                .where(ButtonTemplate.screen == screen)
                .order_by(
                    ButtonTemplate.row.asc(),
                    ButtonTemplate.position.asc(),
                    ButtonTemplate.order.asc(),
                )
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _invalidate(screen: Optional[str] = None) -> None:
    try:
        await invalidate_button_cache(screen)
    except Exception:
        pass


# ============================================================
# LISTA DE TELAS
# ============================================================
async def render_screens(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    screens = await _list_screens()

    if not screens:
        rendered = await message_engine.render(
            MSG_KEY_SCREENS,
            fallback=(
                "🔘 <b>EDITOR DE BOTÕES</b>\n\n"
                "<i>Nenhuma tela com botões cadastrados.</i>\n\n"
                "Os botões padrão (fallback hardcoded) continuam funcionando.\n"
                "Para customizar, use uma tela existente e crie botões para ela."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for sc, count in screens:
            rows.append([truncate(sc, 30), str(count)])

        lista = table(rows, ["Screen", "Botões"])
        rendered = await message_engine.render(
            MSG_KEY_SCREENS,
            fallback=(
                f"🔘 <b>EDITOR DE BOTÕES</b>\n\n"
                f"<code>{lista}</code>"
            ),
            lista=lista,
            total=fmt_num(sum(c for _, c in screens)),
            telas=fmt_num(len(screens)),
        )

    rows_kb: List[List[InlineKeyboardButton]] = []

    # Telas conhecidas (mesmo sem botões) — para permitir criar
    known_screens = [
        "boas_vindas", "catalogo", "detalhe_produto", "saldo_insuficiente",
        "perfil", "historico", "gift", "alterar_dados", "recarregar",
        "pix_qr", "pix_expirado", "afiliados", "saques", "ranking",
        "atendimento", "sobre_bot", "pesquisar", "resultado_busca",
        "canal_compra", "canal_estoque", "canal_promo", "admin_menu",
    ]

    shown: set = set()
    for sc, count in screens:
        shown.add(sc)
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"🎬 {truncate(sc, 24)} ({count})",
                    callback_data=f"{CB_SCREEN}:{sc}",
                )
            ]
        )

    # Telas conhecidas sem botões ainda
    missing = [s for s in known_screens if s not in shown]
    for sc in missing[:10]:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"➕ {truncate(sc, 24)}",
                    callback_data=f"{CB_SCREEN}:{sc}",
                )
            ]
        )

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
        await render_screens(callback)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_root falhou: {exc}")


# ============================================================
# LISTA DE BOTÕES POR TELA
# ============================================================
async def render_screen_buttons(
    target: Union[CallbackQuery, Message],
    screen: str,
    *,
    page: int = 1,
) -> Optional[Message]:
    total = await _count_buttons(screen)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    buttons = await _list_buttons(screen, page)

    if not buttons:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                f"🔘 <b>BOTÕES · {screen}</b>\n\n"
                "<i>Nenhum botão cadastrado nesta tela.</i>\n"
                "Use ➕ Novo para criar."
            ),
            screen=screen,
        )
    else:
        rows: List[Sequence[str]] = []
        for b in buttons:
            tipo = TYPE_LABELS.get(str(b.type or "").lower(), str(b.type or "—"))
            vis = SYM_OK if b.visible else SYM_OFF
            rows.append(
                [
                    str(b.id),
                    truncate((b.emoji or "") + " " + (b.text or "—"), 22),
                    truncate(tipo, 14),
                    f"{int(b.row or 0)}/{int(b.position or 0)}",
                    vis,
                ]
            )

        lista = table(rows, ["ID", "Texto", "Tipo", "R/P", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🔘 <b>BOTÕES · {screen}</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            screen=screen,
            total=total,
            lista=lista,
        )

    rows_kb: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Novo Botão",
                callback_data=f"{CB_NEW}:{screen}",
            ),
            InlineKeyboardButton(
                text="👁️ Preview",
                callback_data=f"{CB_PREVIEW}:0:{screen}",
            ),
        ],
    ]

    for b in buttons:
        emoji = b.emoji or ""
        prefix = "✅" if b.visible else "☑️"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {emoji} {truncate(b.text or '—', 24)}",
                    callback_data=f"{CB_VIEW}:{b.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_SCREEN_PAGE}:{screen}",
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
    )

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data.startswith(f"{CB_SCREEN}:"))
async def on_screen(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            # admin:buttons:screen:SCREEN  OR  admin:buttons:screen:SCREEN:PAGE
            parts = callback.data.split(":")
            screen = parts[3]
        except Exception:
            await toast_err(callback, "❌ Tela inválida.")
            return

        await render_screen_buttons(callback, screen, page=1)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_screen falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_SCREEN_PAGE}:"))
async def on_screen_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # admin:buttons:spage:SCREEN:N
        parts = callback.data.split(":")
        screen = parts[3] if len(parts) > 3 else "boas_vindas"
        try:
            page = int(parts[4]) if len(parts) > 4 else 1
        except Exception:
            page = 1
        await render_screen_buttons(callback, screen, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_screen_page falhou: {exc}")


# ============================================================
# DETALHE (EDITOR)
# ============================================================
async def _render_editor(
    bid: int,
) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    b = await _load_button(bid)
    if b is None:
        return None, None

    rendered = await message_engine.render(
        MSG_KEY_EDITOR,
        fallback=(
            f"🔘 <b>BOTÃO #{b.id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 Tela: <code>{b.screen}</code>\n"
            f"🔑 Key: <code>{b.key}</code>\n"
            f"📝 Texto: {b.text or '—'}\n"
            f"😀 Emoji: {b.emoji or '—'}\n"
            f"🏷️ Tipo: {_type_label(b.type)}\n"
            f"🔗 Callback: <code>{b.callback_data or '—'}</code>\n"
            f"🌐 URL: {truncate(b.url, 40) if b.url else '—'}\n"
            f"🖥️ WebApp: {truncate(b.webapp_url, 40) if b.webapp_url else '—'}\n"
            f"📍 Row: {int(b.row or 0)}\n"
            f"📍 Posição: {int(b.position or 0)}\n"
            f"📍 Ordem: {int(b.order or 0)}\n"
            f"👁️ Visível: {humanize_bool(b.visible)}"
        ),
        id=b.id,
        screen=b.screen,
        key=b.key,
        texto=b.text or "—",
        emoji=b.emoji or "—",
        tipo=_type_label(b.type),
        callback=b.callback_data or "—",
        url=truncate(b.url, 40) if b.url else "—",
        webapp=truncate(b.webapp_url, 40) if b.webapp_url else "—",
        row=int(b.row or 0),
        pos=int(b.position or 0),
        ordem=int(b.order or 0),
        visivel=humanize_bool(b.visible),
    )

    engine_kb = await button_engine.inline(SCREEN_EDITOR)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_editor_keyboard(b))

    return rendered.content, keyboard


def _default_editor_keyboard(b: ButtonTemplate) -> List[List[InlineKeyboardButton]]:
    def btn(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [btn("✏️ Texto", f"{CB_SET}:text:{b.id}"), btn("✏️ Emoji", f"{CB_SET}:emoji:{b.id}")],
        [btn("🏷️ Tipo", f"{CB_TYPE_SET}:{b.id}:menu")],
        [btn("🔗 Callback", f"{CB_SET}:callback_data:{b.id}"), btn("🌐 URL", f"{CB_SET}:url:{b.id}")],
        [btn("🖥️ WebApp URL", f"{CB_SET}:webapp_url:{b.id}")],
        [btn("🔑 Key", f"{CB_SET}:key:{b.id}")],
        [btn("📍 Row", f"{CB_SET}:row:{b.id}"), btn("📍 Posição", f"{CB_SET}:position:{b.id}")],
        [btn("📍 Ordem", f"{CB_SET}:order:{b.id}")],
        [btn("👁️ Toggle Visível", f"{CB_TOGGLE}:{b.id}")],
        [btn("👁️ Preview", f"{CB_PREVIEW}:{b.id}:{b.screen}")],
        [btn("🗑️ Remover", f"{CB_DELETE}:{b.id}")],
        [btn("🔙 Voltar", f"{CB_SCREEN}:{b.screen}")],
    ]


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
        text, keyboard = await _render_editor(bid)
        if text is None:
            await toast_err(callback, "❌ Botão não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_view falhou: {exc}")


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
        # admin:buttons:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        bid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "int":
            hint = "Ex.: <code>1</code>"
        elif meta["validate"] == "url":
            hint = "Deve começar com <code>https://</code>"
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
            field=f"set:{field}:{bid}",
            screen=f"buttons:edit:{bid}",
            extra={"button_id": bid, "field": field},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_set_field falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_buttons_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Editar campo
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        bid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, bid, field, value)
        return

    # Novo botão (multi-passo)
    if awaiting.startswith("newbtn_text:"):
        screen = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _create_button_step1(message, admin_id, screen, value)
        return
    if awaiting.startswith("newbtn_key:"):
        screen = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _create_button_step2(message, admin_id, screen, value)
        return
    if awaiting.startswith("newbtn_callback:"):
        screen = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _create_button_step3(message, admin_id, screen, value)
        return


async def _apply_field_value(
    message: Message,
    admin_id: int,
    bid: int,
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
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        parsed = int(num)
    elif meta["validate"] == "url":
        if not (val.startswith("http://") or val.startswith("https://")):
            await message.answer("❌ URL inválida.")
            return

    if len(str(parsed)) > meta.get("max", 500):
        await message.answer("❌ Texto muito longo.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(
                select(ButtonTemplate).where(ButtonTemplate.id == bid)
            )
            b: Optional[ButtonTemplate] = r.scalar_one_or_none()
            if b is None:
                await message.answer("❌ Botão não encontrado.")
                return
            setattr(b, field, parsed)
            session.add(b)
            screen = str(b.screen or "")
    except Exception as exc:
        logger.exception(f"_apply_field_value (button) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await _invalidate(screen)
    await log_action(
        admin_id=admin_id,
        action=f"button.set.{field}",
        category="admin",
        details={"button_id": bid, "value": str(parsed)},
    )

    text, keyboard = await _render_editor(bid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TIPO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TYPE_SET}:"))
async def on_type_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:buttons:typeset:ID:menu|type
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        bid = int(parts[3])
        action = parts[4]

        if action == "menu":
            rows_kb = []
            for tkey, label in TYPE_LABELS.items():
                rows_kb.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=f"{CB_TYPE_SET}:{bid}:{tkey}",
                        )
                    ]
                )
            rows_kb.append(
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{bid}")]
            )
            text = "🏷️ <b>TIPO DO BOTÃO</b>\n\nEscolha o novo tipo:"
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        if action not in BUTTON_TYPES:
            await toast_err(callback, "❌ Tipo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(ButtonTemplate).where(ButtonTemplate.id == bid)
                )
                b: Optional[ButtonTemplate] = r.scalar_one_or_none()
                if b is None:
                    await toast_err(callback, "❌ Botão não encontrado.")
                    return
                b.type = action
                session.add(b)
                screen = str(b.screen or "")
        except Exception as exc:
            logger.exception(f"type set button falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(screen)
        await log_action(
            admin_id=admin_id,
            action="button.set.type",
            category="admin",
            details={"button_id": bid, "type": action},
        )

        text, keyboard = await _render_editor(bid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🏷️ Tipo alterado.")
    except Exception as exc:
        logger.exception(f"buttons on_type_set falhou: {exc}")


# ============================================================
# TOGGLE VISÍVEL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
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
                    select(ButtonTemplate).where(ButtonTemplate.id == bid)
                )
                b: Optional[ButtonTemplate] = r.scalar_one_or_none()
                if b is None:
                    await toast_err(callback, "❌ Botão não encontrado.")
                    return
                b.visible = not bool(b.visible)
                session.add(b)
                new_val = bool(b.visible)
                screen = str(b.screen or "")
        except Exception as exc:
            logger.exception(f"toggle button falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(screen)
        await log_action(
            admin_id=admin_id,
            action="button.toggle",
            category="admin",
            details={"button_id": bid, "visible": new_val},
        )

        text, keyboard = await _render_editor(bid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔄 Alterado.")
    except Exception as exc:
        logger.exception(f"buttons on_toggle falhou: {exc}")


# ============================================================
# PREVIEW
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PREVIEW}:"))
async def on_preview(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # admin:buttons:preview:BID:SCREEN  OR  admin:buttons:preview:0:SCREEN
        parts = callback.data.split(":")
        if len(parts) < 4:
            await toast_err(callback, "❌ Callback inválido.")
            return
        bid = int(parts[3]) if parts[3].isdigit() else 0
        screen = parts[4] if len(parts) > 4 else ""

        # Busca o teclado atual da tela
        try:
            kb_obj = await button_engine.inline(screen) if screen else None
        except Exception:
            kb_obj = None

        if kb_obj is None:
            text = f"👁️ <b>PREVIEW · {screen}</b>\n\n<i>Nenhum botão visível nesta tela.</i>"
        else:
            lines = [f"👁️ <b>PREVIEW · {screen}</b>", ""]
            for row_idx, row in enumerate(kb_obj.inline_keyboard, start=1):
                for btn in row:
                    label = btn.text or "—"
                    cb = getattr(btn, "callback_data", None) or getattr(btn, "url", None) or "—"
                    lines.append(f"📍 Linha {row_idx}: <b>{label}</b> → <code>{cb}</code>")
            text = "\n".join(lines)

        back_cb = f"{CB_VIEW}:{bid}" if bid else f"{CB_SCREEN}:{screen}"
        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=back_cb)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_preview falhou: {exc}")


# ============================================================
# NOVO BOTÃO (3 passos)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_NEW}:"))
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            screen = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Tela inválida.")
            return
        if not screen:
            await toast_err(callback, "❌ Tela vazia.")
            return

        prompt = (
            f"➕ <b>NOVO BOTÃO · {screen}</b>\n"
            "PASSO 1/3\n\n"
            "Envie o <b>texto</b> do botão.\n"
            "Ex.: <code>Comprar Agora</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"newbtn_text:{screen}",
            screen=f"buttons:new:{screen}",
            extra={"screen": screen},
            use_force_reply=True,
            reply_placeholder="Texto do botão",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_new falhou: {exc}")


async def _create_button_step1(
    message: Message,
    admin_id: int,
    screen: str,
    text: str,
) -> None:
    text = (text or "").strip()
    if not text:
        await message.answer("❌ Texto vazio.")
        return
    if len(text) > 60:
        text = text[:60]

    await AdminFlow.update(admin_id, {"newbtn_text": text})

    prompt = (
        f"➕ <b>NOVO BOTÃO · {screen}</b>\n"
        "PASSO 2/3\n\n"
        "Envie a <b>key</b> única (identificador interno).\n"
        "Ex.: <code>btn_comprar</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field=f"newbtn_key:{screen}",
        screen=f"buttons:new:{screen}",
        extra={"screen": screen},
        use_force_reply=True,
        reply_placeholder="chave_unica",
    )


async def _create_button_step2(
    message: Message,
    admin_id: int,
    screen: str,
    key: str,
) -> None:
    key = (key or "").strip().lower().replace(" ", "_")
    if not key:
        await message.answer("❌ Key vazia.")
        return
    if len(key) > 40:
        await message.answer("❌ Key muito longa.")
        return

    # Verifica unicidade
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(func.count(ButtonTemplate.id))
                .where(ButtonTemplate.screen == screen)
                .where(ButtonTemplate.key == key)
            )
            if int(r.scalar() or 0) > 0:
                await message.answer("❌ Já existe um botão com esta key nesta tela.")
                return
    except Exception:
        pass

    await AdminFlow.update(admin_id, {"newbtn_key": key})

    prompt = (
        f"➕ <b>NOVO BOTÃO · {screen}</b>\n"
        "PASSO 3/3\n\n"
        "Envie o <b>callback_data</b> do botão.\n"
        "Ex.: <code>catalog:open</code>\n\n"
        "⚠️ Deixe vazio se for URL. A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field=f"newbtn_callback:{screen}",
        screen=f"buttons:new:{screen}",
        extra={"screen": screen},
        use_force_reply=True,
        reply_placeholder="callback ou vazio",
    )


async def _create_button_step3(
    message: Message,
    admin_id: int,
    screen: str,
    callback_data: str,
) -> None:
    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    text_v = data.get("newbtn_text")
    key_v = data.get("newbtn_key")

    if not text_v or not key_v:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    cb = (callback_data or "").strip() or None

    # Próxima posição/row disponíveis
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(
                    func.coalesce(func.max(ButtonTemplate.row), 0),
                    func.coalesce(func.max(ButtonTemplate.position), 0),
                )
                .where(ButtonTemplate.screen == screen)
            )
            row_max, pos_max = r.first() or (0, 0)
    except Exception:
        row_max, pos_max = 0, 0

    try:
        async with session_scope() as session:
            b = ButtonTemplate(
                screen=screen,
                key=key_v,
                text=text_v,
                type=TYPE_CALLBACK,
                callback_data=cb,
                row=int(row_max or 0) + 1,
                position=int(pos_max or 0) + 1,
                order=0,
                visible=True,
                active=True,
            )
            session.add(b)
            await session.flush()
            bid = int(b.id)
    except Exception as exc:
        logger.exception(f"_create_button_step3 falhou: {exc}")
        await message.answer("❌ Erro ao criar botão.")
        return

    await _invalidate(screen)
    await AdminFlow.clear(admin_id)

    await log_action(
        admin_id=admin_id,
        action="button.created",
        category="admin",
        details={"button_id": bid, "screen": screen, "key": key_v},
    )

    await message.answer(
        f"✅ <b>Botão criado!</b>\n\n"
        f"🎬 Tela: <code>{screen}</code>\n"
        f"🔑 Key: <code>{key_v}</code>\n"
        f"📝 Texto: {text_v}\n"
        f"🎫 ID: <code>#{bid}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="✏️ Editar", callback_data=f"{CB_VIEW}:{bid}")],
                [InlineKeyboardButton(text="🎬 Ver Tela", callback_data=f"{CB_SCREEN}:{screen}")],
            ]
        ),
    )


# ============================================================
# REMOVER
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
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

        b = await _load_button(bid)
        if b is None:
            await toast_err(callback, "❌ Botão não encontrado.")
            return

        text = (
            f"🗑️ <b>REMOVER BOTÃO</b>\n\n"
            f"🎬 Tela: <code>{b.screen}</code>\n"
            f"📝 Texto: {b.text}\n"
            f"🔑 Key: <code>{b.key}</code>\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{bid}",
            cancel_callback=f"{CB_VIEW}:{bid}",
            confirm_text="🗑️ Sim, remover",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"buttons on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
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

        b = await _load_button(bid)
        if b is None:
            await toast_err(callback, "❌ Botão não encontrado.")
            return
        screen = str(b.screen or "")

        try:
            async with session_scope() as session:
                await session.execute(
                    ButtonTemplate.__table__.delete().where(ButtonTemplate.id == bid)
                )
        except Exception as exc:
            logger.exception(f"delete button falhou: {exc}")
            await toast_err(callback, "❌ Erro ao remover.")
            return

        await _invalidate(screen)
        await log_action(
            admin_id=admin_id,
            action="button.deleted",
            category="admin",
            details={"button_id": bid, "screen": screen},
        )

        await render_screen_buttons(callback, screen, page=1)
        await toast_ok(callback, "🗑️ Removido.")
    except Exception as exc:
        logger.exception(f"buttons on_delete_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_screens",
    "render_screen_buttons",
    "on_root",
    "on_screen",
    "on_screen_page",
    "on_view",
    "on_set_field",
    "on_buttons_text_input",
    "on_type_set",
    "on_toggle",
    "on_preview",
    "on_new",
    "on_delete",
    "on_delete_confirm",
    "CB_ROOT",
    "CB_SCREEN",
    "CB_SCREEN_PAGE",
    "CB_VIEW",
    "CB_SET",
    "CB_TYPE_SET",
    "CB_TOGGLE",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_NEW",
    "CB_PREVIEW",
    "CB_NAV",
    "TEXT_FIELDS",
    "TYPE_LABELS",
]
