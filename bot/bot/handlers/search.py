"""
Larizinha Store — Handler de Pesquisa (🔍 Pesquisar Serviços)
Implementa o fluxo EXATO do DIM:

    1) Usuário clica em "🔍 Pesquisar Serviços" (botão no menu principal)

    2) Tela de instrução (key="pesquisar"):
        🔍 Como procurar um serviço?
        Digite: procurar <nome do serviço>
        @{username_bot}
        procurar

    3) Usuário envia "procurar <nome>"

    4) Resultado inline (key="resultado_busca"):
        🔍 Resultado para "{{busca}}"
        🎯 {{nome_produto}}
        💲 Valor: R$ {{preco}}
        📝 {{descricao_curta}}
        Para comprar, clique no botão "💳 Comprar" abaixo.

        Botões: [💳 Comprar] [🔙 Voltar]

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Inputs via Reply/ForceReply (one_time, resize, selective).
- Apaga a mensagem do usuário após capturar a busca.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Se não encontrar: mensagem editável de "nenhum resultado".
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import Product, User, session_scope
from bot.logger import logger
from bot.redis_client import FlowState, Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from bot.keyboards.reply import build_cancel_reply, build_reply_keyboard_remove


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="search")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PESQUISAR = "pesquisar"
MSG_KEY_RESULTADO = "resultado_busca"
MSG_KEY_SEM_RESULTADO = "pesquisa_sem_resultado"

SCREEN_PESQUISAR = "pesquisar"
SCREEN_RESULTADO = "resultado_busca"

CB_SEARCH_OPEN = "search:open"
CB_SEARCH_BUY = "search:buy"       # search:buy:ID

FLOW_SEARCH = "search"

PREFIX = "procurar"
MAX_RESULTS = 1  # DIM mostra 1 resultado inline por busca


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _extract_query(raw: str) -> Optional[str]:
    """
    Aceita:
      - "procurar <texto>"
      - "procurar"  (retorna vazio -> pede o nome)
      - "/buscar <texto>"
    """
    if not raw:
        return None
    txt = raw.strip()
    low = txt.lower()

    if low.startswith(PREFIX):
        rest = txt[len(PREFIX):].strip()
        return rest if rest else ""

    if low.startswith("/buscar"):
        rest = txt[len("/buscar"):].strip()
        return rest if rest else ""

    return None


async def _search_products(query: str, limit: int = MAX_RESULTS) -> List[Product]:
    """Pesquisa real em nome, subtítulo e descrição."""
    if not query:
        return []
    like = f"%{query}%"
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(Product.visible == True)  # noqa: E712
                .where(
                    or_(
                        func.lower(Product.name).like(func.lower(like)),
                        func.lower(func.coalesce(Product.subtitle, "")).like(func.lower(like)),
                        func.lower(func.coalesce(Product.description, "")).like(func.lower(like)),
                    )
                )
                .order_by(Product.order.asc(), Product.id.asc())
                .limit(limit)
            )
            return list(res.scalars().all())
    except Exception as exc:
        logger.debug(f"_search_products falhou: {exc}")
        return []


# ============================================================
# TELA — INSTRUÇÃO DE PESQUISA
# ============================================================
async def _render_search_help(user: User) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    bot_username = settings.BOT_USERNAME or "seubot"

    rendered = await message_engine.render(
        MSG_KEY_PESQUISAR,
        fallback=(
            "🔍 <b>Como procurar um serviço?</b>\n\n"
            f"Digite: <code>{PREFIX} &lt;nome do serviço&gt;</code>\n\n"
            f"@{bot_username}\n"
            f"<code>{PREFIX}</code>"
        ),
        bot=bot_username,
        username_bot=bot_username,
        prefix=PREFIX,
    )

    kb = await button_engine.inline(SCREEN_PESQUISAR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
            ]
        )
    return rendered.content, kb


# ============================================================
# HANDLER — ABRIR PESQUISA
# ============================================================
@router.callback_query(F.data == CB_SEARCH_OPEN)
async def on_open_search(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "search", cooldown=0.4):
            await callback.answer()
            return

        text, kb = await _render_search_help(user)

        # Guarda estado
        await FlowState.set(
            user_id=user.id,
            name=FLOW_SEARCH,
            data={
                "step": "awaiting_query",
                "chat_id": callback.message.chat.id if callback.message else user.telegram_id,
            },
            ttl=10 * 60,
        )

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            # Pede a query via Reply
            try:
                await callback.message.answer(
                    text="⌨️ Envie sua busca:",
                    reply_markup=build_cancel_reply(placeholder=f"{PREFIX} nome do serviço"),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_open_search falhou: {exc}")


# ============================================================
# HANDLER — COMANDO /buscar
# ============================================================
@router.message(Command("buscar"))
async def on_cmd_buscar(message: Message, user: User) -> None:
    try:
        raw = (message.text or "")
        # Apaga a mensagem do usuário
        try:
            await message.delete()
        except Exception:
            pass

        q = _extract_query(raw)
        if q is None or q == "":
            text, kb = await _render_search_help(user)
            await message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return

        await _handle_search_results(message, user, q)
    except Exception as exc:
        logger.exception(f"/buscar falhou: {exc}")


# ============================================================
# HANDLER — MENSAGEM DE BUSCA (via Reply)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_search_message(message: Message, user: User) -> None:
    """
    Só trata mensagens de texto se o usuário estiver:
      - no fluxo de busca (FlowState), OU
      - enviando explicitamente "procurar <...>"
    Caso contrário, deixa outros handlers.
    """
    state = await FlowState.get(user.id, FLOW_SEARCH)
    raw = (message.text or "").strip()

    # Verifica se é comando de busca
    q = _extract_query(raw)

    if state is None and q is None:
        return  # não é busca → deixa os outros handlers

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_SEARCH)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
        return

    # Apaga a mensagem do usuário
    try:
        await message.delete()
    except Exception:
        pass

    # Se veio pelo prefixo
    if q is not None and q != "":
        await _handle_search_results(message, user, q)
        return

    # Se veio pelo fluxo (sem prefixo), tenta usar direto
    if state and state.get("step") == "awaiting_query":
        if not raw:
            text, kb = await _render_search_help(user)
            await message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return
        await _handle_search_results(message, user, raw)
        return


# ============================================================
# 🔎 EXECUTA A BUSCA E MOSTRA O RESULTADO
# ============================================================
async def _handle_search_results(
    message: Message,
    user: User,
    query: str,
) -> None:
    query = query.strip()
    await FlowState.clear(user.id, FLOW_SEARCH)

    results = await _search_products(query, limit=MAX_RESULTS)

    if not results:
        rendered = await message_engine.render(
            MSG_KEY_SEM_RESULTADO,
            fallback=(
                f"🔍 <b>Nenhum resultado para</b> \"{query}\"\n\n"
                "Tente outro termo ou veja o catálogo completo."
            ),
            busca=query,
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛍️ Ver Catálogo", callback_data="catalog:open")],
            ]
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        return

    # ---------- Monta o resultado (DIM: 1 resultado) ----------
    p = results[0]
    preco = float(p.price_promo or p.price or 0)
    descricao = (p.description or p.subtitle or "—")
    if len(descricao) > 180:
        descricao = descricao[:177] + "..."

    rendered = await message_engine.render(
        MSG_KEY_RESULTADO,
        fallback=(
            f"🔍 Resultado para \"{query}\"\n\n"
            f"🎯 <b>{p.name}</b>\n"
            f"💲 Valor: <b>{_fmt_brl(preco)}</b>\n"
            f"📝 {descricao}\n\n"
            "Para comprar, clique no botão \"💳 Comprar\" abaixo."
        ),
        busca=query,
        nome_produto=p.name,
        preco=preco,
        descricao_curta=descricao,
    )

    # Teclado: tenta painel, senão fallback
    kb = await button_engine.inline(SCREEN_RESULTADO)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Comprar",
                        callback_data=f"{CB_SEARCH_BUY}:{p.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data="catalog:open",
                    )
                ],
            ]
        )

    await message.answer(
        text=rendered.content,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


# ============================================================
# HANDLER — BOTÃO "💳 Comprar" no resultado
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SEARCH_BUY}:"))
async def on_search_buy(callback: CallbackQuery, user: User) -> None:
    try:
        product_id = int(callback.data.split(":")[-1])
    except Exception:
        await callback.answer("❌ Produto inválido.", show_alert=True)
        return

    # Delega para o detalhe do produto (catálogo)
    try:
        from bot.handlers.catalog import on_product_detail

        # Reencaminha como se o usuário tivesse clicado em "catalog:product:ID"
        callback.data = f"catalog:product:{product_id}"  # type: ignore[misc]
        await on_product_detail(callback, user)
    except Exception as exc:
        logger.debug(f"delegação para catálogo falhou: {exc}")
        await callback.answer(
            "❌ Não foi possível abrir o produto.",
            show_alert=True,
        )


# ============================================================
# CANCELAR
# ============================================================
@router.message(Command("cancelar"))
async def on_cancel_search(message: Message, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_SEARCH)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
    except Exception:
        pass


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_search",
    "on_cmd_buscar",
    "on_search_message",
    "on_search_buy",
    "on_cancel_search",
    "CB_SEARCH_OPEN",
    "CB_SEARCH_BUY",
    "FLOW_SEARCH",
    "PREFIX",
]
