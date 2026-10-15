"""
Larizinha Store — Middleware de Throttle (anti-flood)
- Bloqueia spam por usuário via Redis (Throttle.allow)
- Não acumula mensagens: se o usuário spammar, editamos a última mensagem
  do bot com um aviso suave e removemos a mensagem do usuário.
- Regras diferentes por tipo de update (message, callback_query).
- Whitelist para admins (não sofrem throttle pesado).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.config import settings
from bot.logger import logger
from bot.redis_client import Throttle


# ============================================================
# ⚙️ CONFIGURAÇÕES DE THROTTLE POR TIPO
# ============================================================
DEFAULT_COOLDOWN_MESSAGE = 1.0       # segundos entre mensagens comuns
DEFAULT_COOLDOWN_CALLBACK = 0.4      # segundos entre cliques de botão
DEFAULT_COOLDOWN_SENSITIVE = 2.0     # ações sensíveis (PIX, saque, compra)
ADMIN_COOLDOWN = 0.15                # admins quase não têm limite

SENSITIVE_ACTIONS = {
    "pix",
    "withdraw",
    "buy",
    "recharge",
    "gift",
    "sacar",
}

# Cooldowns específicos por "action name" — pode ser estendido
COOLDOWN_MAP: Dict[str, float] = {
    "message": DEFAULT_COOLDOWN_MESSAGE,
    "callback": DEFAULT_COOLDOWN_CALLBACK,
    "pix": DEFAULT_COOLDOWN_SENSITIVE,
    "withdraw": DEFAULT_COOLDOWN_SENSITIVE,
    "buy": DEFAULT_COOLDOWN_SENSITIVE,
    "recharge": DEFAULT_COOLDOWN_SENSITIVE,
    "gift": DEFAULT_COOLDOWN_SENSITIVE,
    "sacar": DEFAULT_COOLDOWN_SENSITIVE,
}


# ============================================================
# 💬 MENSAGEM DE AVISO (editável no painel admin pela key)
# ============================================================
THROTTLE_MESSAGE_KEY = "throttle_aviso"

_FALLBACK_THROTTLE_TEXT = (
    "⏳ <b>Calma aí!</b>\n\n"
    "Você está enviando mensagens muito rápido.\n"
    "Aguarde <b>{seconds}s</b> e tente novamente."
)


class ThrottleMiddleware(BaseMiddleware):
    """
    Middleware global. Deve ser registrado como OUTER middleware
    em dp.message e dp.callback_query.
    """

    def __init__(
        self,
        *,
        action_name: str = "message",
        cooldown: Optional[float] = None,
        silent: bool = False,
    ) -> None:
        self.action_name = action_name
        self.cooldown = cooldown
        self.silent = silent

    # --------------------------------------------------------
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[User] = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # ---------- Admin bypass ----------
        if user.id in settings.admin_ids_list:
            return await handler(event, data)

        # ---------- Cooldown efetivo ----------
        cooldown = self.cooldown
        if cooldown is None:
            cooldown = COOLDOWN_MAP.get(self.action_name, DEFAULT_COOLDOWN_MESSAGE)

        # ---------- Checa no Redis ----------
        allowed = await Throttle.allow(
            user_id=user.id,
            action=self.action_name,
            cooldown=cooldown,
        )

        if allowed:
            return await handler(event, data)

        # ---------- BLOQUEADO ----------
        # Não acumula: apenas avisa (silencioso ou edita)
        if self.silent:
            return None

        remaining = await Throttle.remaining(user_id=user.id, action=self.action_name)
        remaining = max(1, remaining)

        try:
            if isinstance(event, CallbackQuery):
                await self._warn_callback(event, remaining)
            elif isinstance(event, Message):
                await self._warn_message(event, remaining)
        except Exception as exc:
            logger.debug(f"Throttle warn falhou: {exc}")

        return None

    # --------------------------------------------------------
    # Avisos
    # --------------------------------------------------------
    async def _warn_callback(self, event: CallbackQuery, remaining: int) -> None:
        """Em callback, apenas um toast silencioso — não polui o chat."""
        try:
            await event.answer(
                f"⏳ Aguarde {remaining}s...",
                show_alert=False,
            )
        except Exception:
            pass

    async def _warn_message(self, event: Message, remaining: int) -> None:
        """
        Em mensagem comum:
        - Apaga a mensagem do usuário (se possível)
        - Edita a última mensagem do bot (se houver) ou envia aviso simples
        """
        bot = event.bot
        chat_id = event.chat.id

        # Tenta apagar a mensagem do usuário
        try:
            await event.delete()
        except Exception:
            pass

        text = _FALLBACK_THROTTLE_TEXT.format(seconds=remaining)

        # Tenta editar a última mensagem do bot enviada neste chat (se cacheada)
        # Sem cache, cai no fallback: envia um aviso curto, que será substituído
        # na próxima interação natural do usuário.
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            # Agenda exclusão do aviso após cooldown (para não acumular)
            from asyncio import create_task, sleep

            async def _auto_delete() -> None:
                await sleep(max(2, remaining))
                try:
                    await sent.delete()
                except Exception:
                    pass

            create_task(_auto_delete())
        except Exception as exc:
            logger.debug(f"Falha ao enviar aviso de throttle: {exc}")


# ============================================================
# 📌 INSTÂNCIAS PRONTAS
# ============================================================
# Mensagens comuns (1s entre cada)
throttle_message = ThrottleMiddleware(action_name="message", cooldown=1.0)

# Cliques em botões (0.4s entre cada)
throttle_callback = ThrottleMiddleware(action_name="callback", cooldown=0.4)

# Ações sensíveis (PIX, saque, compra)
throttle_sensitive = ThrottleMiddleware(action_name="pix", cooldown=2.0)

# Throttle silencioso (não avisa — só ignora)
throttle_silent = ThrottleMiddleware(action_name="silent", cooldown=0.5, silent=True)


# ============================================================
# 🧩 REGISTRADOR
# ============================================================
def register_throttle(dp) -> None:
    """
    Registra os middlewares de throttle no Dispatcher.
    Uso em `bot/main.py`:
        register_throttle(dp)
    """
    dp.message.outer_middleware(throttle_message)
    dp.callback_query.outer_middleware(throttle_callback)
    logger.info("Throttle middleware registrado.")


__all__ = [
    "ThrottleMiddleware",
    "throttle_message",
    "throttle_callback",
    "throttle_sensitive",
    "throttle_silent",
    "register_throttle",
    "COOLDOWN_MAP",
]
