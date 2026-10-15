"""
Larizinha Store — Message Engine
Motor que renderiza TODAS as mensagens a partir da tabela `message_templates`
no banco, permitindo edição total pelo painel admin.

Regras invioláveis:
- NADA de mensagem hardcoded no bot.
- Todas as variáveis {{...}} são resolvidas em runtime.
- Cache em Redis (TTL curto) + invalidação pelo painel admin.
- NUNCA acumula mensagem — sempre que possível, EDITA (edit_text/edit_caption).
- Suporta mídia (photo, video, document).
- Suporta parse_mode (HTML / MarkdownV2 / Markdown).
- Suporta versões (histórico de alterações no banco).
- Se a key não existir, retorna um fallback seguro (não quebra o bot).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from bot.database import MessageTemplate, MessageTemplateVersion, session_scope
from bot.logger import logger
from bot.redis_client import cache_get, cache_set, cache_delete


# ============================================================
# 🔑 CONSTANTES
# ============================================================
CACHE_NAMESPACE = "messages"
CACHE_TTL = 60  # segundos — painel admin invalida ao salvar

VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

PARSE_HTML = "HTML"
PARSE_MDV2 = "MarkdownV2"
PARSE_MD = "Markdown"
PARSE_NONE = None

ALLOWED_PARSE_MODES = {PARSE_HTML, PARSE_MDV2, PARSE_MD, PARSE_NONE}

MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
MEDIA_DOCUMENT = "document"
MEDIA_ANIMATION = "animation"
ALLOWED_MEDIA_TYPES = {MEDIA_PHOTO, MEDIA_VIDEO, MEDIA_DOCUMENT, MEDIA_ANIMATION}


# ============================================================
# 🧱 ESTRUTURA INTERNA
# ============================================================
@dataclass
class RenderedMessage:
    """Resultado final do render — pronto para enviar/editar."""

    key: str
    content: str
    parse_mode: Optional[str]
    media_type: Optional[str]
    media_url: Optional[str]
    raw_content: str

    @property
    def has_media(self) -> bool:
        return bool(self.media_type and self.media_url)


# ============================================================
# 🧠 ENGINE
# ============================================================
class MessageEngine:
    """
    Motor central de mensagens.

    Uso típico:
        msg = await message_engine.render("boas_vindas", user=user, saldo=10.5)
        await message_engine.send(bot, chat_id, msg, reply_markup=kb)

        # Editar mensagem existente (sem acumular)
        await message_engine.edit(callback, "detalhe_produto", produto=p, saldo=0)
    """

    # --------------------------------------------------------
    # 📥 CARREGAMENTO / CACHE
    # --------------------------------------------------------
    async def load_template(self, key: str) -> Optional[MessageTemplate]:
        """Carrega o template do banco (com cache Redis)."""
        if not key:
            return None

        # ---------- Cache ----------
        cached = await cache_get(CACHE_NAMESPACE, key)
        if cached:
            # Reconstrói um pseudo-objeto com os campos usados
            class _Cached:
                pass

            obj = _Cached()
            for k, v in cached.items():
                setattr(obj, k, v)
            return obj  # type: ignore[return-value]

        # ---------- Banco ----------
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(MessageTemplate)
                    .where(MessageTemplate.key == key)
                    .where(MessageTemplate.active == True)  # noqa: E712
                )
                tpl: Optional[MessageTemplate] = result.scalar_one_or_none()

                if tpl is None:
                    return None

                # Cache
                payload = {
                    "id": int(tpl.id),
                    "key": tpl.key,
                    "screen": tpl.screen,
                    "content": tpl.content,
                    "media_type": tpl.media_type,
                    "media_url": tpl.media_url,
                    "parse_mode": tpl.parse_mode or PARSE_HTML,
                    "version": int(tpl.version or 1),
                }
                await cache_set(CACHE_NAMESPACE, key, payload, ttl=CACHE_TTL)

                return tpl
        except Exception as exc:
            logger.exception(f"Erro ao carregar template '{key}': {exc}")
            return None

    async def invalidate(self, key: Optional[str] = None) -> None:
        """Invalida cache de uma key (ou todas, se key=None)."""
        try:
            if key:
                await cache_delete(CACHE_NAMESPACE, key)
            else:
                await cache_delete(CACHE_NAMESPACE, "*")
        except Exception:
            pass

    # --------------------------------------------------------
    # 🧮 SUBSTITUIÇÃO DE VARIÁVEIS
    # --------------------------------------------------------
    def substitute(
        self,
        content: str,
        variables: Mapping[str, Any],
        *,
        escape: bool = False,
    ) -> str:
        """
        Substitui {{var}} por values em `variables`.

        - Aceita chaves aninhadas com ponto: {{user.first_name}}
        - Variável ausente vira string vazia (não quebra o render)
        - `escape=True` aplica html.escape nos valores (útil p/ HTML)
        """

        def _resolve(path: str) -> str:
            parts = path.split(".")
            current: Any = variables
            for p in parts:
                if isinstance(current, Mapping):
                    if p in current:
                        current = current[p]
                    else:
                        return ""
                else:
                    current = getattr(current, p, "")
                    if callable(current):
                        try:
                            current = current()
                        except Exception:
                            current = ""
                if current is None:
                    return ""
            if isinstance(current, float):
                # Formata valores monetários de forma consistente
                return f"{current:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            return str(current)

        def _replacer(match: re.Match) -> str:
            raw = match.group(1)
            value = _resolve(raw)
            return html.escape(value) if escape else value

        return VAR_PATTERN.sub(_replacer, content or "")

    # --------------------------------------------------------
    # 🎨 RENDER
    # --------------------------------------------------------
    async def render(
        self,
        key: str,
        *,
        fallback: Optional[str] = None,
        escape_html: Optional[bool] = None,
        **variables: Any,
    ) -> RenderedMessage:
        """
        Renderiza um template.

        Args:
            key: chave do template (ex.: "boas_vindas")
            fallback: texto usado se a key não existir no banco
            escape_html: força (True/False) ou auto-detecta pelo parse_mode
            **variables: variáveis a substituir

        Returns:
            RenderedMessage
        """
        tpl = await self.load_template(key)

        if tpl is None:
            content = fallback or f"[template '{key}' não encontrado]"
            return RenderedMessage(
                key=key,
                content=content,
                parse_mode=PARSE_HTML,
                media_type=None,
                media_url=None,
                raw_content=content,
            )

        raw_content = tpl.content or ""
        parse_mode = tpl.parse_mode or PARSE_HTML
        media_type = (tpl.media_type or "").lower() or None
        media_url = tpl.media_url

        # Escape automático só quando o parse_mode for HTML e o chamador não forçar
        do_escape = (
            escape_html
            if escape_html is not None
            else (parse_mode == PARSE_HTML)
        )

        rendered = self.substitute(raw_content, variables, escape=do_escape)

        return RenderedMessage(
            key=key,
            content=rendered,
            parse_mode=parse_mode,
            media_type=media_type if media_type in ALLOWED_MEDIA_TYPES else None,
            media_url=media_url,
            raw_content=raw_content,
        )

    # --------------------------------------------------------
    # 📨 ENVIO
    # --------------------------------------------------------
    async def send(
        self,
        bot: Bot,
        chat_id: int,
        message: Union[RenderedMessage, str],
        *,
        reply_markup: Optional[
            Union[InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove]
        ] = None,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        message_thread_id: Optional[int] = None,
    ) -> Optional[Message]:
        """Envia a mensagem (com mídia, se houver)."""
        if isinstance(message, str):
            rendered = RenderedMessage(
                key="inline",
                content=message,
                parse_mode=PARSE_HTML,
                media_type=None,
                media_url=None,
                raw_content=message,
            )
        else:
            rendered = message

        try:
            if rendered.has_media:
                return await self._send_with_media(
                    bot=bot,
                    chat_id=chat_id,
                    rendered=rendered,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=message_thread_id,
                )
            return await bot.send_message(
                chat_id=chat_id,
                text=rendered.content,
                parse_mode=rendered.parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
                disable_notification=disable_notification,
                message_thread_id=message_thread_id,
            )
        except TelegramBadRequest as exc:
            logger.warning(f"send falhou ({rendered.key}): {exc}")
            # tenta sem parse_mode
            try:
                return await bot.send_message(
                    chat_id=chat_id,
                    text=rendered.content,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                )
            except Exception as exc2:
                logger.exception(f"send retry falhou: {exc2}")
                return None
        except Exception as exc:
            logger.exception(f"send exception: {exc}")
            return None

    async def _send_with_media(
        self,
        *,
        bot: Bot,
        chat_id: int,
        rendered: RenderedMessage,
        reply_markup: Optional[Any],
        disable_notification: bool,
        message_thread_id: Optional[int],
    ) -> Optional[Message]:
        """Envia mensagem com mídia."""
        caption = rendered.content
        media_url = rendered.media_url
        parse_mode = rendered.parse_mode

        try:
            if rendered.media_type == MEDIA_PHOTO:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=media_url,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=message_thread_id,
                )
            if rendered.media_type == MEDIA_VIDEO:
                return await bot.send_video(
                    chat_id=chat_id,
                    video=media_url,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=message_thread_id,
                )
            if rendered.media_type == MEDIA_DOCUMENT:
                return await bot.send_document(
                    chat_id=chat_id,
                    document=media_url,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=message_thread_id,
                )
            if rendered.media_type == MEDIA_ANIMATION:
                return await bot.send_animation(
                    chat_id=chat_id,
                    animation=media_url,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=message_thread_id,
                )
        except Exception as exc:
            logger.warning(f"send_with_media falhou ({rendered.key}): {exc}")

        # Fallback → manda só texto
        return await bot.send_message(
            chat_id=chat_id,
            text=rendered.content or ".",
            parse_mode=rendered.parse_mode,
            reply_markup=reply_markup,
        )

    # --------------------------------------------------------
    # ✏️ EDIÇÃO (regra: NUNCA acumular)
    # --------------------------------------------------------
    async def edit(
        self,
        target: Union[CallbackQuery, Message],
        key: str,
        *,
        fallback: Optional[str] = None,
        reply_markup: Optional[Any] = None,
        escape_html: Optional[bool] = None,
        disable_web_page_preview: bool = True,
        **variables: Any,
    ) -> Optional[Message]:
        """
        Edita a mensagem existente (nunca envia nova, quando possível).

        - Se for CallbackQuery → edita event.message
        - Se for Message → edita essa mensagem
        - Se não for possível (ex.: mudou tipo de mídia), envia nova
        """
        rendered = await self.render(
            key,
            fallback=fallback,
            escape_html=escape_html,
            **variables,
        )

        msg: Optional[Message] = None
        if isinstance(target, CallbackQuery):
            msg = target.message
        elif isinstance(target, Message):
            msg = target

        if msg is None:
            # sem mensagem para editar → fallback para envio
            return None

        bot = msg.bot
        has_media = bool(
            rendered.has_media or (msg.photo or msg.video or msg.document or msg.animation)
        )

        try:
            if has_media:
                # ---------- EDIÇÃO DE MÍDIA ----------
                if rendered.has_media and rendered.media_type == MEDIA_PHOTO:
                    media = InputMediaPhoto(
                        media=rendered.media_url,
                        caption=rendered.content,
                        parse_mode=rendered.parse_mode,
                    )
                    return await bot.edit_message_media(
                        chat_id=msg.chat.id,
                        message_id=msg.message_id,
                        media=media,
                        reply_markup=reply_markup,
                    )

                # Se não conseguiu trocar a mídia, tenta editar a caption
                return await bot.edit_message_caption(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    caption=rendered.content,
                    parse_mode=rendered.parse_mode,
                    reply_markup=reply_markup,
                )

            # ---------- EDIÇÃO DE TEXTO ----------
            try:
                return await bot.edit_message_text(
                    chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    text=rendered.content,
                    parse_mode=rendered.parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                )
            except TelegramBadRequest as exc:
                # "message is not modified" → ignora silenciosamente
                if "not modified" in str(exc).lower():
                    # ainda assim, atualiza o reply_markup
                    try:
                        return await bot.edit_message_reply_markup(
                            chat_id=msg.chat.id,
                            message_id=msg.message_id,
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        return msg
                raise

        except TelegramBadRequest as exc:
            logger.warning(f"edit falhou ({key}): {exc}")
            # Se era uma mensagem com mídia e queremos texto, ou vice-versa:
            # apaga e envia nova como último recurso.
            try:
                await msg.delete()
            except Exception:
                pass
            return await self.send(
                bot=bot,
                chat_id=msg.chat.id,
                message=rendered,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as exc:
            logger.exception(f"edit exception ({key}): {exc}")
            return None

    # --------------------------------------------------------
    # 👁️ PREVIEW (usado pelo painel admin)
    # --------------------------------------------------------
    async def preview(
        self,
        key: str,
        *,
        variables: Optional[Mapping[str, Any]] = None,
        sample_variables: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """
        Renderiza a mensagem para preview no painel admin.
        Usa `sample_variables` para preencher variáveis conhecidas.
        """
        tpl = await self.load_template(key)
        if tpl is None:
            return f"[template '{key}' não encontrado]"
        values: Dict[str, Any] = {}
        if sample_variables:
            values.update(sample_variables)
        if variables:
            values.update(variables)
        return self.substitute(tpl.content or "", values, escape=False)

    # --------------------------------------------------------
    # 📜 VERSÕES (histórico)
    # --------------------------------------------------------
    async def list_versions(self, key: str, limit: int = 20) -> List[MessageTemplateVersion]:
        """Lista as versões anteriores de um template."""
        tpl = await self.load_template(key)
        if tpl is None:
            return []
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(MessageTemplateVersion)
                    .where(MessageTemplateVersion.template_id == tpl.id)
                    .order_by(MessageTemplateVersion.created_at.desc())
                    .limit(limit)
                )
                return list(result.scalars().all())
        except Exception as exc:
            logger.exception(f"Erro ao listar versões de '{key}': {exc}")
            return []

    async def restore_version(self, key: str, version_id: int) -> bool:
        """Restaura uma versão anterior de um template."""
        try:
            async with session_scope() as session:
                tpl_res = await session.execute(
                    select(MessageTemplate).where(MessageTemplate.key == key)
                )
                tpl = tpl_res.scalar_one_or_none()
                if tpl is None:
                    return False

                ver_res = await session.execute(
                    select(MessageTemplateVersion).where(
                        MessageTemplateVersion.id == version_id
                    )
                )
                ver = ver_res.scalar_one_or_none()
                if ver is None or ver.template_id != tpl.id:
                    return False

                # Salva versão atual antes de restaurar
                session.add(
                    MessageTemplateVersion(
                        template_id=tpl.id,
                        content=tpl.content,
                        created_by_id=None,
                    )
                )
                tpl.content = ver.content
                tpl.version = int(tpl.version or 1) + 1
                session.add(tpl)

            await self.invalidate(key)
            return True
        except Exception as exc:
            logger.exception(f"Erro ao restaurar versão: {exc}")
            return False

    # --------------------------------------------------------
    # 🧾 LISTA (painel admin)
    # --------------------------------------------------------
    async def list_all(self, screen: Optional[str] = None) -> List[MessageTemplate]:
        """Lista todos os templates (opcionalmente filtrando por screen)."""
        try:
            async with session_scope() as session:
                stmt = select(MessageTemplate).order_by(
                    MessageTemplate.screen.asc(),
                    MessageTemplate.key.asc(),
                )
                if screen:
                    stmt = stmt.where(MessageTemplate.screen == screen)
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:
            logger.exception(f"Erro ao listar templates: {exc}")
            return []


# ============================================================
# 📌 SINGLETON + ATALHOS DE MÓDULO
# ============================================================
message_engine = MessageEngine()


async def render(
    key: str,
    *,
    fallback: Optional[str] = None,
    escape_html: Optional[bool] = None,
    **variables: Any,
) -> RenderedMessage:
    """Atalho: `await render("boas_vindas", user=u)`."""
    return await message_engine.render(
        key, fallback=fallback, escape_html=escape_html, **variables
    )


async def send(
    bot: Bot,
    chat_id: int,
    key: str,
    *,
    fallback: Optional[str] = None,
    reply_markup: Optional[Any] = None,
    **variables: Any,
) -> Optional[Message]:
    """Atalho: envia uma mensagem por key."""
    msg = await message_engine.render(key, fallback=fallback, **variables)
    return await message_engine.send(
        bot, chat_id, msg, reply_markup=reply_markup
    )


async def edit(
    target: Union[CallbackQuery, Message],
    key: str,
    *,
    fallback: Optional[str] = None,
    reply_markup: Optional[Any] = None,
    **variables: Any,
) -> Optional[Message]:
    """Atalho: edita uma mensagem por key."""
    return await message_engine.edit(
        target,
        key,
        fallback=fallback,
        reply_markup=reply_markup,
        **variables,
    )


async def invalidate_message_cache(key: Optional[str] = None) -> None:
    """Chamado pelo painel admin após salvar/editar templates."""
    await message_engine.invalidate(key)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "MessageEngine",
    "RenderedMessage",
    "message_engine",
    "render",
    "send",
    "edit",
    "invalidate_message_cache",
    "PARSE_HTML",
    "PARSE_MDV2",
    "PARSE_MD",
    "MEDIA_PHOTO",
    "MEDIA_VIDEO",
    "MEDIA_DOCUMENT",
    "MEDIA_ANIMATION",
    "ALLOWED_PARSE_MODES",
    "ALLOWED_MEDIA_TYPES",
]
