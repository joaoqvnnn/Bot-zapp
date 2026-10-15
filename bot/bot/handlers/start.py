"""
Larizinha Store — Handler de Start / Boas-vindas / Bloqueio de canal
Complementa o middleware `channel_guard`, garantindo:

- Se o usuário NÃO cumpre os canais obrigatórios:
    → envia/edita a mensagem de BLOQUEIO (key="start_bloqueio")
    → com SOMENTE 1 botão: "✅ Verificar" (callback: channel:check)
    → link HTML <a href="..."> no texto (roxo/azul do Telegram)

- Se cumpre:
    → envia boas-vindas (key="boas_vindas")
    → teclado renderizado pelo button_engine (screen="boas_vindas")
    → registra Presence.beat()

- Detecta ChatMemberUpdated → usuário acabou de entrar no canal
    → edita a última mensagem de bloqueio (se conhecida) para boas-vindas
    → ou envia boas-vindas se não houver mensagem para editar

Regras invioláveis:
- 1 único botão no bloqueio.
- NUNCA acumula mensagens.
- Todas as mensagens e botões editáveis pelo painel admin.
"""
from __future__ import annotations

from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.database import User, UserStatus, session_scope
from bot.logger import logger
from bot.middlewares.channel_guard import (
    CALLBACK_CHECK_MEMBERSHIP,
    _build_block_keyboard,
    _build_block_text,
    _load_required_channels,
    check_user_membership,
)
from bot.redis_client import Presence, cache_set
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="start")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_BLOQUEIO = "start_bloqueio"
MSG_KEY_BOAS_VINDAS = "boas_vindas"

SCREEN_BLOQUEIO = "start_bloqueio"
SCREEN_BOAS_VINDAS = "boas_vindas"

# Cache simples para saber qual mensagem editar após entrar no canal
CACHE_NS = "start"
CACHE_KEY_LAST_MSG = "lastmsg:{uid}"


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _save_last_message(user_id: int, message_id: int, chat_id: int) -> None:
    """Guarda referência da última mensagem de bloqueio enviada."""
    try:
        await cache_set(
            CACHE_NS,
            CACHE_KEY_LAST_MSG.format(uid=user_id),
            {"message_id": message_id, "chat_id": chat_id},
            ttl=60 * 60 * 24,
        )
    except Exception:
        pass


async def _load_last_message(user_id: int) -> Optional[dict]:
    from bot.redis_client import cache_get

    try:
        return await cache_get(CACHE_NS, CACHE_KEY_LAST_MSG.format(uid=user_id))
    except Exception:
        return None


async def _render_welcome(user: User) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Renderiza boas-vindas (texto + teclado) com dados reais do usuário."""
    rendered = await message_engine.render(
        MSG_KEY_BOAS_VINDAS,
        fallback=(
            f"🎬 <b>Bem-vindo(a) à {settings.APP_NAME}</b> ✨\n\n"
            f"👤 ID: <code>{user.telegram_id}</code>\n"
            f"💰 Saldo: R$ {float(user.balance or 0):.2f}\n\n"
            "👇 Use os botões abaixo para começar."
        ),
        nome=user.first_name or "Cliente",
        id=user.telegram_id,
        saldo=float(user.balance or 0),
        nome_loja=settings.APP_NAME,
        versao=settings.APP_VERSION,
        qtd_clientes=1000,
    )
    keyboard = await button_engine.inline(SCREEN_BOAS_VINDAS)
    return rendered.content, keyboard


async def _send_block(message: Message, user: User) -> None:
    """Envia mensagem de BLOQUEIO com 1 botão (link HTML no texto)."""
    channels = await _load_required_channels()

    # Tenta primeiro o template editável
    rendered = await message_engine.render(
        MSG_KEY_BLOQUEIO,
        fallback=_build_block_text(channels),
        nome=user.first_name or "Cliente",
        id=user.telegram_id,
    )

    # O teclado é SEMPRE o do channel_guard (1 botão apenas)
    keyboard = await button_engine.inline(SCREEN_BLOQUEIO) or _build_block_keyboard(channels)

    try:
        sent = await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await _save_last_message(user.telegram_id, sent.message_id, sent.chat.id)
    except Exception as exc:
        logger.debug(f"_send_block falhou: {exc}")


async def _edit_to_welcome(user_id: int, bot) -> bool:
    """
    Tenta editar a última mensagem de bloqueio para boas-vindas.
    Retorna True se editou; False se não encontrou / não conseguiu.
    """
    ref = await _load_last_message(user_id)
    if not ref:
        return False

    # Carrega user (por telegram_id)
    from sqlalchemy import select

    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.telegram_id == user_id)
            )
            user: Optional[User] = res.scalar_one_or_none()
            if user is None:
                return False
    except Exception:
        return False

    text, keyboard = await _render_welcome(user)

    try:
        await bot.edit_message_text(
            chat_id=ref.get("chat_id"),
            message_id=ref.get("message_id"),
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        logger.debug(f"_edit_to_welcome falhou: {exc}")
        return False


# ============================================================
# /start  (o middleware channel_guard já barra se não cumprir)
# ============================================================
@router.message(CommandStart())
async def on_start(message: Message, user: User) -> None:
    """
    Se o middleware channel_guard não bloqueou, é porque o usuário já
    cumpre os canais. Aqui só tratamos boas-vindas.
    """
    try:
        try:
            await message.delete()
        except Exception:
            pass

        # Reverifica (garantia dupla)
        channels = await _load_required_channels()
        if channels and not await check_user_membership(message.bot, user.telegram_id):
            await _send_block(message, user)
            return

        # Boas-vindas
        text, keyboard = await _render_welcome(user)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        await Presence.beat(user.telegram_id)
    except Exception as exc:
        logger.exception(f"on_start falhou: {exc}")


# ============================================================
# BOTÃO "✅ Verificar" (callback do channel_guard)
# ============================================================
@router.callback_query(F.data == CALLBACK_CHECK_MEMBERSHIP)
async def on_check(callback: CallbackQuery, user: User) -> None:
    """
    Revalida inscrição. Se liberou → edita para boas-vindas.
    Se ainda falta → edita a MESMA mensagem de bloqueio.
    """
    bot = callback.bot
    channels = await _load_required_channels()

    if not channels:
        # Sem canais obrigatórios → boas-vindas direto
        text, keyboard = await _render_welcome(user)
        try:
            await callback.answer("✅ Tudo certo!", show_alert=False)
        except Exception:
            pass
        try:
            await callback.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return

    ok = await check_user_membership(bot, user.telegram_id)

    if ok:
        # Liberado → boas-vindas (mesma mensagem, EDITADA)
        await cache_set(
            "membership_ok",
            str(user.telegram_id),
            True,
            ttl=60,
        )

        try:
            await callback.answer("✅ Verificado! Bem-vindo(a).", show_alert=False)
        except Exception:
            pass

        text, keyboard = await _render_welcome(user)
        try:
            await callback.message.edit_text(
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception:
            # fallback: envia nova
            try:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        return

    # Ainda faltando → mantém bloqueio (EDITA a mesma msg)
    try:
        await callback.answer(
            "❗️ Você ainda não entrou em todos os canais.",
            show_alert=True,
        )
    except Exception:
        pass

    rendered = await message_engine.render(
        MSG_KEY_BLOQUEIO,
        fallback=_build_block_text(channels),
        nome=user.first_name or "Cliente",
        id=user.telegram_id,
    )
    keyboard = (
        await button_engine.inline(SCREEN_BLOQUEIO)
        or _build_block_keyboard(channels)
    )

    try:
        await callback.message.edit_text(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ============================================================
# ChatMemberUpdated — usuário entrou no canal
# ============================================================
@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated) -> None:
    """
    Detecta quando o usuário entra no canal obrigatório.
    Se TODOS os canais forem cumpridos → edita a última mensagem
    de bloqueio para boas-vindas automaticamente.
    """
    try:
        old_status = event.old_chat_member.status
        new_status = event.new_chat_member.status
        user = event.new_chat_member.user

        entered = old_status in ("left", "kicked") and new_status in (
            "member",
            "administrator",
            "creator",
            "restricted",
        )
        if not entered:
            return

        ok = await check_user_membership(event.bot, user.id)
        if not ok:
            return  # ainda falta algum canal

        logger.bind(user_id=user.id).info(
            f"Usuário {user.id} liberou todos os canais — enviando boas-vindas."
        )

        # Marca no cache
        await cache_set("membership_ok", str(user.id), True, ttl=60)

        # Tenta editar a última mensagem de bloqueio
        edited = await _edit_to_welcome(user.id, event.bot)
        if edited:
            return

        # Se não conseguiu editar, envia boas-vindas no privado
        from sqlalchemy import select

        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.telegram_id == user.id)
            )
            db_user: Optional[User] = res.scalar_one_or_none()

        if db_user is None:
            return

        text, keyboard = await _render_welcome(db_user)
        try:
            await event.bot.send_message(
                chat_id=user.id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"on_chat_member falhou: {exc}")


# ============================================================
# Mensagem qualquer de usuário bloqueado (fallback)
# ============================================================
@router.message()
async def fallback_blocked(message: Message, user: User) -> None:
    """
    Se um usuário bloqueado enviar qualquer outra mensagem,
    reenvia a tela de bloqueio (não acumula — apaga a msg do user).
    """
    try:
        channels = await _load_required_channels()
        if not channels:
            return
        ok = await check_user_membership(message.bot, user.telegram_id)
        if ok:
            return

        try:
            await message.delete()
        except Exception:
            pass

        await _send_block(message, user)
    except Exception as exc:
        logger.debug(f"fallback_blocked falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_start",
    "on_check",
    "on_chat_member",
    "fallback_blocked",
]
