"""
Larizinha Store — Módulo Admin: Categorias
Implementa CRUD completo com paginação DIM.

Telas:
- Lista de categorias (com filtro e paginação DIM)
- Criar nova categoria (nome → slug automático)
- Editar (nome, slug, descrição, emoji, ordem)
- Ativar/Desativar (visible)
- Excluir com confirmação (remove categoria e desvincula produtos)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import re
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
from bot.database import Category, Product, session_scope
from bot.logger import logger
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
router = Router(name="admin_categories")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_categories_list"
MSG_KEY_EMPTY = "admin_categories_empty"
MSG_KEY_EDIT = "admin_categories_edit"

SCREEN_LIST = "admin_categories_list"
SCREEN_EDIT = "admin_categories_edit"

# Callbacks base
CB_ROOT = "admin:categories"
CB_PAGE = "admin:categories:page"           # :N
CB_FILTER = "admin:categories:filter"       # :all|active|inactive
CB_NEW = "admin:categories:new"
CB_EDIT = "admin:categories:edit"           # :ID
CB_DELETE = "admin:categories:delete"       # :ID
CB_DELETE_CONFIRM = "admin:categories:delete:yes"  # :ID
CB_TOGGLE = "admin:categories:toggle"       # :ID
CB_SET = "admin:categories:set"             # :field:ID

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todas",
    "active": "🟢 Visíveis",
    "inactive": "🔴 Ocultas",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {"label": "Nome", "validate": "text", "max": 80},
    "slug": {"label": "Slug", "validate": "slug", "max": 80},
    "description": {"label": "Descrição", "validate": "text", "max": 500},
    "emoji": {"label": "Emoji", "validate": "text", "max": 8},
    "order": {"label": "Ordem", "validate": "int"},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    s = s.strip("-")
    return s or "categoria"


async def _load_category(cid: int) -> Optional[Category]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Category).where(Category.id == cid))
            return res.scalar_one_or_none()
    except Exception:
        return None


def _apply_filter(stmt, filter_key: str):
    if filter_key == "active":
        return stmt.where(Category.visible == True)  # noqa: E712
    if filter_key == "inactive":
        return stmt.where(Category.visible == False)  # noqa: E712
    return stmt


async def _count_categories(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Category.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_categories(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Category]:
    try:
        async with session_scope() as session:
            stmt = select(Category).order_by(
                Category.order.asc(), Category.id.asc()
            )
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_products_in_category(cid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Product.id)).where(Product.category_id == cid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_categories(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    cats = await _list_categories(page, filter_key)

    if not cats:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🗂️ <b>CATEGORIAS</b>\n\n"
                "<i>Nenhuma categoria encontrada com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for c in cats:
            vis = SYM_OK if c.visible else SYM_OFF
            emoji = c.emoji or "—"
            rows.append(
                [
                    str(c.id),
                    truncate(c.name, 20),
                    truncate(c.slug, 20),
                    emoji,
                    str(int(c.order or 0)),
                    vis,
                ]
            )

        lista = table(rows, ["ID", "Nome", "Slug", "Emoji", "Ordem", "Vis"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🗂️ <b>CATEGORIAS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
        )

    # ---------- Teclado ----------
    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="➕ Nova Categoria", callback_data=CB_NEW),
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Visíveis", callback_data=f"{CB_FILTER}:active"
                ),
                InlineKeyboardButton(
                    text="🔴 Ocultas", callback_data=f"{CB_FILTER}:inactive"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Todas", callback_data=f"{CB_FILTER}:all"
                ),
            ],
        ]

    # Botões por categoria
    for c in cats:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"📝 #{c.id} {truncate(c.name, 20)}",
                    callback_data=f"{CB_EDIT}:{c.id}",
                )
            ]
        )

    # Paginação DIM
    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=CB_PAGE,
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(build_back_row("admin:menu"))

    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=kb(rows_kb),
    )


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
        logger.exception(f"categories on_root falhou: {exc}")


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
        await render_list(callback, page=page, filter_key="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"categories on_page falhou: {exc}")


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
        logger.exception(f"categories on_filter falhou: {exc}")


# ============================================================
# CRIAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVA CATEGORIA</b>\n\n"
            "Envie o <b>nome</b> da nova categoria.\n"
            "O slug será gerado automaticamente.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_category_name",
            screen="categories:new",
            use_force_reply=True,
            reply_placeholder="Nome da categoria...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"categories on_new falhou: {exc}")


async def _create_category(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        await message.answer("❌ Nome inválido.")
        return
    if len(name) > 80:
        name = name[:80]

    slug_base = _slugify(name)

    try:
        async with session_scope() as session:
            # Slug único
            slug = slug_base
            i = 1
            while True:
                r = await session.execute(
                    select(func.count(Category.id)).where(Category.slug == slug)
                )
                if int(r.scalar() or 0) == 0:
                    break
                i += 1
                slug = f"{slug_base}-{i}"

            c = Category(
                name=name,
                slug=slug,
                visible=True,
                order=0,
            )
            session.add(c)
            await session.flush()
            cid = int(c.id)
    except Exception as exc:
        logger.exception(f"_create_category falhou: {exc}")
        await message.answer("❌ Erro ao criar categoria.")
        return

    await log_action(
        admin_id=admin_id,
        action="category.created",
        category="admin",
        details={"category_id": cid, "name": name, "slug": slug_base},
    )

    fresh = await _load_category(cid)
    if fresh is None:
        await message.answer("❌ Erro ao recarregar.")
        return
    text, keyboard = await _render_edit_text_and_kb(fresh)
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ============================================================
# EDITAR — RENDER
# ============================================================
async def _render_edit_text_and_kb(c: Category) -> Tuple[str, InlineKeyboardMarkup]:
    prod_count = await _count_products_in_category(int(c.id))

    rendered = await message_engine.render(
        MSG_KEY_EDIT,
        fallback=(
            f"🗂️ <b>CATEGORIA #{c.id}</b>\n\n"
            f"├ Nome: {c.name}\n"
            f"├ Slug: <code>{c.slug}</code>\n"
            f"├ Descrição: {truncate(c.description, 60)}\n"
            f"├ Emoji: {c.emoji or '—'}\n"
            f"├ Ordem: {int(c.order or 0)}\n"
            f"├ Visível: {humanize_bool(c.visible)}\n"
            f"└ Produtos vinculados: {prod_count}"
        ),
        id=c.id,
        nome=c.name,
        slug=c.slug,
        descricao=truncate(c.description, 60),
        emoji=c.emoji or "—",
        ordem=int(c.order or 0),
        visivel=humanize_bool(c.visible),
        total=prod_count,
    )

    keyboard = await button_engine.inline(SCREEN_EDIT)
    if keyboard is None:
        keyboard = kb(_default_edit_keyboard(int(c.id)))

    return rendered.content, keyboard


def _default_edit_keyboard(cid: int) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Nome", f"{CB_SET}:name:{cid}"), b("✏️ Slug", f"{CB_SET}:slug:{cid}")],
        [b("✏️ Descrição", f"{CB_SET}:description:{cid}"), b("✏️ Emoji", f"{CB_SET}:emoji:{cid}")],
        [b("✏️ Ordem", f"{CB_SET}:order:{cid}"), b("🔄 Visível", f"{CB_TOGGLE}:{cid}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{cid}")],
        [b("🔙 Voltar", CB_ROOT)],
    ]


@router.callback_query(F.data.startswith(f"{CB_EDIT}:"))
async def on_edit(callback: CallbackQuery) -> None:
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
        c = await _load_category(cid)
        if c is None:
            await toast_err(callback, "❌ Categoria não encontrada.")
            return
        text, keyboard = await _render_edit_text_and_kb(c)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"categories on_edit falhou: {exc}")


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
        # admin:categories:set:field:ID
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
            hint = "Ex.: <code>5</code>"
        elif meta["validate"] == "slug":
            hint = "Apenas letras minúsculas, números e hífens"
        else:
            hint = f"Até {meta.get('max', 500)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}",
            screen=f"categories:edit:{cid}",
            extra={"category_id": cid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"categories on_set_field falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_categories_text_input(message: Message) -> None:
    """
    Trata inputs de texto quando o admin está em um campo do módulo categorias.
    """
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # --- Criação ---
    if awaiting == "new_category_name":
        value, _extra = await consume_input(message, admin_id, expected_field="new_category_name")
        if value is None:
            return
        await _create_category(message, admin_id, value)
        return

    # --- Edição de campo ---
    if awaiting.startswith("set:"):
        field = awaiting.split(":", 1)[1]
        value, extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, field, value, extra)
        return


async def _apply_field_value(
    message: Message,
    admin_id: int,
    field: str,
    value: str,
    extra: Dict[str, Any],
) -> None:
    cid = extra.get("category_id")
    if not cid:
        await message.answer("❌ Categoria não identificada.")
        return
    try:
        cid = int(cid)
    except Exception:
        await message.answer("❌ ID inválido.")
        return

    meta = TEXT_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    # Validação
    new_value: Any = None
    if meta["validate"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        new_value = int(num)
    elif meta["validate"] == "slug":
        new_value = _slugify(val)[:80]
    else:
        if len(val) > meta.get("max", 500):
            await message.answer("❌ Texto muito longo.")
            return
        new_value = val

    # Slug único (quando aplicável)
    if field == "slug":
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(func.count(Category.id)).where(
                        Category.slug == new_value,
                        Category.id != cid,
                    )
                )
                if int(r.scalar() or 0) > 0:
                    await message.answer("❌ Este slug já está em uso.")
                    return
        except Exception:
            pass

    # Salva
    try:
        async with session_scope() as session:
            r = await session.execute(select(Category).where(Category.id == cid))
            c: Optional[Category] = r.scalar_one_or_none()
            if c is None:
                await message.answer("❌ Categoria não encontrada.")
                return
            setattr(c, field, new_value)
            session.add(c)
    except Exception as exc:
        logger.exception(f"_apply_field_value (cat) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"category.set.{field}",
        category="admin",
        details={"category_id": cid, "value": str(new_value)},
    )

    fresh = await _load_category(cid)
    if fresh is None:
        await message.answer("❌ Erro ao recarregar.")
        return
    text, keyboard = await _render_edit_text_and_kb(fresh)
    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ============================================================
# TOGGLE VISIBLE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
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
                r = await session.execute(select(Category).where(Category.id == cid))
                c: Optional[Category] = r.scalar_one_or_none()
                if c is None:
                    await toast_err(callback, "❌ Categoria não encontrada.")
                    return
                c.visible = not bool(c.visible)
                session.add(c)
                new_val = bool(c.visible)
        except Exception as exc:
            logger.exception(f"toggle cat falhou: {exc}")
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action="category.toggle.visible",
            category="admin",
            details={"category_id": cid, "value": new_val},
        )

        fresh = await _load_category(cid)
        if fresh is None:
            await toast_err(callback, "❌ Erro ao recarregar.")
            return
        text, keyboard = await _render_edit_text_and_kb(fresh)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔄 Visibilidade alterada.")
    except Exception as exc:
        logger.exception(f"categories on_toggle falhou: {exc}")


# ============================================================
# EXCLUIR (com confirmação)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
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

        c = await _load_category(cid)
        if c is None:
            await toast_err(callback, "❌ Categoria não encontrada.")
            return

        prod_count = await _count_products_in_category(cid)

        warn = ""
        if prod_count > 0:
            warn = (
                f"\n⚠️ <b>Atenção:</b> {prod_count} produto(s) estão "
                "vinculados a esta categoria. Eles serão <b>desvinculados</b>, "
                "não excluídos.\n"
            )

        text = (
            f"🗑️ <b>EXCLUIR CATEGORIA</b>\n\n"
            f"Você tem certeza que deseja excluir <b>{c.name}</b> (#{c.id})?\n"
            f"{warn}\n"
            "Esta ação é irreversível."
        )

        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_CONFIRM}:{cid}",
            cancel_callback=f"{CB_EDIT}:{cid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )

        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"categories on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_CONFIRM}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
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
                # Desvincula produtos
                from sqlalchemy import update as sa_update

                await session.execute(
                    sa_update(Product)
                    .where(Product.category_id == cid)
                    .values(category_id=None)
                )
                # Remove categoria
                await session.execute(
                    Category.__table__.delete().where(Category.id == cid)
                )
        except Exception as exc:
            logger.exception(f"delete categoria falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="category.deleted",
            category="admin",
            details={"category_id": cid},
        )

        await render_list(callback, page=1, filter_key="all")
        await toast_ok(callback, "🗑️ Categoria excluída.")
    except Exception as exc:
        logger.exception(f"categories on_delete_confirm falhou: {exc}")


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
    "on_edit",
    "on_set_field",
    "on_categories_text_input",
    "on_toggle",
    "on_delete",
    "on_delete_confirm",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_NEW",
    "CB_EDIT",
    "CB_DELETE",
    "CB_DELETE_CONFIRM",
    "CB_TOGGLE",
    "CB_SET",
    "TEXT_FIELDS",
    "FILTERS",
]
