"""
Larizinha Store — Módulo Admin: Produtos
Implementa CRUD completo + credenciais em lote.

Telas:
- Lista de produtos com filtros e paginação DIM
- Cadastrar / Editar (todos os campos do DIM)
- Excluir / Ativar-Desativar / Esgotado / Destaque
- Imagens (TG + WebApp), Arquivos, Categoria
- Estoque, Ilimitado, Limite por usuário, Ordem
- Nota produto / nota ativação / link ativação / garantia / vencimento
- Entrega auto / humana / visível
- Credenciais em lote (adicionar em massa, ver, importar CSV, limpar usadas, apagar tudo)
- Postar no Canal

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via Reply/ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import (
    Category,
    Product,
    ProductCredential,
    ProductFile,
    ProductImage,
    session_scope,
)
from bot.logger import logger
from bot.services.channel_post import channel_post
from bot.services.message_engine import message_engine
from bot.services.stock_alerts import invalidate_stock_cache
from bot.keyboards.button_engine import button_engine
from bot.keyboards.reply import build_cancel_reply, build_force_reply, build_reply_keyboard_remove

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
    fmt_brl,
    fmt_num,
    humanize_bool,
    is_int,
    is_number,
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
router = Router(name="admin_products")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_products_list"
MSG_KEY_EMPTY = "admin_products_empty"
MSG_KEY_EDIT = "admin_products_edit"
MSG_KEY_CONFIRM_DELETE = "admin_products_confirm_delete"
MSG_KEY_NEW_PROMPT = "admin_products_new_prompt"
MSG_KEY_CRED_LIST = "admin_credentials_list"
MSG_KEY_CRED_ADD = "admin_credentials_add"
MSG_KEY_CRED_VIEW = "admin_credentials_view"

SCREEN_LIST = "admin_products_list"
SCREEN_EDIT = "admin_products_edit"
SCREEN_CRED = "admin_credentials"

# Callbacks base
CB_ROOT = "admin:products"
CB_PAGE = "admin:products:page"              # :N
CB_FILTER = "admin:products:filter"          # :all|active|inactive|soldout|featured
CB_SEARCH = "admin:products:search"
CB_NEW = "admin:products:new"
CB_EDIT = "admin:products:edit"              # :ID
CB_DELETE = "admin:products:delete"          # :ID
CB_DELETE_CONFIRM = "admin:products:delete:yes"   # :ID
CB_DELETE_CANCEL = "admin:products:delete:no"     # :ID
CB_TOGGLE = "admin:products:toggle"          # :field:ID
CB_SET = "admin:products:set"                # :field:ID
CB_IMG = "admin:products:img"                # :field:ID
CB_FILE = "admin:products:file"              # :ID
CB_CAT = "admin:products:cat"                # :ID
CB_CAT_SET = "admin:products:catset"         # :ID:CATID
CB_CRED = "admin:products:cred"              # :ID
CB_CRED_ADD = "admin:products:cred:add"      # :ID
CB_CRED_VIEW = "admin:products:cred:view"    # :ID
CB_CRED_CSV = "admin:products:cred:csv"      # :ID
CB_CRED_CLEAN = "admin:products:cred:clean"  # :ID
CB_CRED_WIPE = "admin:products:cred:wipe"    # :ID
CB_POST_CHANNEL = "admin:products:postchan"  # :ID

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "active": "🟢 Ativos",
    "inactive": "🔴 Inativos",
    "soldout": "🚫 Esgotados",
    "featured": "⭐ Destaques",
}

# Campos que podem ser editados via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {"label": "Nome", "validate": "text", "max": 120},
    "subtitle": {"label": "Subtítulo", "validate": "text", "max": 180},
    "description": {"label": "Descrição", "validate": "text", "max": 2000},
    "price": {"label": "Preço", "validate": "money"},
    "price_promo": {"label": "Preço Promocional", "validate": "money"},
    "stock": {"label": "Estoque", "validate": "int"},
    "stock_min": {"label": "Estoque Mínimo", "validate": "int"},
    "per_user_limit": {"label": "Limite por usuário", "validate": "int"},
    "order": {"label": "Ordem", "validate": "int"},
    "warranty_days": {"label": "Garantia (dias)", "validate": "int"},
    "expiration_days": {"label": "Vencimento (dias)", "validate": "int"},
    "note_product": {"label": "Nota do produto", "validate": "text", "max": 500},
    "note_activation": {"label": "Nota de ativação", "validate": "text", "max": 500},
    "activation_link": {"label": "Link de ativação", "validate": "text", "max": 300},
}

# Toggles (booleans)
TOGGLE_FIELDS: Dict[str, str] = {
    "unlimited": "Ilimitado",
    "featured": "Destaque",
    "sold_out": "Esgotado",
    "auto_delivery": "Entrega automática",
    "human_delivery": "Entrega humana",
    "visible": "Visível",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_product(pid: int) -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Product).where(Product.id == pid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_products(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Product.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


def _apply_filter(stmt, filter_key: str):
    if filter_key == "active":
        return stmt.where(Product.visible == True)  # noqa: E712
    if filter_key == "inactive":
        return stmt.where(Product.visible == False)  # noqa: E712
    if filter_key == "soldout":
        return stmt.where(Product.sold_out == True)  # noqa: E712
    if filter_key == "featured":
        return stmt.where(Product.featured == True)  # noqa: E712
    return stmt


async def _list_products(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Product]:
    try:
        async with session_scope() as session:
            stmt = select(Product).order_by(
                Product.order.asc(), Product.id.asc()
            )
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_credentials(pid: int) -> Tuple[int, int, int]:
    """Retorna (total, disponíveis, usadas)."""
    try:
        async with session_scope() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count(ProductCredential.id)).where(
                            ProductCredential.product_id == pid
                        )
                    )
                ).scalar()
                or 0
            )
            disp = int(
                (
                    await session.execute(
                        select(func.count(ProductCredential.id)).where(
                            ProductCredential.product_id == pid,
                            ProductCredential.used == False,  # noqa: E712
                        )
                    )
                ).scalar()
                or 0
            )
            return total, disp, max(0, total - disp)
    except Exception:
        return (0, 0, 0)


async def _list_categories() -> List[Category]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Category).order_by(Category.order.asc(), Category.id.asc())
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
    total = await _count_products(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    products = await _list_products(page, filter_key)

    if not products:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "📦 <b>PRODUTOS</b>\n\n"
                "<i>Nenhum produto encontrado com este filtro.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for p in products:
            preco = float(p.price_promo or p.price or 0)
            estoque = "∞" if p.unlimited else str(int(p.stock or 0))
            vis = SYM_OK if p.visible else SYM_OFF
            rows.append(
                [
                    str(p.id),
                    truncate(p.name, 20),
                    fmt_brl(preco),
                    estoque,
                    vis,
                ]
            )

        lista = table(rows, ["ID", "Nome", "Preço", "Estoque", "Vis"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"📦 <b>PRODUTOS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="➕ Cadastrar", callback_data=CB_NEW),
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Ativos", callback_data=f"{CB_FILTER}:active"
                ),
                InlineKeyboardButton(
                    text="🔴 Inativos", callback_data=f"{CB_FILTER}:inactive"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Destaques", callback_data=f"{CB_FILTER}:featured"
                ),
                InlineKeyboardButton(
                    text="🚫 Esgotados", callback_data=f"{CB_FILTER}:soldout"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 Todos", callback_data=f"{CB_FILTER}:all"
                ),
            ],
        ]

        # Botões por produto (até 8)
        for p in products:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"📝 #{p.id} {truncate(p.name, 20)}",
                        callback_data=f"{CB_EDIT}:{p.id}",
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

    reply_markup = kb(rows_kb)

    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=reply_markup,
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
        logger.exception(f"products on_root falhou: {exc}")


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
        logger.exception(f"products on_page falhou: {exc}")


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
        logger.exception(f"products on_filter falhou: {exc}")


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
            "🔍 <b>BUSCAR PRODUTO</b>\n\n"
            "Envie o nome (ou parte dele) do produto que deseja localizar.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="search_query",
            screen="products:search",
            use_force_reply=True,
            reply_placeholder="Nome do produto...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_products_text_input(message: Message) -> None:
    """
    Intercepta inputs de texto quando o admin está em um campo do módulo produtos.
    Também trata o input de busca.
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

    # --- Busca ---
    if awaiting == "search_query":
        value, _extra = await consume_input(message, admin_id, expected_field="search_query")
        if value is None:
            return
        await _do_search(message, admin_id, value)
        return

    # --- Campos de edição (set) ---
    if awaiting.startswith("set:"):
        field = awaiting.split(":", 1)[1]
        value, extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, field, value, extra)
        return

    # --- Credenciais em massa ---
    if awaiting.startswith("cred_add:"):
        pid_str = awaiting.split(":", 1)[1]
        value, extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        try:
            pid = int(pid_str)
        except Exception:
            return
        await _add_credentials_bulk(message, admin_id, pid, value, extra)
        return

    # --- Novo produto (nome) ---
    if awaiting == "new_product_name":
        value, _extra = await consume_input(message, admin_id, expected_field="new_product_name")
        if value is None:
            return
        await _create_product(message, admin_id, value)
        return


async def _do_search(message: Message, admin_id: int, query: str) -> None:
    like = f"%{query.strip().lower()}%"
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(
                    or_(
                        func.lower(Product.name).like(like),
                        func.lower(func.coalesce(Product.subtitle, "")).like(like),
                    )
                )
                .order_by(Product.id.asc())
                .limit(20)
            )
            products = list(res.scalars().all())
    except Exception:
        products = []

    if not products:
        text = (
            f"🔍 <b>NENHUM RESULTADO</b>\n\n"
            f"Busca: <code>{query}</code>"
        )
        rows_kb = [
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        ]
    else:
        lines = [f"🔍 <b>RESULTADO PARA</b> <code>{query}</code>", ""]
        rows_kb = []
        for p in products:
            lines.append(f"📦 #{p.id} · <b>{p.name}</b>")
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"📝 #{p.id} {truncate(p.name, 20)}",
                        callback_data=f"{CB_EDIT}:{p.id}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )
        text = "\n".join(lines)

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# CADASTRAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVO PRODUTO</b>\n\n"
            "Envie o <b>nome</b> do novo produto.\n"
            "Você poderá editar os outros campos depois.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_product_name",
            screen="products:new",
            use_force_reply=True,
            reply_placeholder="Nome do produto...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_new falhou: {exc}")


async def _create_product(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        await message.answer("❌ Nome inválido.")
        return
    if len(name) > 120:
        name = name[:120]

    # Slug
    import re
    slug_base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "produto"

    try:
        async with session_scope() as session:
            # garante slug único
            slug = slug_base
            i = 1
            while True:
                r = await session.execute(
                    select(func.count(Product.id)).where(Product.slug == slug)
                )
                if int(r.scalar() or 0) == 0:
                    break
                i += 1
                slug = f"{slug_base}-{i}"

            p = Product(
                name=name,
                slug=slug,
                price=Decimal("0.00"),
                visible=False,
                auto_delivery=True,
            )
            session.add(p)
            await session.flush()
            pid = int(p.id)

        await log_action(
            admin_id=admin_id,
            action="product.created",
            category="admin",
            details={"product_id": pid, "name": name},
        )

        # Redireciona para editar o novo produto
        fresh = await _load_product(pid)
        if fresh is None:
            await message.answer("❌ Erro ao carregar produto.")
            return

        text, keyboard = await _render_edit_text_and_kb(fresh)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"_create_product falhou: {exc}")
        await message.answer("❌ Erro ao criar produto.")


# ============================================================
# EDITAR — RENDER
# ============================================================
async def _render_edit_text_and_kb(p: Product) -> Tuple[str, InlineKeyboardMarkup]:
    total, disp, used = await _count_credentials(int(p.id))
    cat_name = "—"
    try:
        if p.category_id:
            async with session_scope() as session:
                r = await session.execute(
                    select(Category).where(Category.id == p.category_id)
                )
                cat = r.scalar_one_or_none()
                if cat:
                    cat_name = cat.name
    except Exception:
        pass

    rendered = await message_engine.render(
        MSG_KEY_EDIT,
        fallback=(
            f"📦 <b>PRODUTO #{p.id}</b>\n\n"
            f"├ Nome: {p.name}\n"
            f"├ Subtítulo: {p.subtitle or '—'}\n"
            f"├ Descrição: {truncate(p.description, 60)}\n"
            f"├ Preço: {fmt_brl(p.price)}\n"
            f"├ Preço promo: {fmt_brl(p.price_promo) if p.price_promo else '—'}\n"
            f"├ Categoria: {cat_name}\n"
            f"├ Slug: <code>{p.slug}</code>\n"
            f"├ Estoque: {int(p.stock or 0)}\n"
            f"├ Estoque mínimo: {int(p.stock_min or 0)}\n"
            f"├ Ilimitado: {humanize_bool(p.unlimited)}\n"
            f"├ Limite/usuário: {int(p.per_user_limit or 0)}\n"
            f"├ Ordem: {int(p.order or 0)}\n"
            f"├ Destaque: {humanize_bool(p.featured)}\n"
            f"├ Esgotado: {humanize_bool(p.sold_out)}\n"
            f"├ Garantia: {int(p.warranty_days or 0)}d\n"
            f"├ Vencimento: {int(p.expiration_days or 0)}d\n"
            f"├ Nota produto: {truncate(p.note_product, 40)}\n"
            f"├ Nota ativação: {truncate(p.note_activation, 40)}\n"
            f"├ Link ativação: {truncate(p.activation_link, 40)}\n"
            f"├ Entrega auto: {humanize_bool(p.auto_delivery)}\n"
            f"├ Entrega humana: {humanize_bool(p.human_delivery)}\n"
            f"└ Visível: {humanize_bool(p.visible)}\n\n"
            f"🔐 Credenciais: total {total} · disponíveis {disp} · usadas {used}"
        ),
        id=p.id,
        nome=p.name,
        subtitulo=p.subtitle or "—",
        descricao=truncate(p.description, 60),
        preco=fmt_brl(p.price),
        preco_promo=fmt_brl(p.price_promo) if p.price_promo else "—",
        cat=cat_name,
        slug=p.slug,
        estoque=int(p.stock or 0),
        estoque_min=int(p.stock_min or 0),
        ilimitado=humanize_bool(p.unlimited),
        limite=int(p.per_user_limit or 0),
        ordem=int(p.order or 0),
        destaque=humanize_bool(p.featured),
        esgotado=humanize_bool(p.sold_out),
        img_tg=truncate(p.image_telegram, 30) if p.image_telegram else "—",
        img_wa=truncate(p.image_webapp, 30) if p.image_webapp else "—",
        arquivo=truncate(p.file_url, 30) if p.file_url else "—",
        nota_produto=truncate(p.note_product, 40),
        nota_ativacao=truncate(p.note_activation, 40),
        link_ativacao=truncate(p.activation_link, 40),
        garantia=int(p.warranty_days or 0),
        venc=int(p.expiration_days or 0),
        entrega_auto=humanize_bool(p.auto_delivery),
        entrega_humana=humanize_bool(p.human_delivery),
        visivel=humanize_bool(p.visible),
        qtd=total,
        disp=disp,
        usadas=used,
    )

    keyboard = await button_engine.inline(SCREEN_EDIT)
    if keyboard is None:
        keyboard = kb(_default_edit_keyboard(int(p.id)))

    return rendered.content, keyboard


def _default_edit_keyboard(pid: int) -> List[List[InlineKeyboardButton]]:
    """Fallback completo do DIM para edição do produto."""
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Nome", f"{CB_SET}:name:{pid}"), b("✏️ Subtítulo", f"{CB_SET}:subtitle:{pid}")],
        [b("✏️ Descrição", f"{CB_SET}:description:{pid}")],
        [b("✏️ Preço", f"{CB_SET}:price:{pid}"), b("✏️ Promo", f"{CB_SET}:price_promo:{pid}")],
        [b("🏷️ Categoria", f"{CB_CAT}:{pid}")],
        [b("✏️ Estoque", f"{CB_SET}:stock:{pid}"), b("✏️ Estoque Mín", f"{CB_SET}:stock_min:{pid}")],
        [b(f"🔄 Ilimitado", f"{CB_TOGGLE}:unlimited:{pid}"), b("✏️ Limite/User", f"{CB_SET}:per_user_limit:{pid}")],
        [b("✏️ Ordem", f"{CB_SET}:order:{pid}"), b(f"🔄 Destaque", f"{CB_TOGGLE}:featured:{pid}")],
        [b("🔄 Esgotado", f"{CB_TOGGLE}:sold_out:{pid}"), b("🖼️ Imagem TG", f"{CB_IMG}:image_telegram:{pid}")],
        [b("🖼️ Imagem WebApp", f"{CB_IMG}:image_webapp:{pid}"), b("📄 Arquivo", f"{CB_FILE}:{pid}")],
        [b("✏️ Nota", f"{CB_SET}:note_product:{pid}"), b("✏️ Nota Ativação", f"{CB_SET}:note_activation:{pid}")],
        [b("✏️ Link Ativação", f"{CB_SET}:activation_link:{pid}"), b("✏️ Garantia", f"{CB_SET}:warranty_days:{pid}")],
        [b("✏️ Vencimento", f"{CB_SET}:expiration_days:{pid}")],
        [b("🔄 Entrega Auto", f"{CB_TOGGLE}:auto_delivery:{pid}"), b("🔄 Entrega Humana", f"{CB_TOGGLE}:human_delivery:{pid}")],
        [b("🔄 Visível", f"{CB_TOGGLE}:visible:{pid}")],
        [b("📦 Credenciais", f"{CB_CRED}:{pid}")],
        [b("🚀 Postar no Canal", f"{CB_POST_CHANNEL}:{pid}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{pid}")],
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
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ Produto inválido.")
            return
        p = await _load_product(pid)
        if p is None:
            await toast_err(callback, "❌ Produto não encontrado.")
            return
        text, keyboard = await _render_edit_text_and_kb(p)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_edit falhou: {exc}")


# ============================================================
# SET FIELD (input de texto)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        parts = callback.data.split(":")
        # admin:products:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        pid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "money":
            hint = "Ex.: <code>19.90</code>"
        elif meta["validate"] == "int":
            hint = "Ex.: <code>10</code>"
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
            screen=f"products:edit:{pid}",
            extra={"product_id": pid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_set_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    field: str,
    value: str,
    extra: Dict[str, Any],
) -> None:
    pid = extra.get("product_id")
    if not pid:
        await message.answer("❌ Produto não identificado.")
        return
    try:
        pid = int(pid)
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

    # ---------- Validação ----------
    new_value: Any = None
    if meta["validate"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        new_value = Decimal(str(round(num, 2)))
    elif meta["validate"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        new_value = int(num)
    else:
        if len(val) > meta.get("max", 500):
            await message.answer("❌ Texto muito longo.")
            return
        new_value = val

    # ---------- Salva ----------
    try:
        async with session_scope() as session:
            r = await session.execute(select(Product).where(Product.id == pid))
            p: Optional[Product] = r.scalar_one_or_none()
            if p is None:
                await message.answer("❌ Produto não encontrado.")
                return
            setattr(p, field, new_value)
            session.add(p)
    except Exception as exc:
        logger.exception(f"_apply_field_value falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"product.set.{field}",
        category="admin",
        details={"product_id": pid, "value": str(new_value)},
    )

    # Invalida cache de estoque se for stock
    try:
        if field in ("stock", "stock_min", "unlimited"):
            await invalidate_stock_cache(pid)
    except Exception:
        pass

    # Redireciona para edição atualizada
    p2 = await _load_product(pid)
    if p2 is None:
        await message.answer("❌ Erro ao recarregar.")
        return
    text, keyboard = await _render_edit_text_and_kb(p2)
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
async def on_toggle_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:products:toggle:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        pid = int(parts[4])

        if field not in TOGGLE_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Product).where(Product.id == pid))
                p: Optional[Product] = r.scalar_one_or_none()
                if p is None:
                    await toast_err(callback, "❌ Produto não encontrado.")
                    return
                current = bool(getattr(p, field))
                setattr(p, field, not current)
                new_val = not current
                session.add(p)
        except Exception as exc:
            logger.exception(f"toggle falhou: {exc}")
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"product.toggle.{field}",
            category="admin",
            details={"product_id": pid, "value": new_val},
        )

        try:
            if field in ("unlimited", "sold_out"):
                await invalidate_stock_cache(pid)
        except Exception:
            pass

        # Re-render
        p2 = await _load_product(pid)
        if p2 is None:
            await toast_err(callback, "❌ Erro ao recarregar.")
            return
        text, keyboard = await _render_edit_text_and_kb(p2)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🔄 {TOGGLE_FIELDS[field]} alterado.")
    except Exception as exc:
        logger.exception(f"products on_toggle_field falhou: {exc}")


# ============================================================
# IMAGEM / ARQUIVO (URL)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_IMG}:"))
async def on_set_image(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:products:img:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        pid = int(parts[4])

        if field not in ("image_telegram", "image_webapp"):
            await toast_err(callback, "❌ Campo inválido.")
            return

        label = "Imagem TG" if field == "image_telegram" else "Imagem WebApp"
        prompt = (
            f"🖼️ <b>EDITAR {label.upper()}</b>\n\n"
            "Envie a URL pública da imagem (começando com http:// ou https://)\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}",
            screen=f"products:edit:{pid}",
            extra={"product_id": pid},
            use_force_reply=True,
            reply_placeholder="https://...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_set_image falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_FILE}:"))
async def on_set_file(callback: CallbackQuery) -> None:
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

        prompt = (
            "📄 <b>ARQUIVO DO PRODUTO</b>\n\n"
            "Envie a URL do arquivo (PDF, ZIP, etc.)\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="set:file_url",
            screen=f"products:edit:{pid}",
            extra={"product_id": pid},
            use_force_reply=True,
            reply_placeholder="https://...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_set_file falhou: {exc}")


# ============================================================
# CATEGORIA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_CAT}:"))
async def on_set_category(callback: CallbackQuery) -> None:
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

        cats = await _list_categories()
        if not cats:
            await toast_err(callback, "❌ Nenhuma categoria cadastrada.")
            return

        rows: List[List[InlineKeyboardButton]] = []
        for c in cats:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🏷️ {c.name}",
                        callback_data=f"{CB_CAT_SET}:{pid}:{c.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Sem categoria",
                    callback_data=f"{CB_CAT_SET}:{pid}:0",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=f"{CB_EDIT}:{pid}",
                )
            ]
        )

        text = "🏷️ <b>SELECIONE A CATEGORIA</b>"
        await edit_or_send(callback, text=text, reply_markup=kb(rows))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_set_category falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CAT_SET}:"))
async def on_apply_category(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:products:catset:ID:CATID
        pid = int(parts[3])
        cat_id = int(parts[4])

        try:
            async with session_scope() as session:
                r = await session.execute(select(Product).where(Product.id == pid))
                p: Optional[Product] = r.scalar_one_or_none()
                if p is None:
                    await toast_err(callback, "❌ Produto não encontrado.")
                    return
                p.category_id = cat_id if cat_id > 0 else None
                session.add(p)
        except Exception as exc:
            logger.exception(f"categoria falhou: {exc}")
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action="product.category.set",
            category="admin",
            details={"product_id": pid, "category_id": cat_id},
        )

        p2 = await _load_product(pid)
        if p2 is None:
            await toast_err(callback, "❌ Erro ao recarregar.")
            return
        text, keyboard = await _render_edit_text_and_kb(p2)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🏷️ Categoria atualizada.")
    except Exception as exc:
        logger.exception(f"products on_apply_category falhou: {exc}")


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
            pid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        p = await _load_product(pid)
        if p is None:
            await toast_err(callback, "❌ Produto não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR PRODUTO</b>\n\n"
            f"Você tem certeza que deseja excluir <b>{p.name}</b> (#{p.id})?\n\n"
            "⚠️ Esta ação é irreversível e removerá também as credenciais, "
            "imagens e arquivos vinculados."
        )

        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_CONFIRM}:{pid}",
            cancel_callback=f"{CB_EDIT}:{pid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )

        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_CONFIRM}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
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
            async with session_scope() as session:
                # Remove credenciais
                await session.execute(
                    ProductCredential.__table__.delete().where(
                        ProductCredential.product_id == pid
                    )
                )
                await session.execute(
                    ProductImage.__table__.delete().where(
                        ProductImage.product_id == pid
                    )
                )
                await session.execute(
                    ProductFile.__table__.delete().where(
                        ProductFile.product_id == pid
                    )
                )
                # Remove produto
                await session.execute(
                    Product.__table__.delete().where(Product.id == pid)
                )
        except Exception as exc:
            logger.exception(f"delete produto falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="product.deleted",
            category="admin",
            details={"product_id": pid},
        )

        await render_list(callback, page=1, filter_key="all")
        await toast_ok(callback, "🗑️ Produto excluído.")
    except Exception as exc:
        logger.exception(f"products on_delete_confirm falhou: {exc}")


# ============================================================
# CREDENCIAIS EM LOTE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_CRED}:"))
async def on_credentials(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # Só aceita admin:products:cred:ID
        parts = callback.data.split(":")
        if len(parts) < 4:
            await toast_err(callback, "❌ Callback inválido.")
            return
        pid = int(parts[3])

        p = await _load_product(pid)
        if p is None:
            await toast_err(callback, "❌ Produto não encontrado.")
            return

        total, disp, used = await _count_credentials(pid)

        rendered = await message_engine.render(
            MSG_KEY_CRED_LIST,
            fallback=(
                f"📦 <b>CREDENCIAIS · {p.name}</b>\n\n"
                f"Total: <b>{total}</b> | "
                f"Disponível: <b>{disp}</b> | "
                f"Usadas: <b>{used}</b>"
            ),
            nome_produto=p.name,
            qtd=total,
            disp=disp,
            usadas=used,
        )

        keyboard = await button_engine.inline(SCREEN_CRED)
        if keyboard is None:
            keyboard = kb(
                [
                    [
                        InlineKeyboardButton(
                            text="➕ Adicionar em Massa",
                            callback_data=f"{CB_CRED_ADD}:{pid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📋 Visualizar (disponíveis)",
                            callback_data=f"{CB_CRED_VIEW}:{pid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📥 Importar CSV",
                            callback_data=f"{CB_CRED_CSV}:{pid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🧹 Limpar Usadas",
                            callback_data=f"{CB_CRED_CLEAN}:{pid}",
                        ),
                        InlineKeyboardButton(
                            text="🗑️ Apagar Tudo",
                            callback_data=f"{CB_CRED_WIPE}:{pid}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔙 Voltar",
                            callback_data=f"{CB_EDIT}:{pid}",
                        )
                    ],
                ]
            )

        await edit_or_send(callback, text=rendered.content, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CRED_ADD}:"))
async def on_credentials_add(callback: CallbackQuery) -> None:
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

        prompt = (
            "➕ <b>ADICIONAR CREDENCIAIS EM MASSA</b>\n\n"
            "Envie UMA credencial por linha.\n"
            "Formatos aceitos:\n"
            "• <code>email|senha</code>\n"
            "• <code>email:senha</code>\n"
            "• <code>texto puro</code>\n\n"
            "Exemplo:\n"
            "<code>cliente1@x.com|abc123\n"
            "cliente2@x.com|def456</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"cred_add:{pid}",
            screen=f"products:cred:{pid}",
            extra={"product_id": pid},
            use_force_reply=True,
            reply_placeholder="cole as credenciais...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_add falhou: {exc}")


async def _add_credentials_bulk(
    message: Message,
    admin_id: int,
    pid: int,
    raw: str,
    extra: Dict[str, Any],
) -> None:
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    if not lines:
        await message.answer("❌ Nenhuma linha válida.")
        return

    inserted = 0
    try:
        async with session_scope() as session:
            for line in lines:
                try:
                    session.add(ProductCredential(product_id=pid, payload=line))
                    inserted += 1
                except Exception:
                    continue
    except Exception as exc:
        logger.exception(f"bulk cred falhou: {exc}")
        await message.answer("❌ Erro ao inserir credenciais.")
        return

    # Atualiza estoque do produto
    try:
        async with session_scope() as session:
            r = await session.execute(select(Product).where(Product.id == pid))
            p: Optional[Product] = r.scalar_one_or_none()
            if p is not None and not p.unlimited:
                p.stock = int(p.stock or 0) + inserted
                p.sold_out = False
                session.add(p)
    except Exception:
        pass

    await log_action(
        admin_id=admin_id,
        action="product.credentials.added",
        category="admin",
        details={"product_id": pid, "count": inserted},
    )

    try:
        await invalidate_stock_cache(pid)
    except Exception:
        pass

    await message.answer(
        text=(
            f"✅ <b>{inserted} credencial(is) adicionada(s).</b>\n\n"
            f"Use o botão 📦 Credenciais para conferir."
        ),
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="📦 Ver Credenciais",
                        callback_data=f"{CB_CRED}:{pid}",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith(f"{CB_CRED_VIEW}:"))
async def on_credentials_view(callback: CallbackQuery) -> None:
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
            async with session_scope() as session:
                res = await session.execute(
                    select(ProductCredential)
                    .where(
                        ProductCredential.product_id == pid,
                        ProductCredential.used == False,  # noqa: E712
                    )
                    .order_by(ProductCredential.id.asc())
                    .limit(30)
                )
                creds = list(res.scalars().all())
        except Exception:
            creds = []

        if not creds:
            text = "📋 <i>Nenhuma credencial disponível.</i>"
        else:
            text = "📋 <b>CREDENCIAIS DISPONÍVEIS (até 30)</b>\n\n"
            for c in creds:
                payload = truncate(c.payload, 60)
                text += f"<code>{c.id}</code> · <code>{payload}</code>\n"

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🔙 Voltar",
                            callback_data=f"{CB_CRED}:{pid}",
                        )
                    ]
                ]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_view falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CRED_CLEAN}:"))
async def on_credentials_clean(callback: CallbackQuery) -> None:
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
            async with session_scope() as session:
                await session.execute(
                    ProductCredential.__table__.delete().where(
                        ProductCredential.product_id == pid,
                        ProductCredential.used == True,  # noqa: E712
                    )
                )
        except Exception as exc:
            logger.exception(f"clean cred falhou: {exc}")
            await toast_err(callback, "❌ Erro ao limpar.")
            return

        await log_action(
            admin_id=admin_id,
            action="product.credentials.cleaned",
            category="admin",
            details={"product_id": pid},
        )

        await toast_ok(callback, "🧹 Credenciais usadas removidas.")
        # Re-render
        fake = type("_Fake", (), {})
        # Reusa o handler de credenciais
        cb_data = f"{CB_CRED}:{pid}"
        callback.data = cb_data  # type: ignore[misc]
        await on_credentials(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_clean falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CRED_WIPE}:"))
async def on_credentials_wipe(callback: CallbackQuery) -> None:
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

        text = (
            "🗑️ <b>APAGAR TODAS AS CREDENCIAIS</b>\n\n"
            "Isso apagará TUDO, incluindo as usadas.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CRED_WIPE}:yes:{pid}",
            cancel_callback=f"{CB_CRED}:{pid}",
            confirm_text="🗑️ Sim, apagar tudo",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_wipe falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CRED_WIPE}:yes:"))
async def on_credentials_wipe_confirm(callback: CallbackQuery) -> None:
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
            async with session_scope() as session:
                await session.execute(
                    ProductCredential.__table__.delete().where(
                        ProductCredential.product_id == pid
                    )
                )
                r = await session.execute(select(Product).where(Product.id == pid))
                p: Optional[Product] = r.scalar_one_or_none()
                if p is not None and not p.unlimited:
                    p.stock = 0
                    p.sold_out = True
                    session.add(p)
        except Exception as exc:
            logger.exception(f"wipe cred falhou: {exc}")
            await toast_err(callback, "❌ Erro ao apagar.")
            return

        await log_action(
            admin_id=admin_id,
            action="product.credentials.wiped",
            category="admin",
            details={"product_id": pid},
        )

        try:
            await invalidate_stock_cache(pid)
        except Exception:
            pass

        await toast_ok(callback, "🗑️ Tudo apagado.")
        callback.data = f"{CB_CRED}:{pid}"  # type: ignore[misc]
        await on_credentials(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_wipe_confirm falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CRED_CSV}:"))
async def on_credentials_csv(callback: CallbackQuery) -> None:
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

        prompt = (
            "📥 <b>IMPORTAR CSV</b>\n\n"
            "Envie o conteúdo do CSV (uma credencial por linha).\n"
            "Não precisa de cabeçalho.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"cred_add:{pid}",
            screen=f"products:cred:{pid}",
            extra={"product_id": pid},
            use_force_reply=True,
            reply_placeholder="cole o CSV...",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"products on_credentials_csv falhou: {exc}")


# ============================================================
# POSTAR NO CANAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_POST_CHANNEL}:"))
async def on_post_channel(callback: CallbackQuery) -> None:
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

        p = await _load_product(pid)
        if p is None:
            await toast_err(callback, "❌ Produto não encontrado.")
            return

        try:
            result = await channel_post.post_promo(
                callback.bot,
                product_id=int(p.id),
                product_name=str(p.name),
                price=float(p.price_promo or p.price or 0),
                subtitle=p.subtitle,
            )
            sent = result.sent if hasattr(result, "sent") else 0
            if sent > 0:
                await toast_ok(callback, f"🚀 Postado em {sent} canal(is).")
            else:
                await toast_err(callback, "❌ Nenhum canal configurado.")
        except Exception as exc:
            logger.exception(f"post_channel falhou: {exc}")
            await toast_err(callback, "❌ Erro ao postar no canal.")

        await log_action(
            admin_id=admin_id,
            action="product.posted_to_channel",
            category="admin",
            details={"product_id": pid},
        )
    except Exception as exc:
        logger.exception(f"products on_post_channel falhou: {exc}")


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
    "on_products_text_input",
    "on_new",
    "on_edit",
    "on_set_field",
    "on_toggle_field",
    "on_set_image",
    "on_set_file",
    "on_set_category",
    "on_apply_category",
    "on_delete",
    "on_delete_confirm",
    "on_credentials",
    "on_credentials_add",
    "on_credentials_view",
    "on_credentials_clean",
    "on_credentials_wipe",
    "on_credentials_wipe_confirm",
    "on_credentials_csv",
    "on_post_channel",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_SEARCH",
    "CB_NEW",
    "CB_EDIT",
    "CB_DELETE",
    "CB_TOGGLE",
    "CB_SET",
    "CB_IMG",
    "CB_FILE",
    "CB_CAT",
    "CB_CAT_SET",
    "CB_CRED",
    "CB_CRED_ADD",
    "CB_CRED_VIEW",
    "CB_CRED_CSV",
    "CB_CRED_CLEAN",
    "CB_CRED_WIPE",
    "CB_POST_CHANNEL",
    "TEXT_FIELDS",
    "TOGGLE_FIELDS",
    "FILTERS",
]
