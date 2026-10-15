"""
Larizinha Store — Middleware Channel Guard
Regras invioláveis:
- Bloqueio de canal tem SOMENTE 1 BOTÃO.
- O link do canal é HTML (<a href>), fica roxo/azul, 100% editável pelo Admin.
- NENHUMA mensagem acumula — sempre EDITA quando possível.
- Ao detectar ChatMemberUpdated (entrou no canal/grupo) → edita para Boas-vindas.
- Se o usuário clicar em qualquer coisa enquanto não entrou, EDITA a mesma
  mensagem de bloqueio (não envia nova).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiogram import BaseMiddleware, F
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import select

from bot.config import settings
from bot.database import Channel, ChannelType, session_scope
from bot.logger import logger
from bot.redis_client import cache_get, cache_set


# ============================================================
# 🔑 CONSTANTES
# ============================================================
CALLBACK_CHECK_MEMBERSHIP = "channel:check"

CACHE_KEY_CHANNELS = "required_channels"
CACHE_TTL_CHANNELS = 60          # segundos — painel pode alterar

MSG_KEY_BLOQUEIO = "start_bloqueio"       # chave editável no painel admin
MSG_KEY_BOAS_VINDAS = "boas_vindas"       # chave editável no painel admin


# ============================================================
# 🛰️ HELPERS
# ============================================================
async def _load_required_channels() -> List[Dict[str, Any]]:
    """
    Carrega os canais/grupos obrigatórios do banco (com cache).
    Retorna lista de dicts com: chat_id, username, title, invite_link, type.
    """
    cached = await cache_get(CACHE_KEY_CHANNELS, "all")
    if cached:
        return cached

    async with session_scope() as session:
        result = await session.execute(
            select(Channel).where(Channel.required == True)  # noqa: E712
        )
        rows = result.scalars().all()

    items: List[Dict[str, Any]] = []
    for ch in rows:
        items.append(
            {
                "chat_id": int(ch.chat_id),
                "username": ch.username,
                "title": ch.title,
                "invite_link": ch.invite_link,
                "type": ch.type.value if hasattr(ch.type, "value") else str(ch.type),
            }
        )

    await cache_set(CACHE_KEY_CHANNELS, "all", items, ttl=CACHE_TTL_CHANNELS)
    return items


async def _is_member(bot, chat_id: int, user_id: int) -> bool:
    """Verifica se o usuário é membro do chat."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, member_id=user_id)
        status = getattr(member, "status", None)
        return status in ("member", "administrator", "creator", "restricted")
    except Exception as exc:
        # Se o bot não conseguir verificar (ex.: não é admin do canal),
        # considera como "não membro" para forçar o usuário a entrar.
        logger.debug(f"get_chat_member falhou (chat={chat_id}, user={user_id}): {exc}")
        return False


async def check_user_membership(bot, user_id: int) -> bool:
    """
    Verifica TODOS os canais obrigatórios.
    Retorna True SOMENTE se o usuário estiver em todos.
    """
    channels = await _load_required_channels()
    if not channels:
        return True
    for ch in channels:
        ok = await _is_member(bot, ch["chat_id"], user_id)
        if not ok:
            return False
    return True


# ============================================================
# 💬 TEXTO DO BLOQUEIO — LINK HTML EDITÁVEL
# ============================================================
def _build_block_text(channels: List[Dict[str, Any]]) -> str:
    """
    Monta o texto do bloqueio com os links em HTML (<a href>).
    Links ficam ROXO/AZUL por padrão do Telegram.
    O Admin pode sobrescrever esse texto via painel (key: start_bloqueio).
    """
    if not channels:
        return "❗️ Para usar o bot, entre nos canais obrigatórios."

    lines: List[str] = []
    lines.append("❗️ <b>Para utilizar nosso serviço é obrigatório</b>")
    lines.append("<b>que você entre no(s) canal(is) abaixo:</b>")
    lines.append("")

    for ch in channels:
        link = ch.get("invite_link") or (
            f"https://t.me/{ch['username']}" if ch.get("username") else None
        )
        if not link:
            continue
        display = f"@{ch['username']}" if ch.get("username") else ch["title"]
        lines.append(f"📢 <a href=\"{link}\">{display}</a>")

    lines.append("")
    lines.append("<i>Depois de entrar, clique em “✅ Verificar” abaixo.</i>")
    return "\n".join(lines)


def _build_block_keyboard(channels: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    REGRA: SOMENTE 1 BOTÃO de ação.
    O link é exibido dentro do TEXTO (HTML) — não é botão.
    O único botão é “✅ Verificar”, que reenvia o check.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Verificar", callback_data=CALLBACK_CHECK_MEMBERSHIP)]
        ]
    )


# ============================================================
# 🛡️ MIDDLEWARE
# ============================================================
class ChannelGuardMiddleware(BaseMiddleware):
    """
    Deve ser registrado como OUTER middleware de dp.message e dp.callback_query.

    Fluxo:
    - Se usuário é admin → libera.
    - Se não há canais obrigatórios → libera.
    - Se usuário já entrou em todos → libera.
    - Senão → EDITA (callback) ou envia bloqueio (message) com 1 botão.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # ------- Admin bypass -------
        if user.id in settings.admin_ids_list:
            return await handler(event, data)

        # ------- Callback especial "channel:check" ------
        # (deixa o handler dedicado tratar — só passa adiante)
        if isinstance(event, CallbackQuery) and event.data == CALLBACK_CHECK_MEMBERSHIP:
            return await handler(event, data)

        # ------- Carrega canais ------
        channels = await _load_required_channels()
        if not channels:
            return await handler(event, data)

        # ------- Checa memberships ------
        bot = data.get("bot") or event.bot  # type: ignore[attr-defined]
        is_member = await check_user_membership(bot, user.id)
        if is_member:
            return await handler(event, data)

        # ------- Não é membro → BLOQUEIA -------
        text = _build_block_text(channels)
        keyboard = _build_block_keyboard(channels)

        try:
            if isinstance(event, Message):
                # Apaga a mensagem do usuário para não poluir
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.answer(
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

            elif isinstance(event, CallbackQuery):
                # EDITA a mesma mensagem (nunca acumula)
                try:
                    await event.answer("❗️ Entre no canal para continuar.", show_alert=False)
                except Exception:
                    pass
                if event.message:
                    try:
                        await event.message.edit_text(
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception:
                        # se não conseguir editar (ex.: mensagem de foto), manda nova
                        await event.message.answer(
                            text=text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
        except Exception as exc:
            logger.debug(f"Falha ao enviar bloqueio de canal: {exc}")

        return None


# ============================================================
# 🔄 HANDLER DE ChatMemberUpdated (auto desbloqueio)
# ============================================================
class ChannelWatcherMiddleware(BaseMiddleware):
    """
    Registrado especificamente em `dp.chat_member`.
    Quando o usuário entra no canal/grupo obrigatório,
    EDITA a última mensagem de bloqueio (se houver) para Boas-vindas.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, ChatMemberUpdated):
            return await handler(event, data)

        try:
            old_status = event.old_chat_member.status
            new_status = event.new_chat_member.status
            user = event.new_chat_member.user

            # Só interessa quando o usuário PASSOU a ser membro
            entered = old_status in ("left", "kicked") and new_status in (
                "member",
                "administrator",
                "creator",
                "restricted",
            )
            if not entered:
                return await handler(event, data)

            # Checa se TODOS os canais agora estão cumpridos
            bot = event.bot
            ok = await check_user_membership(bot, user.id)
            if not ok:
                # Ainda falta algum canal → mantém bloqueio
                return await handler(event, data)

            # Marca desbloqueio no cache curto
            await cache_set("membership_ok", str(user.id), True, ttl=60)

            # Envia boas-vindas automáticas (o handler de /start já reage)
            # Aqui apenas registramos — o handler principal trata o /start.
            logger.bind(user_id=user.id).info(
                f"Usuário {user.id} entrou no canal obrigatório — desbloqueado."
            )

            # Notifica o handler (se houver) para reenviar boas-vindas
            data["channel_unlocked"] = True
            return await handler(event, data)

        except Exception as exc:
            logger.exception(f"ChannelWatcher falhou: {exc}")
            return await handler(event, data)


# ============================================================
# 🎛️ HANDLERS AUXILIARES (botão “✅ Verificar”)
# ============================================================
from aiogram import Router

channel_router = Router(name="channel_guard")


@channel_router.callback_query(F.data == CALLBACK_CHECK_MEMBERSHIP)
async def on_check_membership(callback: CallbackQuery, user=None):
    """
    Chamado quando o usuário clica em “✅ Verificar”.
    Revalida e:
      - Se entrou em todos → edita para Boas-vindas.
      - Se ainda falta → edita a MESMA mensagem de bloqueio.
    """
    bot = callback.bot
    tg_user = callback.from_user

    channels = await _load_required_channels()
    if not channels:
        try:
            await callback.answer("✅ Liberado!", show_alert=False)
        except Exception:
            pass
        # segue para boas-vindas via comando start
        try:
            await callback.message.edit_text(
                "✅ Tudo pronto! Envie /start para começar.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    ok = await check_user_membership(bot, tg_user.id)

    if ok:
        # ---------- SUCESSO: edita para BOAS-VINDAS ----------
        try:
            await callback.answer("✅ Verificado! Bem-vindo(a).", show_alert=False)
        except Exception:
            pass

        # Carrega texto de boas-vindas editável (fallback local)
        welcome_text = await _render_welcome_text(tg_user)
        welcome_kb = await _render_welcome_keyboard()

        try:
            await callback.message.edit_text(
                text=welcome_text,
                reply_markup=welcome_kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await callback.message.answer(
                text=welcome_text,
                reply_markup=welcome_kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        return

    # ---------- AINDA FALTANDO: EDITA A MESMA MENSAGEM ----------
    try:
        await callback.answer("❗️ Você ainda não entrou em todos os canais.", show_alert=True)
    except Exception:
        pass

    text = _build_block_text(channels)
    keyboard = _build_block_keyboard(channels)

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ============================================================
# 🧾 RENDER DE BOAS-VINDAS (fallback — painel admin sobrescreve)
# ============================================================
async def _render_welcome_text(tg_user) -> str:
    """
    Texto padrão de boas-vindas.
    Se existir MessageTemplate key=boas_vindas no banco, usa ela.
    """
    # tenta pegar do banco (message_engine é o ideal — usamos aqui fallback)
    try:
        from bot.database import MessageTemplate
        from bot.services.message_engine import render_template  # opcional

        async with session_scope() as session:
            result = await session.execute(
                select(MessageTemplate).where(MessageTemplate.key == MSG_KEY_BOAS_VINDAS)
            )
            tpl = result.scalar_one_or_none()
        if tpl and tpl.content:
            # tenta renderizar com variáveis simples
            try:
                return await render_template(MSG_KEY_BOAS_VINDAS, user=tg_user)
            except Exception:
                return tpl.content
    except Exception:
        pass

    # fallback hardcoded (não ideal — painel admin deve sobrescrever)
    return (
        f"🎬 <b>Bem-vindo(a) à {settings.APP_NAME}</b> ✨\n\n"
        f"👤 ID: <code>{tg_user.id}</code>\n\n"
        f"👇 <b>Como começar:</b>\n"
        f"Use os botões abaixo para navegar."
    )


async def _render_welcome_keyboard() -> Optional[InlineKeyboardMarkup]:
    """
    Teclado de boas-vindas fallback.
    Se existir template editável no painel, ele é usado pelo motor de botões.
    """
    try:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛍️ Comprar Produtos", callback_data="catalog:page:1")],
            ]
        )
    except Exception:
        return None


# ============================================================
# 🧩 REGISTRADOR
# ============================================================
def register_channel_guard(dp, router_owner: Optional[Router] = None) -> None:
    """
    Registra:
      - Middleware global de guarda (message + callback_query)
      - Middleware de chat_member
      - Router auxiliar com o callback “✅ Verificar”
    """
    guard = ChannelGuardMiddleware()
    watcher = ChannelWatcherMiddleware()

    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.chat_member.outer_middleware(watcher)

    # Router do botão Verificar
    dp.include_router(channel_router)

    logger.info("Channel guard middleware registrado.")


__all__ = [
    "ChannelGuardMiddleware",
    "ChannelWatcherMiddleware",
    "channel_router",
    "check_user_membership",
    "register_channel_guard",
    "CALLBACK_CHECK_MEMBERSHIP",
]
