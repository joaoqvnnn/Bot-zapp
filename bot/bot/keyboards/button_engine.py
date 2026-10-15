"""
Larizinha Store — Button Engine
Motor que renderiza TODOS os botões a partir da tabela `button_templates`
no banco, permitindo edição total pelo painel admin (texto, emoji, ordem,
posição, visibilidade, tipo, callback, url, webapp).

Regras invioláveis:
- NADA de botão hardcoded.
- Cacheado em Redis (curto TTL) para performance.
- Suporta tipos: callback | url | webapp | reply | copy
- Monta teclados inline e reply
- Se a tela não existir no banco, retorna teclado vazio (nunca falha)
- Nenhuma mensagem é montada aqui — só o teclado.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Union

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy import select

from bot.database import ButtonTemplate, session_scope
from bot.logger import logger
from bot.redis_client import cache_get, cache_set, cache_delete


# ============================================================
# 🔑 CONSTANTES
# ============================================================
CACHE_NAMESPACE = "buttons"
CACHE_TTL = 60  # segundos — painel invalida ao salvar

# Tipos suportados
TYPE_CALLBACK = "callback"
TYPE_URL = "url"
TYPE_WEBAPP = "webapp"
TYPE_REPLY = "reply"
TYPE_COPY = "copy"

ALL_TYPES = {TYPE_CALLBACK, TYPE_URL, TYPE_WEBAPP, TYPE_REPLY, TYPE_COPY}


# ============================================================
# 🧱 DATACLASS INTERNA
# ============================================================
@dataclass
class ButtonDef:
    """Representação em memória de um botão vindo do banco."""

    id: int
    screen: str
    key: str
    text: str
    emoji: Optional[str]
    type: str
    callback_data: Optional[str]
    url: Optional[str]
    webapp_url: Optional[str]
    row: int
    position: int
    order: int
    visible: bool

    @property
    def full_text(self) -> str:
        """Texto + emoji (se houver)."""
        if self.emoji:
            return f"{self.emoji} {self.text}".strip()
        return self.text.strip()

    @classmethod
    def from_model(cls, m: ButtonTemplate) -> "ButtonDef":
        return cls(
            id=int(m.id),
            screen=str(m.screen or ""),
            key=str(m.key or ""),
            text=str(m.text or ""),
            emoji=m.emoji,
            type=str(m.type or TYPE_CALLBACK).lower(),
            callback_data=m.callback_data,
            url=m.url,
            webapp_url=m.webapp_url,
            row=int(m.row or 1),
            position=int(m.position or 0),
            order=int(m.order or 0),
            visible=bool(m.visible),
        )


# ============================================================
# 🧠 ENGINE
# ============================================================
class ButtonEngine:
    """
    Motor central de botões.
    Uso:
        engine = ButtonEngine()
        kb = await engine.inline("boas_vindas")
        kb = await engine.reply("catalog")
    """

    # --------------------------------------------------------
    # 📥 CARREGAMENTO
    # --------------------------------------------------------
    async def load_screen(self, screen: str) -> List[ButtonDef]:
        """
        Carrega todos os botões VISÍVEIS de uma tela, ordenados.
        Cache via Redis com TTL curto. Painel admin invalida ao salvar.
        """
        if not screen:
            return []

        # ---------- Cache ----------
        cached = await cache_get(CACHE_NAMESPACE, screen)
        if cached:
            try:
                return [ButtonDef(**b) for b in cached]
            except Exception:
                # cache corrompido → limpa e recarrega
                await cache_delete(CACHE_NAMESPACE, screen)

        # ---------- Banco ----------
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(ButtonTemplate)
                    .where(ButtonTemplate.screen == screen)
                    .where(ButtonTemplate.visible == True)  # noqa: E712
                    .order_by(
                        ButtonTemplate.row.asc(),
                        ButtonTemplate.position.asc(),
                        ButtonTemplate.order.asc(),
                    )
                )
                rows: List[ButtonTemplate] = list(result.scalars().all())
        except Exception as exc:
            logger.exception(f"Erro ao carregar botões da tela '{screen}': {exc}")
            return []

        defs = [ButtonDef.from_model(r) for r in rows]
        defs = [b for b in defs if b.type in ALL_TYPES]

        # ---------- Cache ----------
        try:
            await cache_set(
                CACHE_NAMESPACE,
                screen,
                [b.__dict__ for b in defs],
                ttl=CACHE_TTL,
            )
        except Exception:
            pass

        return defs

    async def invalidate(self, screen: Optional[str] = None) -> None:
        """Invalida cache de uma tela (ou todas, se screen=None)."""
        try:
            if screen:
                await cache_delete(CACHE_NAMESPACE, screen)
            else:
                await cache_delete(CACHE_NAMESPACE, "*")
        except Exception:
            pass

    # --------------------------------------------------------
    # 🧩 AGRUPAMENTO POR LINHA
    # --------------------------------------------------------
    def _group_by_row(
        self,
        defs: Sequence[ButtonDef],
        *,
        kind: str,
    ) -> List[List[Union[InlineKeyboardButton, KeyboardButton]]]:
        """
        Agrupa os botões por linha (row) e devolve uma matriz pronta
        para InlineKeyboardMarkup ou ReplyKeyboardMarkup.

        kind: "inline" | "reply"
        """
        buckets: Dict[int, List[ButtonDef]] = {}
        for d in defs:
            if d.type == TYPE_REPLY and kind != "reply":
                continue
            if d.type != TYPE_REPLY and kind == "reply":
                continue
            buckets.setdefault(d.row, []).append(d)

        rows: List[List[Union[InlineKeyboardButton, KeyboardButton]]] = []
        for row_idx in sorted(buckets.keys()):
            items = sorted(buckets[row_idx], key=lambda x: (x.position, x.order))
            built_row: List[Union[InlineKeyboardButton, KeyboardButton]] = []
            for d in items:
                btn = self._build_button(d, kind=kind)
                if btn is not None:
                    built_row.append(btn)
            if built_row:
                rows.append(built_row)
        return rows

    # --------------------------------------------------------
    # 🔨 CONSTRÓI BOTÃO INDIVIDUAL
    # --------------------------------------------------------
    def _build_button(
        self,
        d: ButtonDef,
        *,
        kind: str,
    ) -> Optional[Union[InlineKeyboardButton, KeyboardButton]]:
        text = d.full_text or "—"

        if kind == "reply":
            # Reply só aceita texto
            return KeyboardButton(text=text)

        # ---------- INLINE ----------
        try:
            if d.type == TYPE_URL:
                if not d.url:
                    return None
                return InlineKeyboardButton(text=text, url=d.url)

            if d.type == TYPE_WEBAPP:
                if not d.webapp_url:
                    return None
                return InlineKeyboardButton(
                    text=text,
                    web_app=WebAppInfo(url=d.webapp_url),
                )

            if d.type == TYPE_COPY:
                # copia callback_data (payload) — silencioso
                payload = d.callback_data or d.text
                # Bot API 7.11+
                try:
                    from aiogram.types import CopyTextButton

                    return InlineKeyboardButton(
                        text=text,
                        copy_text=CopyTextButton(text=payload),
                    )
                except Exception:
                    # fallback: usa callback interno
                    import hashlib

                    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
                    return InlineKeyboardButton(
                        text=text,
                        callback_data=f"copy:{digest}",
                    )

            # TYPE_CALLBACK (padrão)
            if not d.callback_data:
                # sem callback → usa a key como callback (útil p/ debug)
                return InlineKeyboardButton(
                    text=text,
                    callback_data=f"noop:{d.key}",
                )
            return InlineKeyboardButton(text=text, callback_data=d.callback_data)

        except Exception as exc:
            logger.debug(f"Falha ao construir botão '{d.key}': {exc}")
            return None

    # --------------------------------------------------------
    # 📤 API PÚBLICA
    # --------------------------------------------------------
    async def inline(self, screen: str) -> Optional[InlineKeyboardMarkup]:
        """Retorna um InlineKeyboardMarkup pronto (ou None se vazio)."""
        defs = await self.load_screen(screen)
        if not defs:
            return None
        rows = self._group_by_row(defs, kind="inline")
        if not rows:
            return None
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def reply(
        self,
        screen: str,
        *,
        one_time: bool = True,
        resize: bool = True,
        selective: bool = True,
        placeholder: Optional[str] = None,
    ) -> Optional[ReplyKeyboardMarkup]:
        """Retorna um ReplyKeyboardMarkup pronto (ou None se vazio)."""
        defs = await self.load_screen(screen)
        if not defs:
            return None
        rows = self._group_by_row(defs, kind="reply")
        if not rows:
            return None
        return ReplyKeyboardMarkup(
            keyboard=rows,  # type: ignore[arg-type]
            one_time_keyboard=one_time,
            resize_keyboard=resize,
            selective=selective,
            input_field_placeholder=placeholder,
        )

    async def inline_rows(self, screen: str) -> List[List[InlineKeyboardButton]]:
        """Devolve as linhas inline (para quem quiser montar manualmente)."""
        defs = await self.load_screen(screen)
        rows = self._group_by_row(defs, kind="inline")
        return rows  # type: ignore[return-value]

    async def reply_rows(self, screen: str) -> List[List[KeyboardButton]]:
        """Devolve as linhas reply (para quem quiser montar manualmente)."""
        defs = await self.load_screen(screen)
        rows = self._group_by_row(defs, kind="reply")
        return rows  # type: ignore[return-value]

    # --------------------------------------------------------
    # 🎯 CONSULTA DIRETA
    # --------------------------------------------------------
    async def get(self, screen: str, key: str) -> Optional[ButtonDef]:
        """Retorna a definição de um botão específico."""
        defs = await self.load_screen(screen)
        for d in defs:
            if d.key == key:
                return d
        return None

    async def get_callback(self, screen: str, key: str) -> Optional[str]:
        """Retorna o callback_data de um botão (se existir)."""
        d = await self.get(screen, key)
        return d.callback_data if d else None

    async def get_url(self, screen: str, key: str) -> Optional[str]:
        """Retorna a URL de um botão (se existir)."""
        d = await self.get(screen, key)
        return d.url if d else None


# ============================================================
# 📌 SINGLETON + ATALHOS DE MÓDULO
# ============================================================
button_engine = ButtonEngine()


async def render_inline(screen: str) -> Optional[InlineKeyboardMarkup]:
    """Atalho: `await render_inline("boas_vindas")`."""
    return await button_engine.inline(screen)


async def render_reply(
    screen: str,
    *,
    one_time: bool = True,
    resize: bool = True,
    selective: bool = True,
    placeholder: Optional[str] = None,
) -> Optional[ReplyKeyboardMarkup]:
    """Atalho: `await render_reply("catalog")`."""
    return await button_engine.reply(
        screen,
        one_time=one_time,
        resize=resize,
        selective=selective,
        placeholder=placeholder,
    )


async def invalidate_button_cache(screen: Optional[str] = None) -> None:
    """Chamado pelo painel admin após salvar/editar botões."""
    await button_engine.invalidate(screen)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "ButtonEngine",
    "ButtonDef",
    "button_engine",
    "render_inline",
    "render_reply",
    "invalidate_button_cache",
    "TYPE_CALLBACK",
    "TYPE_URL",
    "TYPE_WEBAPP",
    "TYPE_REPLY",
    "TYPE_COPY",
    "ALL_TYPES",
    "CACHE_NAMESPACE",
    "CACHE_TTL",
]
