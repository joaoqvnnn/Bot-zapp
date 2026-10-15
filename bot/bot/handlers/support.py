"""
Larizinha Store — Handler de Suporte e Sobre o Bot
Implementa:
  ✉️ Atendimento:
    • Mensagem editável key="atendimento"
    • Botão [💬 Abrir Atendimento] que usa link silencioso do Telegram
      no formato: https://t.me/m/{link_atendimento}
    • Botão [🔙 Voltar]

  🤖 Sobre o Bot:
    • Mensagem editável key="sobre_bot"
    • Versão, loja, contato WhatsApp do suporte
    • Botões [📱 WhatsApp] [🔙 Voltar]

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Link silencioso usa o formato oficial do Telegram (t.me/m/...).
- Se não houver link configurado, cai para o username do suporte (t.me/USER).
"""
from __future__ import annotations

from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.database import User
from bot.logger import logger
from bot.redis_client import Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="support")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_ATENDIMENTO = "atendimento"
MSG_KEY_SOBRE_BOT = "sobre_bot"

SCREEN_ATENDIMENTO = "atendimento"
SCREEN_SOBRE_BOT = "sobre_bot"

CB_SUPPORT_OPEN = "support:open"
CB_SUPPORT_ABRIR = "support:abrir"
CB_ABOUT_OPEN = "support:about"
CB_SUPPORT_VOLTAR = "support:voltar"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _build_silent_link() -> str:
    """
    Retorna o link silencioso de atendimento.
    Preferência: settings.SUPPORT_SILENT_LINK (formato: https://t.me/m/xxxxx)
    Fallback: usa o username público (t.me/username).
    """
    link = (settings.SUPPORT_SILENT_LINK or "").strip()
    if link:
        if not link.startswith("http"):
            link = f"https://t.me/m/{link.lstrip('/')}"
        return link

    # Fallback: WhatsApp/Suporte genérico (evita botão quebrado)
    fallback_username = (getattr(settings, "SUPPORT_USERNAME", "") or "").strip().lstrip("@")
    if fallback_username:
        return f"https://t.me/{fallback_username}"

    return ""


# ============================================================
# TELA — ATENDIMENTO
# ============================================================
async def _render_support(user: User) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    rendered = await message_engine.render(
        MSG_KEY_ATENDIMENTO,
        fallback=(
            "✉️ <b>ATENDIMENTO</b>\n\n"
            "Clique no botão abaixo para falar com nosso suporte.\n"
            "Uma conversa será aberta no seu privado com uma mensagem pronta."
        ),
        nome=user.first_name or "Cliente",
        id=user.telegram_id,
        nome_loja=settings.APP_NAME,
    )

    # ---------- Tenta teclado customizado do painel ----------
    kb = await button_engine.inline(SCREEN_ATENDIMENTO)
    if kb is not None:
        return rendered.content, kb

    # ---------- Fallback: botão único com link silencioso ----------
    link = _build_silent_link()

    rows = []
    if link:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Abrir Atendimento",
                    url=link,
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💬 Abrir Atendimento",
                    callback_data=CB_SUPPORT_ABRIR,
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# TELA — SOBRE O BOT
# ============================================================
async def _render_about(user: User) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    whatsapp_suporte = (getattr(settings, "SUPPORT_WHATSAPP", "") or "").strip()
    whatsapp_display = whatsapp_suporte or "não cadastrado"

    rendered = await message_engine.render(
        MSG_KEY_SOBRE_BOT,
        fallback=(
            "🤖 <b>SOBRE O BOT</b>\n"
            f"Versão: <b>{settings.APP_VERSION}</b>\n"
            f"Desenvolvido para <b>{settings.APP_NAME}</b>\n"
            f"📞 Contato: {whatsapp_display}"
        ),
        versao=settings.APP_VERSION,
        nome_loja=settings.APP_NAME,
        whatsapp_suporte=whatsapp_display,
        id=user.telegram_id,
    )

    # ---------- Tenta teclado customizado do painel ----------
    kb = await button_engine.inline(SCREEN_SOBRE_BOT)
    if kb is not None:
        return rendered.content, kb

    # ---------- Fallback ----------
    rows = []
    if whatsapp_suporte:
        digits = "".join(c for c in whatsapp_suporte if c.isdigit())
        if digits:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📱 WhatsApp",
                        url=f"https://wa.me/{digits}",
                    )
                ]
            )

    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# HANDLERS — ABRIR ATENDIMENTO
# ============================================================
@router.callback_query(F.data == CB_SUPPORT_OPEN)
async def on_open_support(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "support", cooldown=0.4):
            await callback.answer()
            return

        text, kb = await _render_support(user)

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
        logger.exception(f"on_open_support falhou: {exc}")


@router.callback_query(F.data == CB_SUPPORT_ABRIR)
async def on_open_support_fallback(callback: CallbackQuery, user: User) -> None:
    """
    Fallback usado quando não há link configurado.
    Apenas informa o usuário — o botão real deve ser configurado no painel.
    """
    try:
        try:
            await callback.answer(
                "✉️ Contate o suporte pelo canal informado.",
                show_alert=True,
            )
        except Exception:
            pass
    except Exception as exc:
        logger.debug(f"on_open_support_fallback falhou: {exc}")


# ============================================================
# HANDLERS — ABRIR SOBRE O BOT
# ============================================================
@router.callback_query(F.data == CB_ABOUT_OPEN)
async def on_open_about(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "about", cooldown=0.4):
            await callback.answer()
            return

        text, kb = await _render_about(user)

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
        logger.exception(f"on_open_about falhou: {exc}")


# ============================================================
# HANDLERS — VOLTAR (compatibilidade)
# ============================================================
@router.callback_query(F.data == CB_SUPPORT_VOLTAR)
async def on_support_back(callback: CallbackQuery, user: User) -> None:
    """Volta para a tela de boas-vindas."""
    try:
        try:
            await callback.answer()
        except Exception:
            pass

        try:
            from bot.handlers.start import _render_welcome

            text, kb = await _render_welcome(user)
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
        except Exception:
            if callback.message:
                try:
                    await callback.message.edit_text(
                        text="🏠 Voltando ao início...",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as exc:
        logger.debug(f"on_support_back falhou: {exc}")


# ============================================================
# COMANDOS DE ATALHO
# ============================================================
@router.message(Command("suporte"))
async def on_cmd_support(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        text, kb = await _render_support(user)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/suporte falhou: {exc}")


@router.message(Command("sobre"))
async def on_cmd_about(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        text, kb = await _render_about(user)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/sobre falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_support",
    "on_open_support_fallback",
    "on_open_about",
    "on_support_back",
    "on_cmd_support",
    "on_cmd_about",
    "CB_SUPPORT_OPEN",
    "CB_SUPPORT_ABRIR",
    "CB_ABOUT_OPEN",
    "CB_SUPPORT_VOLTAR",
]
