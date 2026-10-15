"""
Larizinha Store — Módulo Admin: Editor de Mensagens
Implementa o editor completo de TODAS as mensagens editáveis do bot.

Telas:
- Lista de keys (com busca e paginação DIM)
- Editor individual (conteúdo, mídia, parse_mode, restaurar, versões)
- Preview em tempo real
- Criar nova mensagem
- Excluir
- Histórico de versões

Keys cobertas (conforme DIM):
Bloqueio de canal, boas-vindas, catálogo, detalhe do produto, saldo
insuficiente, comprar mais de um, compra sucesso, perfil, histórico
(vazio/cheio), gift, alterar dados, whatsapp, recarregar, valor, PIX
(gerando/qr/expirado/não pago/confirmado), carrinho abandonado, promoção,
afiliados (inativo/ativo), sacar, chave PIX, confirmação, senha, processando,
saque ok, senha errada, ranking, atendimento, sobre, pesquisar, resultado,
loja webapp, canal (compra/estoque/promo), disparo, comandos (/start /pix
/historico /alertas /afiliados /id /saldo /ranking /termos), admin, etc.

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
from bot.database import MessageTemplate, MessageTemplateVersion, session_scope
from bot.logger import logger
from bot.services.message_engine import (
    ALLOWED_MEDIA_TYPES,
    MEDIA_ANIMATION,
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_VIDEO,
    PARSE_HTML,
    PARSE_MD,
    PARSE_MDV2,
    message_engine,
)
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
router = Router(name="admin_messages")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_messages_list"
MSG_KEY_EMPTY = "admin_messages_empty"
MSG_KEY_EDITOR = "admin_messages_editor"
MSG_KEY_PREVIEW = "admin_messages_preview"
MSG_KEY_VERSIONS = "admin_messages_versions"

SCREEN_LIST = "admin_messages_list"
SCREEN_EDITOR = "admin_messages_editor"
SCREEN_PREVIEW = "admin_messages_preview"

# Callbacks base
CB_ROOT = "admin:messages"
CB_PAGE = "admin:messages:page"           # :filter:N
CB_FILTER = "admin:messages:filter"       # :key
CB_SEARCH = "admin:messages:search"
CB_VIEW = "admin:messages:view"           # :KEY
CB_EDIT = "admin:messages:edit"           # :field:KEY
CB_MEDIA = "admin:messages:media"         # :KEY
CB_MEDIA_SET = "admin:messages:mediaset"  # :KEY:type
CB_PARSE = "admin:messages:parse"         # :KEY
CB_PARSE_SET = "admin:messages:parseset"  # :KEY:mode
CB_PREVIEW = "admin:messages:preview"     # :KEY
CB_RESTORE = "admin:messages:restore"     # :KEY
CB_RESTORE_YES = "admin:messages:restore:yes"  # :KEY
CB_VERSIONS = "admin:messages:versions"   # :KEY
CB_VERSION_VIEW = "admin:messages:ver"    # :KEY:VID
CB_VERSION_RESTORE = "admin:messages:verrestore"  # :KEY:VID
CB_NEW = "admin:messages:new"
CB_DELETE = "admin:messages:delete"       # :KEY
CB_DELETE_YES = "admin:messages:delete:yes"  # :KEY
CB_CLEAR = "admin:messages:clear"         # :KEY

# Filtros de categoria (screen)
FILTERS: Dict[str, str] = {
    "all": "📋 Todas",
    "membro": "👤 Membro",
    "pix": "💠 PIX",
    "afiliados": "👥 Afiliados",
    "saques": "💸 Saques",
    "admin": "🛡️ Admin",
    "canal": "📡 Canal",
    "comandos": "⌨️ Comandos",
    "promo": "📢 Promoção",
}

# Mapa de filtro → prefixos de screen
FILTER_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "all": (),
    "membro": ("membro", "start", "catalog", "profile", "historico", "gift",
               "alterar", "whatsapp", "compra", "comprar", "saldo_", "loja",
               "atendimento", "sobre", "pesquisar", "resultado", "carrinho"),
    "pix": ("pix", "valor", "recarregar", "carrinho"),
    "afiliados": ("afiliados",),
    "saques": ("sacar", "saque", "chave", "senha", "processando", "historico_saque"),
    "admin": ("admin", "msg_"),
    "canal": ("canal_", "post_"),
    "comandos": ("cmd_",),
    "promo": ("promo", "promo_nova", "disparo"),
}

# Campos editáveis
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "content": {"label": "Conteúdo", "validate": "text", "max": 5000},
    "media_url": {"label": "URL da mídia", "validate": "url", "max": 500},
    "media_type": {"label": "Tipo de mídia", "validate": "text", "max": 20},
    "parse_mode": {"label": "Parse mode", "validate": "text", "max": 20},
    "screen": {"label": "Tela (screen)", "validate": "text", "max": 60},
}

# Parse modes disponíveis
PARSE_MODES: Dict[str, str] = {
    PARSE_HTML: "🌐 HTML (padrão)",
    PARSE_MD: "📝 Markdown",
    PARSE_MDV2: "📝 MarkdownV2",
    "NONE": "📄 Sem formatação",
}

# Tipos de mídia
MEDIA_TYPES: Dict[str, str] = {
    "none": "🚫 Sem mídia",
    MEDIA_PHOTO: "🖼️ Foto",
    MEDIA_VIDEO: "🎥 Vídeo",
    MEDIA_DOCUMENT: "📄 Documento",
    MEDIA_ANIMATION: "🎞️ Animação",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _media_label(media_type: Optional[str]) -> str:
    if not media_type:
        return "🚫 Sem mídia"
    return MEDIA_TYPES.get(str(media_type).lower(), f"❓ {media_type}")


async def _load_template(key: str) -> Optional[MessageTemplate]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(MessageTemplate).where(MessageTemplate.key == key)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_templates(filter_key: str = "all") -> int:
    prefixes = FILTER_PREFIXES.get(filter_key, ())
    try:
        async with session_scope() as session:
            stmt = select(func.count(MessageTemplate.id))
            if prefixes:
                conds = []
                for p in prefixes:
                    conds.append(MessageTemplate.key.like(f"{p}%"))
                    conds.append(MessageTemplate.screen.like(f"{p}%"))
                stmt = stmt.where(or_(*conds))
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_templates(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[MessageTemplate]:
    prefixes = FILTER_PREFIXES.get(filter_key, ())
    try:
        async with session_scope() as session:
            stmt = select(MessageTemplate).order_by(
                MessageTemplate.screen.asc(),
                MessageTemplate.key.asc(),
            )
            if prefixes:
                conds = []
                for p in prefixes:
                    conds.append(MessageTemplate.key.like(f"{p}%"))
                    conds.append(MessageTemplate.screen.like(f"{p}%"))
                stmt = stmt.where(or_(*conds))
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_versions(template_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(MessageTemplateVersion.id)).where(
                    MessageTemplateVersion.template_id == template_id
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _invalidate(key: Optional[str] = None) -> None:
    try:
        await message_engine.invalidate(key)
    except Exception:
        pass


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_templates(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    templates = await _list_templates(page, filter_key)

    if not templates:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "✏️ <b>EDITOR DE MENSAGENS</b>\n\n"
                "<i>Nenhuma mensagem encontrada com este filtro.</i>\n\n"
                "Use ➕ Nova para criar."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for t in templates:
            media_lbl = "🖼️" if t.media_type else "—"
            ativo = SYM_OK if t.active else SYM_OFF
            rows.append(
                [
                    truncate(t.key, 22),
                    truncate(t.screen or "—", 16),
                    media_lbl,
                    ativo,
                ]
            )

        lista = table(rows, ["Key", "Screen", "Mídia", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"✏️ <b>EDITOR DE MENSAGENS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="➕ Nova", callback_data=CB_NEW),
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
            ],
            [
                InlineKeyboardButton(text="👤 Membro", callback_data=f"{CB_FILTER}:membro"),
                InlineKeyboardButton(text="💠 PIX", callback_data=f"{CB_FILTER}:pix"),
            ],
            [
                InlineKeyboardButton(text="👥 Afiliados", callback_data=f"{CB_FILTER}:afiliados"),
                InlineKeyboardButton(text="💸 Saques", callback_data=f"{CB_FILTER}:saques"),
            ],
            [
                InlineKeyboardButton(text="🛡️ Admin", callback_data=f"{CB_FILTER}:admin"),
                InlineKeyboardButton(text="📡 Canal", callback_data=f"{CB_FILTER}:canal"),
            ],
            [
                InlineKeyboardButton(text="⌨️ Comandos", callback_data=f"{CB_FILTER}:comandos"),
                InlineKeyboardButton(text="📋 Todas", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for t in templates:
        media_emoji = "🖼️" if t.media_type else "✏️"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{media_emoji} {truncate(t.key, 26)}",
                    callback_data=f"{CB_VIEW}:{t.key}",
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
        logger.exception(f"messages on_root falhou: {exc}")


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
        logger.exception(f"messages on_page falhou: {exc}")


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
        logger.exception(f"messages on_filter falhou: {exc}")


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
            "🔍 <b>BUSCAR MENSAGEM</b>\n\n"
            "Envie a key (ou parte) da mensagem.\n"
            "Ex.: <code>boas_vindas</code> ou <code>pix</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="messages_search_query",
            screen="messages:search",
            use_force_reply=True,
            reply_placeholder="key",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_messages_text_input(message: Message) -> None:
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
    if awaiting == "messages_search_query":
        value, _extra = await consume_input(message, admin_id, expected_field="messages_search_query")
        if value is None:
            return
        await _do_search(message, admin_id, value)
        return

    # Editar campo
    if awaiting.startswith("edit:"):
        # edit:FIELD:KEY
        parts = awaiting.split(":", 2)
        field = parts[1]
        key = parts[2]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, key, field, value)
        return

    # Nova mensagem
    if awaiting == "new_message_key":
        value, _extra = await consume_input(message, admin_id, expected_field="new_message_key")
        if value is None:
            return
        await _create_template_step1(message, admin_id, value)
        return
    if awaiting == "new_message_screen":
        value, _extra = await consume_input(message, admin_id, expected_field="new_message_screen")
        if value is None:
            return
        await _create_template_step2(message, admin_id, value)
        return
    if awaiting == "new_message_content":
        value, _extra = await consume_input(message, admin_id, expected_field="new_message_content")
        if value is None:
            return
        await _create_template_step3(message, admin_id, value)
        return


async def _do_search(message: Message, admin_id: int, query: str) -> None:
    query = (query or "").strip().lower()
    if not query:
        await message.answer("❌ Valor vazio.")
        return

    try:
        async with session_scope() as session:
            res = await session.execute(
                select(MessageTemplate)
                .where(
                    or_(
                        func.lower(MessageTemplate.key).like(f"%{query}%"),
                        func.lower(func.coalesce(MessageTemplate.screen, "")).like(f"%{query}%"),
                    )
                )
                .order_by(MessageTemplate.key.asc())
                .limit(30)
            )
            results = list(res.scalars().all())
    except Exception:
        results = []

    if not results:
        await message.answer(
            text="🔍 <b>NENHUM RESULTADO</b>",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    rows_kb: List[List[InlineKeyboardButton]] = []
    for t in results:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ {truncate(t.key, 30)}",
                    callback_data=f"{CB_VIEW}:{t.key}",
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

    await message.answer(
        text=f"🔍 <b>RESULTADOS ({len(results)})</b>",
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# VIEW / EDITOR
# ============================================================
async def _render_editor(
    key: str,
) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    t = await _load_template(key)
    if t is None:
        return None, None

    versions = await _count_versions(int(t.id))
    preview = await message_engine.preview(key)
    preview_short = truncate(preview or "", 200)

    rendered = await message_engine.render(
        MSG_KEY_EDITOR,
        fallback=(
            f"✏️ <b>EDITAR · {t.key}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 Tela: <code>{t.screen or '—'}</code>\n"
            f"🏷️ Parse mode: <code>{t.parse_mode or 'HTML'}</code>\n"
            f"🖼️ Mídia: {_media_label(t.media_type)}\n"
            f"🔗 URL mídia: {truncate(t.media_url, 40) if t.media_url else '—'}\n"
            f"📌 Ativo: {SYM_OK if t.active else SYM_OFF}\n"
            f"🔢 Versões: {versions}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📄 <b>Conteúdo atual:</b>\n"
            f"<i>{truncate(t.content or '', 300)}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👁️ <b>Preview:</b>\n"
            f"{preview_short}"
        ),
        key=t.key,
        screen=t.screen or "—",
        parse_mode=t.parse_mode or "HTML",
        media=_media_label(t.media_type),
        media_url=truncate(t.media_url, 40) if t.media_url else "—",
        ativo=SYM_OK if t.active else SYM_OFF,
        versoes=versions,
        conteudo=truncate(t.content or "", 300),
        preview=preview_short,
        lista="—",
    )

    engine_kb = await button_engine.inline(SCREEN_EDITOR)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_editor_keyboard(t))

    return rendered.content, keyboard


def _default_editor_keyboard(t: MessageTemplate) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Conteúdo", f"{CB_EDIT}:content:{t.key}")],
        [b("🖼️ Mídia", f"{CB_MEDIA}:{t.key}")],
        [b("🏷️ Parse mode", f"{CB_PARSE}:{t.key}")],
        [b("👁️ Preview", f"{CB_PREVIEW}:{t.key}")],
        [b("📜 Versões", f"{CB_VERSIONS}:{t.key}")],
        [b("♻️ Restaurar original", f"{CB_RESTORE}:{t.key}")],
        [b("🗑️ Limpar conteúdo", f"{CB_CLEAR}:{t.key}")],
        [b("❌ Excluir", f"{CB_DELETE}:{t.key}")],
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
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return
        text, keyboard = await _render_editor(key)
        if text is None:
            await toast_err(callback, "❌ Mensagem não encontrada.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_view falhou: {exc}")


# ============================================================
# EDIT FIELD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EDIT}:"))
async def on_edit_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:messages:edit:field:KEY
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        key = parts[4]

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if field == "content":
            hint = (
                "Você pode usar variáveis dinâmicas:\n"
                "<code>{{nome}}</code>, <code>{{id}}</code>, "
                "<code>{{saldo}}</code>, etc.\n"
                "HTML permitido se parse_mode = HTML."
            )
        elif field == "media_url":
            hint = "Ex.: <code>https://i.imgur.com/xxxxx.jpg</code>"
        else:
            hint = f"Até {meta.get('max', 200)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR · {label.upper()}</b>\n"
            f"🎯 Key: <code>{key}</code>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"edit:{field}:{key}",
            screen=f"messages:edit:{key}",
            extra={"key": key, "field": field},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_edit_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    key: str,
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

    if len(val) > meta.get("max", 5000):
        await message.answer("❌ Texto muito longo.")
        return

    if field == "media_url":
        if not (val.startswith("http://") or val.startswith("https://")):
            await message.answer("❌ URL inválida (deve começar com https://).")
            return

    if field == "parse_mode":
        val = val.upper()
        if val == "NONE":
            val = ""

    try:
        async with session_scope() as session:
            r = await session.execute(
                select(MessageTemplate).where(MessageTemplate.key == key)
            )
            t: Optional[MessageTemplate] = r.scalar_one_or_none()
            if t is None:
                await message.answer("❌ Template não encontrado.")
                return

            # Salva versão antes de alterar
            try:
                session.add(
                    MessageTemplateVersion(
                        template_id=int(t.id),
                        content=t.content or "",
                        created_by_id=admin_id,
                    )
                )
            except Exception:
                pass

            if field == "media_type":
                if val.lower() not in ALLOWED_MEDIA_TYPES:
                    await message.answer("❌ Tipo de mídia inválido.")
                    return
                t.media_type = val.lower()
            elif field == "parse_mode":
                t.parse_mode = val or "HTML"
            else:
                setattr(t, field, val)

            t.version = int(t.version or 1) + 1
            session.add(t)
    except Exception as exc:
        logger.exception(f"_apply_field_value (msg) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await _invalidate(key)

    await log_action(
        admin_id=admin_id,
        action=f"message.set.{field}",
        category="admin",
        details={"key": key, "field": field},
    )

    text, keyboard = await _render_editor(key)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# MÍDIA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_MEDIA}:"))
async def on_media(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        rows_kb: List[List[InlineKeyboardButton]] = []
        buf: List[InlineKeyboardButton] = []
        for mkey, label in MEDIA_TYPES.items():
            buf.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CB_MEDIA_SET}:{key}:{mkey}",
                )
            )
            if len(buf) == 2:
                rows_kb.append(buf)
                buf = []
        if buf:
            rows_kb.append(buf)
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{key}")]
        )

        text = "🖼️ <b>TIPO DE MÍDIA</b>\n\nEscolha o tipo:"
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_media falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_MEDIA_SET}:"))
async def on_media_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:messages:mediaset:KEY:type
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        key = parts[4]
        mtype = parts[5].lower()

        if mtype == "none":
            new_type = None
        elif mtype in ALLOWED_MEDIA_TYPES:
            new_type = mtype
        else:
            await toast_err(callback, "❌ Tipo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                t: Optional[MessageTemplate] = r.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return
                t.media_type = new_type
                if new_type is None:
                    t.media_url = None
                session.add(t)
        except Exception as exc:
            logger.exception(f"media set falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.set.media",
            category="admin",
            details={"key": key, "media_type": new_type},
        )

        # Se tem tipo mas sem URL, pede a URL
        if new_type is not None:
            prompt = (
                "🔗 <b>URL DA MÍDIA</b>\n\n"
                "Envie a URL pública da mídia.\n"
                "Ex.: <code>https://i.imgur.com/xxxxx.jpg</code>\n\n"
                "⚠️ A mensagem será apagada automaticamente."
            )
            await ask_input(
                callback,
                prompt,
                field=f"edit:media_url:{key}",
                screen=f"messages:edit:{key}",
                extra={"key": key, "field": "media_url"},
                use_force_reply=True,
                reply_placeholder="https://...",
            )
            await answer(callback)
            return

        text, keyboard = await _render_editor(key)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🖼️ Mídia atualizada.")
    except Exception as exc:
        logger.exception(f"messages on_media_set falhou: {exc}")


# ============================================================
# PARSE MODE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PARSE}:"))
async def on_parse(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        rows_kb: List[List[InlineKeyboardButton]] = []
        for pm, label in PARSE_MODES.items():
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"{CB_PARSE_SET}:{key}:{pm}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{key}")]
        )

        text = "🏷️ <b>PARSE MODE</b>\n\nEscolha o formato:"
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_parse falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PARSE_SET}:"))
async def on_parse_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        key = parts[4]
        pm = parts[5]

        if pm == "NONE":
            new_pm = "HTML"  # fallback
        else:
            new_pm = pm if pm in (PARSE_HTML, PARSE_MD, PARSE_MDV2) else PARSE_HTML

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                t: Optional[MessageTemplate] = r.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return
                t.parse_mode = new_pm
                session.add(t)
        except Exception as exc:
            logger.exception(f"parse set falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.set.parse_mode",
            category="admin",
            details={"key": key, "parse_mode": new_pm},
        )

        text, keyboard = await _render_editor(key)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🏷️ {new_pm} aplicado.")
    except Exception as exc:
        logger.exception(f"messages on_parse_set falhou: {exc}")


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
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        preview_text = await message_engine.preview(key)

        rendered = await message_engine.render(
            MSG_KEY_PREVIEW,
            fallback=(
                f"👁️ <b>PREVIEW · {key}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{preview_text or '[vazio]'}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            key=key,
            preview=preview_text or "[vazio]",
            conteudo=preview_text or "[vazio]",
        )

        await edit_or_send(
            callback,
            text=rendered.content,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{key}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_preview falhou: {exc}")


# ============================================================
# RESTAURAR / LIMPAR / EXCLUIR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESTORE}:"))
async def on_restore(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        key = parts[-1]

        text = (
            f"♻️ <b>RESTAURAR · {key}</b>\n\n"
            "Isso vai substituir o conteúdo atual pelo texto padrão do bot.\n"
            "Versões anteriores ficam salvas no histórico.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RESTORE_YES}:{key}",
            cancel_callback=f"{CB_VIEW}:{key}",
            confirm_text="♻️ Sim, restaurar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_restore falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RESTORE_YES}:"))
async def on_restore_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        # Restaura para a versão mais antiga (original)
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                t: Optional[MessageTemplate] = r.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return

                # Busca versão mais antiga
                vres = await session.execute(
                    select(MessageTemplateVersion)
                    .where(MessageTemplateVersion.template_id == t.id)
                    .order_by(MessageTemplateVersion.created_at.asc())
                    .limit(1)
                )
                oldest = vres.scalar_one_or_none()
                if oldest is not None:
                    # Salva atual antes
                    session.add(
                        MessageTemplateVersion(
                            template_id=int(t.id),
                            content=t.content or "",
                            created_by_id=admin_id,
                        )
                    )
                    t.content = oldest.content
                    session.add(t)
        except Exception as exc:
            logger.exception(f"restore falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.restored",
            category="admin",
            details={"key": key},
        )

        text, keyboard = await _render_editor(key)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "♻️ Restaurado.")
    except Exception as exc:
        logger.exception(f"messages on_restore_confirm falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CLEAR}:"))
async def on_clear(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                t: Optional[MessageTemplate] = r.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return
                session.add(
                    MessageTemplateVersion(
                        template_id=int(t.id),
                        content=t.content or "",
                        created_by_id=admin_id,
                    )
                )
                t.content = ""
                session.add(t)
        except Exception as exc:
            logger.exception(f"clear falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.cleared",
            category="admin",
            details={"key": key},
        )

        text, keyboard = await _render_editor(key)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🗑️ Conteúdo limpo.")
    except Exception as exc:
        logger.exception(f"messages on_clear falhou: {exc}")


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
        key = parts[-1]

        text = (
            f"❌ <b>EXCLUIR · {key}</b>\n\n"
            "Isso removerá a mensagem do banco. O bot usará o texto\n"
            "padrão interno como fallback.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{key}",
            cancel_callback=f"{CB_VIEW}:{key}",
            confirm_text="❌ Sim, excluir",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                t: Optional[MessageTemplate] = r.scalar_one_or_none()
                if t is None:
                    await toast_err(callback, "❌ Não encontrado.")
                    return
                # Remove versões
                await session.execute(
                    MessageTemplateVersion.__table__.delete().where(
                        MessageTemplateVersion.template_id == t.id
                    )
                )
                await session.execute(
                    MessageTemplate.__table__.delete().where(
                        MessageTemplate.id == t.id
                    )
                )
        except Exception as exc:
            logger.exception(f"delete msg falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.deleted",
            category="admin",
            details={"key": key},
        )

        await render_list(callback, page=1, filter_key="all")
        await toast_ok(callback, "❌ Excluída.")
    except Exception as exc:
        logger.exception(f"messages on_delete_confirm falhou: {exc}")


# ============================================================
# VERSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_VERSIONS}:"))
async def on_versions(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Key inválida.")
            return

        t = await _load_template(key)
        if t is None:
            await toast_err(callback, "❌ Não encontrada.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(MessageTemplateVersion)
                    .where(MessageTemplateVersion.template_id == t.id)
                    .order_by(MessageTemplateVersion.created_at.desc())
                    .limit(20)
                )
                versions = list(res.scalars().all())
        except Exception:
            versions = []

        if not versions:
            text = "📜 <i>Nenhuma versão anterior.</i>"
        else:
            lines = [f"📜 <b>VERSÕES · {key}</b>", ""]
            for v in versions:
                lines.append(
                    f"#{v.id} · {fmt_dt(v.created_at, with_time=False)}\n"
                    f"    <i>{truncate(v.content or '', 60)}</i>"
                )
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for v in versions[:10]:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"📄 #{v.id} · {fmt_dt(v.created_at, with_time=False)}",
                        callback_data=f"{CB_VERSION_VIEW}:{key}:{v.id}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{key}")]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_versions falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_VERSION_VIEW}:"))
async def on_version_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        key = parts[4]
        vid = int(parts[5])

        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(MessageTemplateVersion).where(
                        MessageTemplateVersion.id == vid
                    )
                )
                v: Optional[MessageTemplateVersion] = r.scalar_one_or_none()
        except Exception:
            v = None

        if v is None:
            await toast_err(callback, "❌ Versão não encontrada.")
            return

        text = (
            f"📄 <b>VERSÃO #{vid} · {key}</b>\n"
            f"📅 {fmt_dt(v.created_at)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{truncate(v.content or '', 1200)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="♻️ Restaurar esta versão",
                    callback_data=f"{CB_VERSION_RESTORE}:{key}:{vid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar", callback_data=f"{CB_VERSIONS}:{key}"
                )
            ],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_version_view falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_VERSION_RESTORE}:"))
async def on_version_restore(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        key = parts[4]
        vid = int(parts[5])

        ok = await message_engine.restore_version(key, vid)
        if not ok:
            await toast_err(callback, "❌ Não foi possível restaurar.")
            return

        await _invalidate(key)
        await log_action(
            admin_id=admin_id,
            action="message.version_restored",
            category="admin",
            details={"key": key, "version_id": vid},
        )

        text, keyboard = await _render_editor(key)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "♻️ Versão restaurada.")
    except Exception as exc:
        logger.exception(f"messages on_version_restore falhou: {exc}")


# ============================================================
# CRIAR NOVA MENSAGEM (3 passos)
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVA MENSAGEM · PASSO 1/3</b>\n\n"
            "Envie a <b>key</b> da nova mensagem (única).\n"
            "Ex.: <code>minha_msg_custom</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_message_key",
            screen="messages:new",
            use_force_reply=True,
            reply_placeholder="chave_unica",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"messages on_new falhou: {exc}")


async def _create_template_step1(
    message: Message,
    admin_id: int,
    key: str,
) -> None:
    key = (key or "").strip().lower().replace(" ", "_")
    if not key:
        await message.answer("❌ Key vazia.")
        return
    if len(key) > 60:
        await message.answer("❌ Key muito longa.")
        return

    existing = await _load_template(key)
    if existing is not None:
        await message.answer("❌ Já existe uma mensagem com esta key.")
        return

    await AdminFlow.update(admin_id, {"new_key": key})

    prompt = (
        "➕ <b>NOVA MENSAGEM · PASSO 2/3</b>\n\n"
        "Envie o <b>screen</b> (tela) da mensagem.\n"
        "Ex.: <code>boas_vindas</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="new_message_screen",
        screen="messages:new",
        use_force_reply=True,
        reply_placeholder="screen",
    )


async def _create_template_step2(
    message: Message,
    admin_id: int,
    screen: str,
) -> None:
    screen = (screen or "").strip().lower().replace(" ", "_")
    if not screen:
        await message.answer("❌ Screen vazio.")
        return

    await AdminFlow.update(admin_id, {"new_screen": screen})

    prompt = (
        "➕ <b>NOVA MENSAGEM · PASSO 3/3</b>\n\n"
        "Envie o <b>conteúdo</b> da mensagem.\n"
        "Pode usar <code>{{variaveis}}</code> e HTML.\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="new_message_content",
        screen="messages:new",
        use_force_reply=True,
        reply_placeholder="Conteúdo...",
    )


async def _create_template_step3(
    message: Message,
    admin_id: int,
    content: str,
) -> None:
    content = (content or "").strip()
    if not content:
        await message.answer("❌ Conteúdo vazio.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    key = data.get("new_key")
    screen = data.get("new_screen")

    if not key or not screen:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    try:
        async with session_scope() as session:
            t = MessageTemplate(
                key=key,
                screen=screen,
                content=content,
                parse_mode="HTML",
                version=1,
                active=True,
            )
            session.add(t)
    except Exception as exc:
        logger.exception(f"_create_template_step3 falhou: {exc}")
        await message.answer("❌ Erro ao criar.")
        return

    await _invalidate(key)
    await AdminFlow.clear(admin_id)

    await log_action(
        admin_id=admin_id,
        action="message.created",
        category="admin",
        details={"key": key, "screen": screen},
    )

    await message.answer(
        f"✅ <b>Mensagem criada!</b>\n\n"
        f"🔑 Key: <code>{key}</code>\n"
        f"🎬 Screen: <code>{screen}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="✏️ Editar", callback_data=f"{CB_VIEW}:{key}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_filter",
    "on_search",
    "on_messages_text_input",
    "on_view",
    "on_edit_field",
    "on_media",
    "on_media_set",
    "on_parse",
    "on_parse_set",
    "on_preview",
    "on_restore",
    "on_restore_confirm",
    "on_clear",
    "on_delete",
    "on_delete_confirm",
    "on_versions",
    "on_version_view",
    "on_version_restore",
    "on_new",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_VIEW",
    "CB_EDIT",
    "CB_MEDIA",
    "CB_MEDIA_SET",
    "CB_PARSE",
    "CB_PARSE_SET",
    "CB_PREVIEW",
    "CB_RESTORE",
    "CB_RESTORE_YES",
    "CB_VERSIONS",
    "CB_VERSION_VIEW",
    "CB_VERSION_RESTORE",
    "CB_NEW",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_CLEAR",
    "FILTERS",
    "TEXT_FIELDS",
    "PARSE_MODES",
    "MEDIA_TYPES",
]
