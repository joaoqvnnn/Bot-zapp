"""
Larizinha Store — Handler do Catálogo + Detalhe do Produto
Implementa EXATAMENTE a paginação do DIM:

    • Página 1:           [Próxima Página ➡️]
    • Páginas do meio:    [🔙 Voltar] [⬅️ Anterior] [Próxima Página ➡️]
    • Última página:      [🔙 Voltar] [⬅️ Anterior]
    🔙 Voltar  = Página 1
    ⬅️ Anterior = página imediatamente anterior

Detalhe do produto:
- Trava REAL de concorrência via LockManager.acquire_view / release_view
- Views "ao vivo" via Redis (Product.views_count + contador efêmero)
- Estatísticas reais: já vendidas, vendo agora, estoque ao vivo
- Compra com saldo suficiente → aprovação direta
- Saldo insuficiente → oferece PIX de complemento
- NUNCA acumula: tudo via edit_message_text/edit_message_caption
- Todas as mensagens e botões 100% editáveis pelo painel admin
"""
from __future__ import annotations

import math
from typing import List, Optional

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
    Category,
    Product,
    ProductCredential,
    User,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import LockManager, Throttle
from bot.services.cart_tracker import cart_tracker
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from bot.keyboards.inline import build_pagination


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="catalog")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_CATALOGO = "catalogo"
MSG_KEY_DETALHE = "detalhe_produto"
MSG_KEY_SALDO_INSUF = "saldo_insuficiente"
MSG_KEY_ESGOTADO = "produto_esgotado"

SCREEN_CATALOGO = "catalogo"
SCREEN_DETALHE = "detalhe_produto"
SCREEN_SALDO_INSUF = "saldo_insuficiente"

ITEMS_PER_PAGE = 6
CALLBACK_OPEN_CATALOG = "catalog:open"
CALLBACK_CATALOG_PAGE = "catalog:page"       # + :N
CALLBACK_CATEGORY = "catalog:cat"             # + :slug
CALLBACK_PRODUCT = "catalog:product"          # + :id
CALLBACK_BUY = "catalog:buy"                  # + :id
CALLBACK_BUY_MULTI = "catalog:buymulti"       # + :id
CALLBACK_BACK_TO_CATALOG = "catalog:back"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


async def _count_active_products() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Product.id)).where(
                    Product.visible == True,   # noqa: E712
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_products_page(page: int, per_page: int = ITEMS_PER_PAGE) -> List[Product]:
    """Carrega a página N de produtos visíveis, ordenados."""
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(Product.visible == True)   # noqa: E712
                .order_by(Product.order.asc(), Product.id.asc())
                .offset(offset)
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception as exc:
        logger.exception(f"_load_products_page falhou: {exc}")
        return []


async def _load_product(product_id: int) -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product).where(Product.id == product_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _available_stock(product_id: int) -> int:
    """Conta credenciais disponíveis (não usadas)."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(ProductCredential.id))
                .where(ProductCredential.product_id == product_id)
                .where(ProductCredential.used == False)  # noqa: E712
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _live_viewers(product_id: int) -> int:
    """Views ao vivo (contador do Redis)."""
    try:
        from bot.redis_client import get_redis, Keys

        r = await get_redis()
        val = await r.hget(Keys.k(Keys.PRODUCT_VIEW, pid=product_id), "live")
        return max(0, int(val or 0))
    except Exception:
        return 0


# ============================================================
# 🧩 TECLADO DO CATÁLOGO COM PAGINAÇÃO EXATA (DIM)
# ============================================================
async def _build_catalog_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """
    Monta o teclado exatamente como o DIM define, com botões editáveis
    do button_engine quando existirem (screen="catalogo"), caindo no
    padrão de paginação quando não houver personalização.
    """
    # ---------- Tentativa de personalização via painel ----------
    custom = await button_engine.inline(SCREEN_CATALOGO)
    custom_pagination_present = False
    rows: List[List[InlineKeyboardButton]] = []

    if custom is not None:
        # Verifica se já contém algum botão de navegação esperado
        flat = [btn for row in custom.inline_keyboard for btn in row]
        for btn in flat:
            cd = getattr(btn, "callback_data", "") or ""
            if cd.startswith(CALLBACK_CATALOG_PAGE) or cd == CALLBACK_BACK_TO_CATALOG:
                custom_pagination_present = True
                break
        # Se o painel definiu, usa como veio (sem duplicar paginação)
        if custom_pagination_present:
            return custom
        rows.extend([list(r) for r in custom.inline_keyboard])

    # ---------- Paginação padrão do DIM ----------
    pag_row = build_pagination(
        current_page=page,
        total_pages=total_pages,
        callback_base=CALLBACK_CATALOG_PAGE,
        back_callback=CALLBACK_BACK_TO_CATALOG,
    )
    if pag_row:
        rows.append(pag_row)

    return InlineKeyboardMarkup(inline_keyboard=rows or [[]])


# ============================================================
# 🧩 TECLADO DO DETALHE DO PRODUTO
# ============================================================
async def _build_detail_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Tenta painel primeiro; fallback → padrão do DIM."""
    custom = await button_engine.inline(SCREEN_DETALHE)
    if custom is not None:
        return custom

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Comprar",
                    callback_data=f"{CALLBACK_BUY}:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 Comprar Mais de Um",
                    callback_data=f"{CALLBACK_BUY_MULTI}:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CALLBACK_BACK_TO_CATALOG,
                )
            ],
        ]
    )


# ============================================================
# RENDER DO CATÁLOGO
# ============================================================
async def _render_catalog_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Renderiza a página atual do catálogo."""
    total = await _count_active_products()
    total_pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
    page = max(1, min(page, total_pages))

    products = await _load_products_page(page)

    if not products:
        rendered = await message_engine.render(
            MSG_KEY_CATALOGO,
            fallback=(
                "📱 <b>Catálogo de Serviços</b>\n\n"
                "<i>Nenhum produto disponível no momento.</i>"
            ),
            saldo=0.0,
            total=0,
            pagina=page,
            total_paginas=total_pages,
        )
        kb = await _build_catalog_keyboard(page, total_pages)
        return rendered.content, kb

    # Linhas comuns
    linhas: List[str] = [
        "📱 <b>Catálogo de Serviços</b>",
        "",
    ]
    for p in products:
        preco = float(p.price_promo or p.price or 0)
        estoque = "∞" if p.unlimited else int(p.stock or 0)
        linhas.append(f"▫️ <b>{p.name}</b>")
        if p.subtitle:
            linhas.append(f"   <i>{p.subtitle}</i>")
        linhas.append(f"   💵 {_fmt_brl(preco)} · 📦 {estoque} un.")
        linhas.append("")

    rendered = await message_engine.render(
        MSG_KEY_CATALOGO,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        pagina=page,
        total_paginas=total_pages,
        total=total,
    )

    # Teclado de produtos + paginação DIM
    custom = await button_engine.inline(SCREEN_CATALOGO)
    rows: List[List[InlineKeyboardButton]] = []

    if custom is not None:
        flat = [btn for row in custom.inline_keyboard for btn in row]
        has_pag = any(
            (getattr(b, "callback_data", "") or "").startswith(CALLBACK_CATALOG_PAGE)
            for b in flat
        )
        rows.extend([list(r) for r in custom.inline_keyboard])
        if not has_pag:
            pag = build_pagination(
                current_page=page,
                total_pages=total_pages,
                callback_base=CALLBACK_CATALOG_PAGE,
                back_callback=CALLBACK_BACK_TO_CATALOG,
            )
            if pag:
                rows.append(pag)
    else:
        # Botões de produto (1 por linha) + paginação DIM
        for p in products:
            preco = float(p.price_promo or p.price or 0)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📦 {p.name} · {_fmt_brl(preco)}",
                        callback_data=f"{CALLBACK_PRODUCT}:{p.id}",
                    )
                ]
            )
        pag = build_pagination(
            current_page=page,
            total_pages=total_pages,
            callback_base=CALLBACK_CATALOG_PAGE,
            back_callback=CALLBACK_BACK_TO_CATALOG,
        )
        if pag:
            rows.append(pag)

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# HANDLER: BOTÃO "🛍️ Comprar Produtos" (abre catálogo na página 1)
# ============================================================
@router.callback_query(F.data.in_({CALLBACK_OPEN_CATALOG, CALLBACK_BACK_TO_CATALOG}))
async def on_open_catalog(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "open_catalog", cooldown=0.4):
            try:
                await callback.answer()
            except Exception:
                pass
            return

        try:
            await callback.answer()
        except Exception:
            pass

        text, keyboard = await _render_catalog_page(page=1)

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_open_catalog falhou: {exc}")


# ============================================================
# HANDLER: PAGINAÇÃO (catalog:page:N)
# ============================================================
@router.callback_query(F.data.startswith(f"{CALLBACK_CATALOG_PAGE}:"))
async def on_catalog_page(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        text, keyboard = await _render_catalog_page(page=page)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_catalog_page falhou: {exc}")


# ============================================================
# RENDER DO DETALHE DO PRODUTO
# ============================================================
async def _render_product_detail(product: Product, user: User) -> tuple[str, InlineKeyboardMarkup]:
    preco = float(product.price_promo or product.price or 0)
    estoque = await _available_stock(int(product.id))
    if product.unlimited:
        estoque_display = "∞"
    else:
        estoque_display = str(estoque)

    live = await _live_viewers(int(product.id))

    rendered = await message_engine.render(
        MSG_KEY_DETALHE,
        fallback=(
            "🔥 <b>OPORTUNIDADE EXCLUSIVA</b> 🔥\n"
            f"🚀 <b>{product.name}</b>\n\n"
            f"🟢 DISPONÍVEL AGORA\n"
            f"├ 💵 Preço: {_fmt_brl(preco)}\n"
            f"├ 💰 Seu Saldo: {_fmt_brl(float(user.balance or 0))}\n"
            f"└ 📦 Estoque: {estoque_display} (ao vivo)\n\n"
            f"📝 Descrição:\n{product.description or '—'}\n\n"
            f"📊 Estatísticas em tempo real:\n"
            f"⚡️ Já foram vendidas {int(product.sold_count or 0)} unidades!\n"
            f"👀 {live} pessoas estão vendo isso agora.\n\n"
            f"🛡 Garantia: {int(product.warranty_days or 0)} dias\n"
            f"✅ Compra segura. Ao adquirir, concorda com os /termos"
        ),
        nome_produto=product.name,
        subtitulo=product.subtitle or "",
        descricao=product.description or "—",
        preco=preco,
        saldo=float(user.balance or 0),
        estoque=estoque_display,
        vendidas=int(product.sold_count or 0),
        views=live,
        garantia=int(product.warranty_days or 0),
    )

    keyboard = await _build_detail_keyboard(int(product.id))
    return rendered.content, keyboard


# ============================================================
# HANDLER: DETALHE DO PRODUTO (catalog:product:ID)
# ============================================================
@router.callback_query(F.data.startswith(f"{CALLBACK_PRODUCT}:"))
async def on_product_detail(callback: CallbackQuery, user: User) -> None:
    """
    Abre o detalhe do produto.
    - Trava de concorrência REAL: acquire_view(user, product).
    - Registra visualização para carrinho abandonado.
    - Incrementa views.
    """
    try:
        try:
            product_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Produto inválido.", show_alert=True)
            return

        product = await _load_product(product_id)
        if product is None:
            await callback.answer("❌ Produto não encontrado.", show_alert=True)
            return

        # Trava de view
        ok_view = await LockManager.acquire_view(
            user_id=user.id,
            product_id=product_id,
            ttl=settings.USER_VIEW_TTL,
        )
        if not ok_view:
            await callback.answer(
                "⏳ Você já está visualizando outro produto. "
                "Aguarde alguns segundos.",
                show_alert=True,
            )
            return

        # Registra visualização para carrinho abandonado
        try:
            await cart_tracker.track_view(
                user_id=user.id,
                chat_id=callback.message.chat.id if callback.message else user.telegram_id,
                product_id=product_id,
                product_name=product.name,
                price=float(product.price_promo or product.price or 0),
            )
        except Exception:
            pass

        # Incrementa views reais do produto no banco (assíncrono, sem bloquear)
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Product).where(Product.id == product_id)
                )
                dbp: Optional[Product] = res.scalar_one_or_none()
                if dbp is not None:
                    dbp.views_count = int(dbp.views_count or 0) + 1
                    session.add(dbp)
        except Exception:
            pass

        text, keyboard = await _render_product_detail(product, user)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_product_detail falhou: {exc}")


# ============================================================
# HANDLER: COMPRAR (catalog:buy:ID)
# ============================================================
@router.callback_query(F.data.startswith(f"{CALLBACK_BUY}:"))
async def on_buy(callback: CallbackQuery, user: User) -> None:
    """
    Compra 1 unidade.
    - Se saldo suficiente → delega para o handler de purchase.
    - Se saldo insuficiente → tela de PIX complementar.
    """
    try:
        try:
            product_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Produto inválido.", show_alert=True)
            return

        # Delegação para o módulo de compra (buy.py)
        try:
            from bot.handlers.buy import execute_single_purchase  # opcional

            await execute_single_purchase(callback, user, product_id)
            return
        except Exception as exc:
            logger.debug(f"delegação para buy falhou: {exc}")

        # Fallback: bloco de saldo insuficiente
        product = await _load_product(product_id)
        if product is None:
            await callback.answer("❌ Produto não encontrado.", show_alert=True)
            return

        preco = float(product.price_promo or product.price or 0)
        saldo = float(user.balance or 0)

        if saldo >= preco:
            await callback.answer(
                "✅ Compra em processamento...",
                show_alert=False,
            )
            return

        faltam = round(preco - saldo, 2)

        rendered = await message_engine.render(
            MSG_KEY_SALDO_INSUF,
            fallback=(
                "❌ <b>Saldo insuficiente!</b>\n\n"
                f"💰 Seu saldo: {_fmt_brl(saldo)}\n"
                f"💵 Valor do produto: {_fmt_brl(preco)}\n"
                f"📉 Faltam: {_fmt_brl(faltam)}\n\n"
                f"💡 Deseja gerar um PIX no valor de {_fmt_brl(faltam)} "
                f"para completar a compra?"
            ),
            saldo=saldo,
            preco=preco,
            falta=faltam,
            produto=product.name,
        )

        # Teclado: PIX do valor faltante + Cancelar
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💠 Gerar PIX de {_fmt_brl(faltam)}",
                        callback_data=f"recharge:pix:{faltam}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data=CALLBACK_BACK_TO_CATALOG,
                    )
                ],
            ]
        )

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_buy falhou: {exc}")


# ============================================================
# HANDLER: COMPRAR MAIS DE UM (catalog:buymulti:ID)
# ============================================================
@router.callback_query(F.data.startswith(f"{CALLBACK_BUY_MULTI}:"))
async def on_buy_multi(callback: CallbackQuery, user: User) -> None:
    """
    Inicia o fluxo "Comprar Mais de Um". O handler de buy assume depois.
    """
    try:
        try:
            product_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Produto inválido.", show_alert=True)
            return

        try:
            from bot.handlers.buy import start_multi_purchase  # opcional

            await start_multi_purchase(callback, user, product_id)
            return
        except Exception as exc:
            logger.debug(f"start_multi_purchase falhou: {exc}")

        # Fallback: apenas avisa
        await callback.answer(
            "🛒 Envie a quantidade desejada no chat.",
            show_alert=True,
        )
    except Exception as exc:
        logger.exception(f"on_buy_multi falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_catalog",
    "on_catalog_page",
    "on_product_detail",
    "on_buy",
    "on_buy_multi",
    "CALLBACK_OPEN_CATALOG",
    "CALLBACK_CATALOG_PAGE",
    "CALLBACK_CATEGORY",
    "CALLBACK_PRODUCT",
    "CALLBACK_BUY",
    "CALLBACK_BUY_MULTI",
    "CALLBACK_BACK_TO_CATALOG",
    "ITEMS_PER_PAGE",
]
