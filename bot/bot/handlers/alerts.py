"""
Larizinha Store — Handler de Alertas (/alertas)
Implementa o sistema de alertas de abastecimento exatamente como o DIM define:

    ⚠️ Sistema de /alertas
    Seja notificado quando seu serviço favorito for abastecido 🤩
    🎯 Selecione abaixo os serviços que deseja ser notificado
    ✅ Nossos produtos são de grandes demandas e acabam rápido
    Lista de serviços que você pode ser notificado ⤵️

    • Cada produto aparece como [☑️ <nome>]
    • Ao clicar → vira [✅ <nome>] e a MESMA mensagem é EDITADA
    • Clicar de novo → volta para [☑️ <nome>]
    • Paginação: [🔙 Voltar] [Próxima Página ➡️] (regra DIM)
    • Persistência em User.alerts_enabled (JSON: [product_id, ...])

Integração:
- Quando o `StockAlerts` detecta reabastecimento, o worker chama
  `notify_alerts_subscribers(product_id)` que envia para TODOS os usuários
  inscritos a mensagem editável `alerta_estoque` (já usada pelo stock_alerts).
- NUNCA acumula: o envio para o usuário é UMA mensagem nova (a notificação
  legítima), mas a navegação dentro de /alertas sempre EDITA.
"""
from __future__ import annotations

import math
from typing import List, Optional, Set, Tuple

from aiogram import F, Router
from aiogram.filters import Command
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
    ProductCredential,
    User,
    session_scope,
)
from bot.logger import audit, logger
from bot.redis_client import Throttle
from bot.services.message_engine import message_engine
from bot.services.stock_alerts import (
    MESSAGE_KEY_ALERTA_ESTOQUE,
    stock_alerts,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="alerts")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_ALERTAS = "cmd_alertas"
MSG_KEY_ALERTA_ESTOQUE = MESSAGE_KEY_ALERTA_ESTOQUE  # reuso
SCREEN_ALERTAS = "alerts"

CB_ALERTS_OPEN = "alerts:open"
CB_ALERTS_TOGGLE = "alerts:toggle"     # alerts:toggle:ID:page:N
CB_ALERTS_PAGE = "alerts:page"         # alerts:page:N

ITEMS_PER_PAGE = 8


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _reload_user(user_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


def _user_alert_ids(user: User) -> Set[int]:
    """Extrai o conjunto de IDs do `alerts_enabled` (tolerante a formatos)."""
    raw = user.alerts_enabled if user is not None else []
    out: Set[int] = set()
    if not raw:
        return out
    if isinstance(raw, list):
        for v in raw:
            try:
                out.add(int(v))
            except Exception:
                continue
    return out


async def _count_visible_products() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Product.id))
                .where(Product.visible == True)  # noqa: E712
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_products_page(page: int, per_page: int = ITEMS_PER_PAGE) -> List[Product]:
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(Product.visible == True)  # noqa: E712
                .order_by(Product.order.asc(), Product.id.asc())
                .offset(offset)
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# 🧩 TECLADO DO /alertas
# ============================================================
def _build_alerts_keyboard(
    *,
    products: List[Product],
    subscribed: Set[int],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """
    Monta o teclado do /alertas:
      - Cada produto vira [☑️ nome] ou [✅ nome]
      - Paginação conforme DIM:
          Página 1:            [Próxima Página ➡️]
          Páginas do meio:     [🔙 Voltar] [⬅️ Anterior] [Próxima Página ➡️]
          Última página:       [🔙 Voltar] [⬅️ Anterior]
          (🔙 Voltar leva à página 1)
    """
    rows: List[List[InlineKeyboardButton]] = []

    # ---------- Botões dos produtos ----------
    for p in products:
        pid = int(p.id)
        marcado = pid in subscribed
        prefix = "✅" if marcado else "☑️"
        # Nome encurtado pra caber no botão
        nome = p.name if len(p.name) <= 32 else p.name[:29] + "..."
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {nome}",
                    callback_data=f"{CB_ALERTS_TOGGLE}:{pid}:page:{page}",
                )
            ]
        )

    # ---------- Paginação DIM ----------
    if total_pages > 1:
        if page == 1:
            # Primeira página
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Próxima Página ➡️",
                        callback_data=f"{CB_ALERTS_PAGE}:{page + 1}",
                    )
                ]
            )
        elif 1 < page < total_pages:
            # Meio
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=f"{CB_ALERTS_PAGE}:1",
                    ),
                    InlineKeyboardButton(
                        text="⬅️ Anterior",
                        callback_data=f"{CB_ALERTS_PAGE}:{page - 1}",
                    ),
                    InlineKeyboardButton(
                        text="Próxima Página ➡️",
                        callback_data=f"{CB_ALERTS_PAGE}:{page + 1}",
                    ),
                ]
            )
        else:
            # Última
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=f"{CB_ALERTS_PAGE}:1",
                    ),
                    InlineKeyboardButton(
                        text="⬅️ Anterior",
                        callback_data=f"{CB_ALERTS_PAGE}:{page - 1}",
                    ),
                ]
            )
    else:
        # Uma página só
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data="catalog:open",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# RENDER PRINCIPAL
# ============================================================
async def _render_alerts_page(user: User, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    total = await _count_visible_products()
    total_pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
    page = max(1, min(page, total_pages))

    products = await _load_products_page(page, ITEMS_PER_PAGE)
    subscribed = _user_alert_ids(user)

    # ---------- Texto editável ----------
    rendered = await message_engine.render(
        MSG_KEY_ALERTAS,
        fallback=(
            "⚠️ <b>Sistema de alertas</b>\n\n"
            "Seja notificado quando seu serviço favorito for abastecido 🤩\n"
            "🎯 Basta selecionar abaixo os serviços que você deseja ser "
            "notificado, e eu lhe avisarei sempre que for abastecido novas unidades.\n\n"
            "✅ Nossos produtos são de grandes demandas e acabam rápido, "
            "é importante que você seja notificado para aproveitar antes que acabe!\n\n"
            "Lista de serviços que você pode ser notificado ⤵️"
        ),
        total=total,
        seguindo=len(subscribed),
        pagina=page,
        total_paginas=total_pages,
    )

    kb = _build_alerts_keyboard(
        products=products,
        subscribed=subscribed,
        page=page,
        total_pages=total_pages,
    )
    return rendered.content, kb


# ============================================================
# HANDLER: ABRIR /alertas (via comando)
# ============================================================
@router.message(Command("alertas"))
async def on_cmd_alertas(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_alerts_page(fresh, page=1)

        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/alertas falhou: {exc}")


# ============================================================
# HANDLER: ABRIR VIA CALLBACK
# ============================================================
@router.callback_query(F.data == CB_ALERTS_OPEN)
async def on_open_alerts(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "alerts", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_alerts_page(fresh, page=1)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_open_alerts falhou: {exc}")


# ============================================================
# HANDLER: TOGGLE DE PRODUTO (☑️ ↔ ✅)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_ALERTS_TOGGLE}:"))
async def on_toggle_alert(callback: CallbackQuery, user: User) -> None:
    """
    Formato do callback: alerts:toggle:ID:page:N
    """
    try:
        parts = callback.data.split(":")
        # parts = [alerts, toggle, ID, page, N]
        product_id = 0
        page = 1
        try:
            product_id = int(parts[2])
        except Exception:
            await callback.answer("❌ Produto inválido.", show_alert=True)
            return
        try:
            if len(parts) >= 5 and parts[3] == "page":
                page = int(parts[4])
        except Exception:
            page = 1

        # ---------- Persistência ----------
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(User).where(User.id == user.id).with_for_update()
                )
                u: Optional[User] = res.scalar_one_or_none()
                if u is None:
                    await callback.answer("❌ Usuário não encontrado.", show_alert=True)
                    return

                ids = _user_alert_ids(u)

                if product_id in ids:
                    ids.discard(product_id)
                    toggled_on = False
                else:
                    ids.add(product_id)
                    toggled_on = True

                u.alerts_enabled = sorted(ids)
                session.add(u)
        except Exception as exc:
            logger.exception(f"toggle de alerta falhou: {exc}")
            await callback.answer("❌ Falha ao salvar.", show_alert=True)
            return

        # ---------- Re-renderiza a MESMA mensagem ----------
        fresh = await _reload_user(user.id) or user
        text, kb = await _render_alerts_page(fresh, page=page)

        try:
            await callback.answer(
                "✅ Inscrito!" if toggled_on else "☑️ Removido.",
                show_alert=False,
            )
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                if "not modified" not in str(exc).lower():
                    logger.debug(f"edit alerts falhou: {exc}")

        audit(
            action="alerts.toggle",
            user_id=user.id,
            details={"product_id": product_id, "on": toggled_on},
        )
    except Exception as exc:
        logger.exception(f"on_toggle_alert falhou: {exc}")


# ============================================================
# HANDLER: PAGINAÇÃO (regra DIM)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_ALERTS_PAGE}:"))
async def on_alerts_page(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_alerts_page(fresh, page=page)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_alerts_page falhou: {exc}")


# ============================================================
# 🛎️ NOTIFICAR INSCRITOS (chamado pelo worker/stock_alerts)
# ============================================================
async def notify_alerts_subscribers(
    bot,
    *,
    product_id: int,
    product_name: str,
    new_stock: int,
) -> int:
    """
    Envia a mensagem editável `alerta_estoque` para TODOS os usuários que
    marcaram este produto como "quero ser notificado".

    Retorna a quantidade de notificações enviadas.
    NÃO acumula mensagens de navegação — é uma notificação legítima
    (uma nova mensagem por usuário, por evento real de reabastecimento).
    """
    if not bot:
        return 0

    # ---------- Busca os inscritos ----------
    # JSONB: alerts_enabled @> '[product_id]'
    try:
        async with session_scope() as session:
            # Usa funcionalidade do Postgres para filtrar direto no banco
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB

            stmt = (
                select(User.telegram_id)
                .where(User.status == "ACTIVE")
                .where(
                    cast(User.alerts_enabled, JSONB).op("@>")(
                        cast([int(product_id)], JSONB)
                    )
                )
            )
            res = await session.execute(stmt)
            subscribers = [int(tg) for (tg,) in res.all() if tg]
    except Exception as exc:
        logger.debug(f"notify_alerts_subscribers: query falhou: {exc}")
        subscribers = []

    if not subscribers:
        return 0

    # ---------- Renderiza a mensagem uma vez ----------
    rendered = await message_engine.render(
        MSG_KEY_ALERTA_ESTOQUE,
        fallback=(
            f"🎁 <b>NOVO ESTOQUE ADICIONADO!</b>\n\n"
            f"📦 {product_name}\n"
            f"📊 Quantidade: {new_stock}\n\n"
            "🏃‍♂️ Corra e garanta o seu!"
        ),
        nome_produto=product_name,
        qtd=new_stock,
        quantidade=new_stock,
        nome_loja=settings.APP_NAME,
    )

    # ---------- Teclado (product_id na callback) ----------
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Comprar Agora",
                    callback_data=f"catalog:product:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔕 Cancelar alerta deste produto",
                    callback_data=f"alerts:toggle:{product_id}:page:1",
                )
            ],
        ]
    )

    # ---------- Envio ----------
    sent = 0
    failed = 0
    import asyncio

    async def _send_one(tg_id: int) -> None:
        nonlocal sent, failed
        try:
            await bot.send_message(
                chat_id=tg_id,
                text=rendered.content,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            failed += 1

    # Limita concorrência em blocos
    batch_size = 25
    for i in range(0, len(subscribers), batch_size):
        batch = subscribers[i : i + batch_size]
        await asyncio.gather(*(_send_one(tg) for tg in batch))
        # pequeno respiro para evitar flood
        await asyncio.sleep(0.7)

    logger.bind(category="alerts").info(
        f"Notificações de estoque: produto={product_id} "
        f"enviadas={sent} falhas={failed}"
    )
    return sent


# ============================================================
# 🎯 HELPER: chamado pelo worker após `stock_alerts.check_product`
# ============================================================
async def dispatch_stock_notifications(bot, event) -> None:
    """
    Recebe um StockEvent do `stock_alerts` e dispara as notificações
    para os inscritos (apenas se foi REFILLED ou LOW/OUT).
    Também envia para admins (opcional) via stock_alerts.persist_alert.
    """
    try:
        if event is None:
            return

        # Envia apenas para REFILLED (reabastecido) e OUT/LOW (aviso)
        await notify_alerts_subscribers(
            bot,
            product_id=int(event.product_id),
            product_name=str(event.product_name),
            new_stock=int(event.new_stock),
        )

        # Persiste alerta admin (agrega no painel)
        try:
            await stock_alerts.persist_alert(
                category="stock",
                severity="warning" if event.is_alert() else "info",
                title=f"Estoque: {event.product_name}",
                message=(
                    f"Estado: {event.state.upper()} · "
                    f"antes={event.prev_stock} agora={event.new_stock} "
                    f"(threshold={event.threshold})"
                ),
            )
        except Exception:
            pass
    except Exception as exc:
        logger.debug(f"dispatch_stock_notifications falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_cmd_alertas",
    "on_open_alerts",
    "on_toggle_alert",
    "on_alerts_page",
    "notify_alerts_subscribers",
    "dispatch_stock_notifications",
    "CB_ALERTS_OPEN",
    "CB_ALERTS_TOGGLE",
    "CB_ALERTS_PAGE",
    "ITEMS_PER_PAGE",
]
