"""
Larizinha Store — Handler da Loja (WebApp)
Abre a Loja WebApp para o usuário via botão `web_app` do Telegram.

Regras invioláveis (DIM):
- Saldo no topo + botão Adicionar Saldo dentro do WebApp.
- Valores rápidos (R$ 30, R$ 60) + campo livre.
- Foto + username do Telegram.
- Catálogo, imagens, descrições e quantidade disponível.
- Carrinho virtual com subtotal.
- Estoque em tempo real.
- Imagens WebApp independentes das do Telegram.
- Textos e botões do WebApp editáveis pelo Admin.

Este handler:
- Gera URL ASSINADA com HMAC-SHA256 (usando WEBAPP_SECRET).
- Envia mensagem editável (key="loja_webapp") com botão `web_app`.
- NUNCA acumula — edita sempre que chamado por callback.
- O WebApp validará a assinatura com o mesmo segredo no backend.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from bot.config import settings
from bot.database import User
from bot.logger import logger
from bot.redis_client import Presence, Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="shop")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LOJA = "loja_webapp"
SCREEN_LOJA = "loja_webapp"

CALLBACK_OPEN_SHOP = "shop:open"
CALLBACK_OPEN_WALLET = "shop:wallet"

URL_AUTH_TTL = settings.WEBAPP_AUTH_MAX_AGE or 3600


# ============================================================
# 🔐 ASSINATURA HMAC (URL TIPADA)
# ============================================================
def _sign_payload(payload: dict) -> str:
    """
    Assina o payload com HMAC-SHA256.
    O WebApp verifica a assinatura usando o mesmo WEBAPP_SECRET.
    """
    if not settings.WEBAPP_SECRET:
        # Sem segredo configurado, usa token do bot como fallback mínimo
        secret = (settings.BOT_TOKEN or "larizinha").encode()
    else:
        secret = settings.WEBAPP_SECRET.encode()

    # Ordena as chaves para garantir determinismo
    items = sorted(payload.items())
    message = "&".join(f"{k}={v}" for k, v in items).encode("utf-8")

    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def build_webapp_url(
    *,
    user_id: int,
    telegram_id: int,
    path: str = "/",
    extra: Optional[dict] = None,
) -> str:
    """
    Gera a URL assinada para o WebApp.

    - Inclui: uid, tid, ts (timestamp) e hash HMAC.
    - `path` é relativo ao WEBAPP_URL (ex.: "/", "/wallet", "/orders").
    - `extra` permite adicionar parâmetros específicos.
    """
    base = (settings.WEBAPP_URL or "").rstrip("/")
    if not base:
        # Se não houver WEBAPP_URL configurado, retorna string vazia
        return ""

    ts = int(time.time())
    payload = {
        "uid": user_id,
        "tid": telegram_id,
        "ts": ts,
        "exp": ts + URL_AUTH_TTL,
    }
    if extra:
        for k, v in extra.items():
            if v is None:
                continue
            payload[k] = v

    signature = _sign_payload(payload)
    payload["sig"] = signature

    # path já com "/" inicial
    if not path.startswith("/"):
        path = "/" + path

    query = urlencode(payload)
    return f"{base}{path}?{query}"


# ============================================================
# 🧰 HELPERS DE RENDER
# ============================================================
async def _render_shop_message(user: User) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Monta a mensagem editável key="loja_webapp".
    Se houver WEBAPP_URL, monta botão `web_app`; senão, cai para URL comum.
    """
    rendered = await message_engine.render(
        MSG_KEY_LOJA,
        fallback=(
            "🛒 <b>LOJA LARIZINHA STORE</b>\n\n"
            f"👤 <b>{user.first_name or 'Cliente'}</b>\n"
            f"💰 Saldo atual: <b>R$ {float(user.balance or 0):.2f}</b>\n\n"
            "Clique no botão abaixo para abrir a loja e adicionar saldo "
            "diretamente pelo mini-app."
        ),
        nome=user.first_name or "Cliente",
        id=user.telegram_id,
        saldo=float(user.balance or 0),
        nome_loja=settings.APP_NAME,
    )

    # ------- Tenta teclado editável do painel -------
    keyboard = await button_engine.inline(SCREEN_LOJA)
    if keyboard is not None:
        return rendered.content, keyboard

    # ------- Fallback: botão direto apontando pro WebApp -------
    base = (settings.WEBAPP_URL or "").rstrip("/")
    if not base:
        return rendered.content, None

    url = build_webapp_url(
        user_id=user.id,
        telegram_id=user.telegram_id,
        path="/",
    )
    if not url:
        return rendered.content, None

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Abrir Loja", web_app=WebAppInfo(url=url))],
            [
                InlineKeyboardButton(
                    text="💰 Adicionar Saldo",
                    web_app=WebAppInfo(
                        url=build_webapp_url(
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                            path="/wallet",
                        )
                    ),
                )
            ],
        ]
    )
    return rendered.content, kb


# ============================================================
# HANDLER: BOTÃO "🛒 Abrir Loja" (callback)
# ============================================================
@router.callback_query(F.data == CALLBACK_OPEN_SHOP)
async def on_open_shop(callback: CallbackQuery, user: User) -> None:
    """
    Abre a Loja WebApp.
    - Se vindo de callback com message → EDITA a mensagem atual.
    - Throttle interno evita duplo clique.
    """
    try:
        if not await Throttle.allow(user.telegram_id, "open_shop", cooldown=1.0):
            try:
                await callback.answer()
            except Exception:
                pass
            return

        try:
            await callback.answer()
        except Exception:
            pass

        text, keyboard = await _render_shop_message(user)

        if callback.message is not None:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                # fallback: envia nova se a edição falhar
                try:
                    await callback.message.answer(
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass

        await Presence.beat(user.telegram_id)
    except Exception as exc:
        logger.exception(f"on_open_shop falhou: {exc}")


# ============================================================
# HANDLER: BOTÃO "💰 Adicionar Saldo"
# ============================================================
@router.callback_query(F.data == CALLBACK_OPEN_WALLET)
async def on_open_wallet(callback: CallbackQuery, user: User) -> None:
    """
    Abre o WebApp direto na tela de Carteira.
    """
    try:
        try:
            await callback.answer()
        except Exception:
            pass

        base = (settings.WEBAPP_URL or "").rstrip("/")
        if not base:
            # Sem webapp configurado → redireciona para fluxo de recarga padrão
            try:
                from bot.handlers.recharge import on_recharge_button  # opcional

                await on_recharge_button(callback, user)
                return
            except Exception:
                await callback.message.edit_text(
                    "💠 <b>Recarregar Saldo</b>\n\n"
                    "Use o botão 💠 Recarregar Saldo no menu principal.",
                    parse_mode="HTML",
                )
                return

        url = build_webapp_url(
            user_id=user.id,
            telegram_id=user.telegram_id,
            path="/wallet",
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💰 Abrir Carteira",
                        web_app=WebAppInfo(url=url),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CALLBACK_OPEN_SHOP,
                    )
                ],
            ]
        )

        if callback.message is not None:
            try:
                await callback.message.edit_text(
                    text=(
                        "💰 <b>CARTEIRA</b>\n\n"
                        f"👤 {user.first_name or 'Cliente'}\n"
                        f"💸 Saldo: <b>R$ {float(user.balance or 0):.2f}</b>\n\n"
                        "Clique no botão abaixo para abrir a carteira e adicionar "
                        "saldo via PIX."
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_open_wallet falhou: {exc}")


# ============================================================
# HANDLER: /loja (comando opcional)
# ============================================================
@router.message(F.text == "/loja")
async def on_loja_command(message: Message, user: User) -> None:
    """
    Comando /loja — mesma função do botão 🛒 Abrir Loja.
    Não está no DIM oficial, mas é útil para deep-links externos.
    """
    try:
        try:
            await message.delete()
        except Exception:
            pass

        text, keyboard = await _render_shop_message(user)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await Presence.beat(user.telegram_id)
    except Exception as exc:
        logger.exception(f"/loja falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_shop",
    "on_open_wallet",
    "on_loja_command",
    "build_webapp_url",
    "_sign_payload",
    "CALLBACK_OPEN_SHOP",
    "CALLBACK_OPEN_WALLET",
]
