"""
Larizinha Store — Middleware User Loader
Responsável por:
- Garantir que TODO usuário que interage com o bot exista no banco.
- Criar o usuário automaticamente na primeira interação (/start ou qualquer msg).
- Sincronizar dados do Telegram (username, first_name, last_name, photo_url, is_premium).
- Atualizar `last_seen_at` em toda interação (com throttle para não pesar no DB).
- Disparar `Presence.beat()` (heartbeat de online no Redis).
- Verificar blacklist e status (banned/blocked).
- Injetar `user` no contexto do handler via `data["user"]`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser
from sqlalchemy import select

from bot.config import settings
from bot.database import User, UserStatus, session_scope
from bot.logger import logger
from bot.redis_client import Presence


# ============================================================
# 🔧 CONSTANTES
# ============================================================
LAST_SEEN_THROTTLE_SECONDS = 60      # atualiza last_seen no máximo 1x/min
CACHE_USER_KEY = "user:{uid}"
CACHE_USER_TTL = 300                 # cache do user por 5min


# ============================================================
# 💬 MENSAGENS EDITÁVEIS (fallback — Painel Admin pode sobrescrever)
# ============================================================
BANNED_TEXT = (
    "⛔ <b>Você foi banido.</b>\n\n"
    "Motivo: <i>{reason}</i>\n\n"
    "Se acredita que isso foi um engano, entre em contato com o suporte."
)
BLOCKED_TEXT = (
    "🚫 <b>Sua conta está bloqueada.</b>\n\n"
    "Motivo: <i>{reason}</i>\n\n"
    "Entre em contato com o suporte para mais informações."
)


# ============================================================
# 🧠 MIDDLEWARE
# ============================================================
class UserLoaderMiddleware(BaseMiddleware):
    """
    Middleware que roda ANTES de qualquer handler.
    Injeta `data["user"]` (model User já carregado/criado).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: Optional[TgUser] = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        # ----------------------------------------------------
        # 1) Heartbeat (online)
        # ----------------------------------------------------
        try:
            await Presence.beat(tg_user.id)
        except Exception as exc:
            logger.debug(f"Presence.beat falhou: {exc}")

        # ----------------------------------------------------
        # 2) Carrega/cria usuário no banco
        # ----------------------------------------------------
        try:
            user = await self._get_or_create(tg_user)
        except Exception as exc:
            logger.exception(f"Falha ao carregar/criar usuário {tg_user.id}: {exc}")
            # Não bloqueia o handler — deixa seguir sem user (improvável)
            return await handler(event, data)

        if user is None:
            return await handler(event, data)

        # ----------------------------------------------------
        # 3) Verifica status (banido / bloqueado)
        # ----------------------------------------------------
        if user.status == UserStatus.BANNED:
            await self._reject(event, data, BANNED_TEXT.format(reason=user.ban_reason or "—"))
            return None

        if user.status == UserStatus.BLOCKED:
            await self._reject(event, data, BLOCKED_TEXT.format(reason=user.block_reason or "—"))
            return None

        # ----------------------------------------------------
        # 4) Injeta no contexto
        # ----------------------------------------------------
        data["user"] = user
        data["user_id"] = user.id
        data["telegram_id"] = user.telegram_id

        return await handler(event, data)

    # --------------------------------------------------------
    async def _get_or_create(self, tg_user: TgUser) -> Optional[User]:
        """
        Busca o usuário no banco. Se não existir, cria.
        Sincroniza dados do Telegram de forma leve.
        """
        async with session_scope() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == tg_user.id)
            )
            user: Optional[User] = result.scalar_one_or_none()

            now = datetime.now(timezone.utc)

            if user is None:
                # --------- CRIA NOVO ---------
                user = User(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                    language_code=tg_user.language_code,
                    is_premium=bool(tg_user.is_premium),
                    status=UserStatus.ACTIVE,
                    balance=settings.INITIAL_BALANCE,
                    last_seen_at=now,
                    alerts_enabled=[],
                )
                session.add(user)
                await session.flush()  # garante o ID

                logger.bind(user_id=user.id).info(
                    f"Novo usuário criado: {tg_user.id} (@{tg_user.username or '—'})"
                )
            else:
                # --------- SINCRONIZA (somente se mudou) ---------
                changed = False
                if user.username != tg_user.username:
                    user.username = tg_user.username
                    changed = True
                if user.first_name != tg_user.first_name:
                    user.first_name = tg_user.first_name
                    changed = True
                if user.last_name != tg_user.last_name:
                    user.last_name = tg_user.last_name
                    changed = True
                if user.language_code != tg_user.language_code:
                    user.language_code = tg_user.language_code
                    changed = True
                if user.is_premium != bool(tg_user.is_premium):
                    user.is_premium = bool(tg_user.is_premium)
                    changed = True

                # --------- last_seen com throttle ---------
                should_update_seen = (
                    user.last_seen_at is None
                    or (now - user.last_seen_at).total_seconds() > LAST_SEEN_THROTTLE_SECONDS
                )
                if should_update_seen:
                    user.last_seen_at = now
                    changed = True

                if changed:
                    session.add(user)

            return user

    # --------------------------------------------------------
    async def _reject(
        self,
        event: TelegramObject,
        data: Dict[str, Any],
        text: str,
    ) -> None:
        """Avisa o usuário que está banido/bloqueado. Sem acumular mensagens."""
        try:
            if isinstance(event, Message):
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.answer(text, parse_mode="HTML", disable_web_page_preview=True)

            elif isinstance(event, CallbackQuery):
                try:
                    await event.answer("⛔ Acesso negado.", show_alert=True)
                except Exception:
                    pass
                if event.message:
                    try:
                        await event.message.edit_text(text, parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug(f"Falha ao rejeitar usuário: {exc}")


# ============================================================
# 📌 INSTÂNCIA PADRAO
# ============================================================
user_loader = UserLoaderMiddleware()


# ============================================================
# 🧩 REGISTRADOR
# ============================================================
def register_user_loader(dp) -> None:
    """
    Registra o middleware em message + callback_query.
    Uso em `bot/main.py`:
        register_user_loader(dp)
    """
    dp.message.outer_middleware(user_loader)
    dp.callback_query.outer_middleware(user_loader)
    logger.info("User loader middleware registrado.")


__all__ = [
    "UserLoaderMiddleware",
    "user_loader",
    "register_user_loader",
]
