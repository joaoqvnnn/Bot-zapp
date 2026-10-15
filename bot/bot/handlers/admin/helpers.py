"""
Larizinha Store — Admin Helpers
Utilitários compartilhados por TODOS os módulos do painel administrativo.

Oferece:
- Formatação (BRL, datas, tabelas, status)
- Renderização genérica de telas (via message_engine + button_engine)
- Edição sem acumular (edit_or_send)
- Inputs via Reply / ForceReply com estado em Redis (AdminFlow)
- Confirmações de ações críticas
- Paginação genérica com regras do DIM
- Auditoria automática (grava em Log + loguru)
- Atalhos para carregar entidades comuns (user, product, etc.)
- Helpers de "voltar", "atualizar", "cancelar"
- Utilitário de exportação CSV / JSON
- Cliente de notificação para admins

Regras invioláveis:
- NUNCA acumula: edit_or_send sempre tenta editar primeiro.
- Toda mensagem/botão é renderizado em runtime (motor de mensagens/botões).
- Inputs abrem ReplyKeyboard/ForceReply com one_time_keyboard, resize, selective.
- Apaga a mensagem do admin após capturar input sensível.
- Toda ação crítica é auditada em `logs`.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Type, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Admin,
    Admin as AdminModel,
    Log,
    Product,
    User,
    session_scope,
)
from bot.logger import audit, logger
from bot.redis_client import FlowState, Keys, get_redis
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from bot.keyboards.inline import build_pagination
from bot.keyboards.reply import build_cancel_reply, build_force_reply


# ============================================================
# 🔑 CONSTANTES
# ============================================================
ADMIN_FLOW_NAMESPACE = "admin_flow"
ADMIN_FLOW_DEFAULT_TTL = 15 * 60     # 15 minutos
ADMIN_PAGE_SIZE_DEFAULT = 8

# Símbolos de status
SYM_OK = "🟢"
SYM_OFF = "🔴"
SYM_WARN = "🟡"
SYM_ERR = "🔴"
SYM_PENDING = "🟡"
SYM_PROCESSING = "⏳"
SYM_DONE = "🟢"
SYM_CANCEL = "⚪️"
SYM_BANNED = "⛔"
SYM_BLOCKED = "🚫"
SYM_ACTIVE = "🟢"

# Regex para valores
NUM_RE = re.compile(r"^-?\d+([.,]\d{1,2})?$")
INT_RE = re.compile(r"^-?\d+$")
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
DATETIME_RE = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}$")


# ============================================================
# 🎨 FORMATAÇÃO
# ============================================================
def fmt_brl(value: Union[float, int, Decimal, None]) -> str:
    """Formata em BRL: R$ 1.234,56"""
    try:
        v = float(value or 0)
    except Exception:
        v = 0.0
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_num(value: Union[float, int, Decimal, None], decimals: int = 0) -> str:
    """Formata número com separador de milhar."""
    try:
        v = float(value or 0)
    except Exception:
        v = 0.0
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_dt(dt: Optional[datetime], with_time: bool = True) -> str:
    """Formata data/hora no padrão BR."""
    if dt is None:
        return "—"
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
    except Exception:
        local = dt
    if with_time:
        return local.strftime("%d/%m/%Y %H:%M")
    return local.strftime("%d/%m/%Y")


def fmt_ago(dt: Optional[datetime]) -> str:
    """Retorna 'há X min' a partir de um datetime."""
    if dt is None:
        return "—"
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"há {seconds}s"
        if seconds < 3600:
            return f"há {seconds // 60}min"
        if seconds < 86400:
            return f"há {seconds // 3600}h"
        return f"há {seconds // 86400}d"
    except Exception:
        return "—"


def status_emoji(value: Any) -> str:
    """Converte um status textual em emoji."""
    s = str(value or "").strip().lower()
    mapping = {
        "active": SYM_ACTIVE,
        "ativo": SYM_ACTIVE,
        "ok": SYM_OK,
        "online": SYM_OK,
        "blocked": SYM_BLOCKED,
        "bloqueado": SYM_BLOCKED,
        "banned": SYM_BANNED,
        "banido": SYM_BANNED,
        "pending": SYM_PENDING,
        "pendente": SYM_PENDING,
        "processing": SYM_PROCESSING,
        "processando": SYM_PROCESSING,
        "paid": SYM_DONE,
        "pago": SYM_DONE,
        "approved": SYM_DONE,
        "aprovado": SYM_DONE,
        "canceled": SYM_CANCEL,
        "cancelado": SYM_CANCEL,
        "refused": SYM_ERR,
        "recusado": SYM_ERR,
        "expired": SYM_WARN,
        "expirado": SYM_WARN,
        "refunded": SYM_WARN,
        "reembolsado": SYM_WARN,
        "true": SYM_OK,
        "false": SYM_OFF,
    }
    return mapping.get(s, "")


def truncate(text: Optional[str], size: int = 40) -> str:
    """Encurta um texto."""
    s = str(text or "").strip()
    if len(s) <= size:
        return s
    return s[: size - 3] + "..."


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """Mascara segredos deixando apenas os últimos N caracteres."""
    if not value:
        return "—"
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def humanize_bool(v: Any) -> str:
    """Converte bool para 🟢/🔴."""
    if isinstance(v, str):
        low = v.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return SYM_OK
        if low in {"0", "false", "no", "off"}:
            return SYM_OFF
    return SYM_OK if bool(v) else SYM_OFF


def table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    """
    Monta uma tabela de texto usando | para as linhas.
    Formato:
        | Header1 | Header2 |
        | row1c1  | row1c2  |
    """
    if not headers and not rows:
        return ""
    out: List[str] = []
    if headers:
        out.append("| " + " | ".join(str(h) for h in headers) + " |")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ============================================================
# 🎛️ ADMIN FLOW (estado de navegação/inputs)
# ============================================================
class AdminFlow:
    """
    Estado de fluxo do painel admin armazenado em Redis.
    Uso:
        await AdminFlow.set(admin_id, "products:edit", {"product_id": 3})
        state = await AdminFlow.get(admin_id)
        await AdminFlow.clear(admin_id)
    """

    @staticmethod
    def _key(admin_id: int) -> str:
        return f"{settings.REDIS_PREFIX}:{ADMIN_FLOW_NAMESPACE}:{admin_id}"

    @staticmethod
    async def set(
        admin_id: int,
        screen: str,
        data: Optional[Dict[str, Any]] = None,
        ttl: int = ADMIN_FLOW_DEFAULT_TTL,
    ) -> None:
        try:
            r = await get_redis()
            payload = {
                "screen": screen,
                "data": data or {},
                "updated_at": int(datetime.now(timezone.utc).timestamp()),
            }
            await r.set(
                AdminFlow._key(admin_id),
                json.dumps(payload, default=str),
                ex=ttl,
            )
        except Exception as exc:
            logger.debug(f"AdminFlow.set falhou: {exc}")

    @staticmethod
    async def get(admin_id: int) -> Optional[Dict[str, Any]]:
        try:
            r = await get_redis()
            raw = await r.get(AdminFlow._key(admin_id))
            if not raw:
                return None
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    async def update(admin_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        current = await AdminFlow.get(admin_id) or {"screen": None, "data": {}}
        current.setdefault("data", {})
        current["data"].update(patch)
        await AdminFlow.set(admin_id, current.get("screen") or "unknown", current["data"])
        return current

    @staticmethod
    async def clear(admin_id: int) -> None:
        try:
            r = await get_redis()
            await r.delete(AdminFlow._key(admin_id))
        except Exception:
            pass

    @staticmethod
    async def set_awaiting(
        admin_id: int,
        field: str,
        screen: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Marca o admin como aguardando input de um campo específico."""
        data = {"awaiting": field, "screen": screen, **(extra or {})}
        await AdminFlow.set(admin_id, screen, data)


# ============================================================
# 🧠 NAVEGAÇÃO — EDIT OR SEND
# ============================================================
async def edit_or_send(
    target: Union[CallbackQuery, Message],
    *,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
) -> Optional[Message]:
    """
    Tenta EDITAR a mensagem. Se não for possível, envia nova.
    Nunca acumula quando a origem é um callback.
    """
    message: Optional[Message] = None

    if isinstance(target, CallbackQuery):
        message = target.message
    elif isinstance(target, Message):
        message = target

    if message is None:
        return None

    bot = message.bot

    # ---------- Tenta editar ----------
    try:
        if message.photo or message.video or message.document or message.animation:
            # é mídia → edita caption
            return await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=message.message_id,
                caption=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as exc:
        low = str(exc).lower()
        if "not modified" in low:
            # só atualiza o teclado
            try:
                return await bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=reply_markup,
                )
            except Exception:
                return message
        # tenta enviar nova
        try:
            return await bot.send_message(
                chat_id=message.chat.id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception as exc2:
            logger.debug(f"edit_or_send falhou (fallback): {exc2}")
            return None
    except Exception as exc:
        logger.debug(f"edit_or_send falhou: {exc}")
        return None


# ============================================================
# 💬 RESPOSTAS RÁPIDAS
# ============================================================
async def answer(
    target: Union[CallbackQuery, None],
    text: str = "",
    *,
    alert: bool = False,
) -> None:
    """Responde um callback com toast (ou silencioso)."""
    if target is None:
        return
    try:
        await target.answer(text=text or None, show_alert=alert)
    except Exception:
        pass


async def toast_ok(cb: CallbackQuery, text: str = "✅ Feito!") -> None:
    await answer(cb, text, alert=False)


async def toast_err(cb: CallbackQuery, text: str = "❌ Erro.") -> None:
    await answer(cb, text, alert=True)


# ============================================================
# 🔘 BOTÕES PADRÃO
# ============================================================
def btn(
    text: str,
    callback_data: Optional[str] = None,
    *,
    url: Optional[str] = None,
    webapp: Optional[str] = None,
) -> InlineKeyboardButton:
    """Cria um InlineKeyboardButton de forma conveniente."""
    if url:
        return InlineKeyboardButton(text=text, url=url)
    if webapp:
        from aiogram.types import WebAppInfo

        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=webapp))
    return InlineKeyboardButton(text=text, callback_data=callback_data or "noop")


def kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Atalho para InlineKeyboardMarkup."""
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def build_back_row(
    callback: str,
    text: str = "🔙 Voltar",
) -> List[InlineKeyboardButton]:
    return [btn(text, callback)]


def build_confirm_keyboard(
    confirm_callback: str,
    cancel_callback: str,
    *,
    confirm_text: str = "✅ Confirmar",
    cancel_text: str = "❌ Cancelar",
) -> InlineKeyboardMarkup:
    return kb(
        [
            [
                btn(confirm_text, confirm_callback),
                btn(cancel_text, cancel_callback),
            ]
        ]
    )


def build_pagination_row(
    current_page: int,
    total_pages: int,
    base_callback: str,
    *,
    back_callback: Optional[str] = None,
    back_text: str = "🔙 Voltar",
    prev_text: str = "⬅️ Anterior",
    next_text: str = "Próxima ➡️",
) -> List[InlineKeyboardButton]:
    """
    Aplica a MESMA regra de paginação do DIM:
        Página 1:        [Próxima ➡️]
        Meio:            [🔙 Voltar] [⬅️ Anterior] [Próxima ➡️]
        Última:          [🔙 Voltar] [⬅️ Anterior]
    """
    return build_pagination(
        current_page=current_page,
        total_pages=total_pages,
        callback_base=base_callback,
        back_callback=back_callback,
        back_text=back_text,
        prev_text=prev_text,
        next_text=next_text,
    )


# ============================================================
# 📥 INPUTS (ForceReply / Reply)
# ============================================================
async def ask_input(
    target: Union[CallbackQuery, Message],
    prompt: str,
    *,
    field: str,
    screen: str,
    extra: Optional[Dict[str, Any]] = None,
    use_force_reply: bool = True,
    use_reply_keyboard: bool = False,
    reply_placeholder: Optional[str] = None,
    cancel_row: bool = True,
) -> Optional[Message]:
    """
    Pede input ao admin.

    - Se `use_force_reply=True` → envia ForceReply.
    - Se `use_reply_keyboard=True` → envia ReplyKeyboard com [❌ Cancelar].
    """
    admin_id = None
    chat_id = None

    if isinstance(target, CallbackQuery):
        admin_id = target.from_user.id
        chat_id = target.message.chat.id if target.message else None
        bot = target.bot
    elif isinstance(target, Message):
        admin_id = target.from_user.id
        chat_id = target.chat.id
        bot = target.bot
    else:
        return None

    # Salva estado do admin
    if admin_id is not None:
        await AdminFlow.set_awaiting(
            admin_id,
            field=field,
            screen=screen,
            extra=extra or {},
        )

    reply_markup: Optional[Union[ReplyKeyboardMarkup, ForceReply, ReplyKeyboardRemove]] = None
    if use_reply_keyboard and cancel_row:
        reply_markup = build_cancel_reply(
            placeholder=reply_placeholder or "Digite aqui..."
        )
    elif use_force_reply:
        reply_markup = build_force_reply(placeholder=reply_placeholder)

    # Envia prompt (não acumula, envia como nova para o input)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=prompt,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug(f"ask_input falhou: {exc}")
        return None


async def consume_input(
    message: Message,
    admin_id: int,
    *,
    expected_field: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Consome um input do admin:
    - Apaga a mensagem do admin (por segurança).
    - Retorna (valor, extra_data) se o admin estava aguardando input.
    - Retorna None se não estava aguardando.
    """
    state = await AdminFlow.get(admin_id)
    if not state or not state.get("data"):
        return None

    awaiting = state["data"].get("awaiting")
    if not awaiting:
        return None
    if expected_field and awaiting != expected_field:
        return None

    value = (message.text or "").strip()

    # Apaga a mensagem do admin
    try:
        await message.delete()
    except Exception:
        pass

    extra = dict(state["data"])
    extra.pop("awaiting", None)

    return value, extra


# ============================================================
# 🧾 RENDER DE TELA
# ============================================================
async def render_screen(
    target: Union[CallbackQuery, Message],
    *,
    message_key: str,
    screen: Optional[str] = None,
    fallback: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None,
    extra_rows_above: Optional[Sequence[Sequence[InlineKeyboardButton]]] = None,
    extra_rows_below: Optional[Sequence[Sequence[InlineKeyboardButton]]] = None,
    pagination: Optional[Tuple[int, int, str, Optional[str]]] = None,
    parse_mode: str = "HTML",
) -> Optional[Message]:
    """
    Renderiza uma tela do painel admin:
    - Texto: via message_engine (editável, com variáveis).
    - Teclado: via button_engine + linhas extras + paginação.

    Args:
        pagination: (current_page, total_pages, base_callback, back_callback)
    """
    # ---------- Texto ----------
    rendered = await message_engine.render(
        message_key,
        fallback=fallback,
        **(variables or {}),
    )

    # ---------- Teclado ----------
    rows: List[List[InlineKeyboardButton]] = []

    if extra_rows_above:
        for r in extra_rows_above:
            rows.append(list(r))

    # Botões do button_engine (se houver tela configurada)
    if screen:
        engine_kb = await button_engine.inline(screen)
        if engine_kb is not None:
            for r in engine_kb.inline_keyboard:
                rows.append(list(r))

    if pagination is not None:
        page, total_pages, base_cb, back_cb = pagination
        pag_row = build_pagination_row(
            current_page=page,
            total_pages=total_pages,
            base_callback=base_cb,
            back_callback=back_cb,
        )
        if pag_row:
            rows.append(pag_row)

    if extra_rows_below:
        for r in extra_rows_below:
            rows.append(list(r))

    reply_markup = kb(rows) if rows else None

    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


# ============================================================
# 📊 PAGINAÇÃO — TOTAL DE PÁGINAS
# ============================================================
def total_pages(total_items: int, per_page: int = ADMIN_PAGE_SIZE_DEFAULT) -> int:
    if total_items <= 0:
        return 1
    return max(1, math.ceil(total_items / per_page))


def page_offset(page: int, per_page: int = ADMIN_PAGE_SIZE_DEFAULT) -> int:
    page = max(1, page)
    return (page - 1) * per_page


# ============================================================
# 🧮 CARREGAR ENTIDADES
# ============================================================
async def load_user_by_id(user_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def load_user_by_tg(telegram_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def load_product(product_id: int) -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product).where(Product.id == product_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def load_admin(admin_id: int) -> Optional[Admin]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Admin).where(Admin.telegram_id == admin_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def count_rows(model: Type, *filters) -> int:
    """Conta linhas de um model (com filtros opcionais)."""
    try:
        async with session_scope() as session:
            stmt = select(func.count(model.id))
            for f in filters:
                stmt = stmt.where(f)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


# ============================================================
# 💾 AUDITORIA
# ============================================================
async def log_action(
    *,
    admin_id: Optional[int],
    action: str,
    category: str = "admin",
    user_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> None:
    """
    Registra uma ação administrativa em `logs` + loguru.
    """
    # Loguru (audit.log)
    try:
        audit(
            action=action,
            admin_id=admin_id,
            user_id=user_id,
            details=details or {},
            category=category,
        )
    except Exception:
        pass

    # Banco
    try:
        async with session_scope() as session:
            session.add(
                Log(
                    category=category,
                    action=action,
                    admin_id=admin_id,
                    user_id=user_id,
                    details=details or {},
                    ip=ip,
                )
            )
    except Exception as exc:
        logger.debug(f"log_action falhou: {exc}")


# ============================================================
# 📣 NOTIFICAR ADMINS
# ============================================================
async def notify_admins(
    bot: Bot,
    text: str,
    *,
    parse_mode: str = "HTML",
    exclude: Optional[Iterable[int]] = None,
) -> int:
    """Envia uma notificação para todos os admins do .env."""
    exclude_set = set(exclude or [])
    sent = 0
    for admin_id in settings.admin_ids_list:
        if admin_id in exclude_set:
            continue
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            continue
    return sent


# ============================================================
# 🧾 EXPORTAÇÃO (CSV / JSON)
# ============================================================
def to_csv_bytes(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    delimiter: str = ";",
) -> bytes:
    """Gera bytes de CSV (UTF-8 BOM) para envio como documento."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for r in rows:
        writer.writerow([("" if v is None else v) for v in r])
    data = buffer.getvalue().encode("utf-8-sig")
    return data


def to_json_bytes(data: Any) -> bytes:
    """Gera bytes de JSON indentado."""
    return json.dumps(data, indent=2, default=str, ensure_ascii=False).encode("utf-8")


async def send_csv(
    bot: Bot,
    chat_id: int,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    filename: str = "export.csv",
    caption: Optional[str] = None,
) -> bool:
    """Envia CSV como documento."""
    try:
        file = BufferedInputFile(to_csv_bytes(headers, rows), filename=filename)
        await bot.send_document(
            chat_id=chat_id,
            document=file,
            caption=caption or f"📥 <b>{filename}</b>",
            parse_mode="HTML",
        )
        return True
    except Exception as exc:
        logger.debug(f"send_csv falhou: {exc}")
        return False


# ============================================================
# 🧪 VALIDADORES
# ============================================================
def is_number(value: str) -> bool:
    if not value:
        return False
    v = value.strip().replace(",", ".")
    return NUM_RE.match(v) is not None


def is_int(value: str) -> bool:
    if not value:
        return False
    return INT_RE.match(value.strip()) is not None


def is_date(value: str) -> bool:
    if not value:
        return False
    if not DATE_RE.match(value.strip()):
        return False
    try:
        datetime.strptime(value.strip(), "%d/%m/%Y")
        return True
    except Exception:
        return False


def is_datetime(value: str) -> bool:
    if not value:
        return False
    if not DATETIME_RE.match(value.strip()):
        return False
    try:
        datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
        return True
    except Exception:
        return False


def parse_number(value: str) -> Optional[float]:
    if not is_number(value):
        return None
    try:
        return float(value.strip().replace(",", "."))
    except Exception:
        return None


def parse_int(value: str) -> Optional[int]:
    if not is_int(value):
        return None
    try:
        return int(value.strip())
    except Exception:
        return None


def parse_date(value: str) -> Optional[datetime]:
    if not is_date(value):
        return None
    try:
        dt = datetime.strptime(value.strip(), "%d/%m/%Y")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_datetime(value: str) -> Optional[datetime]:
    if not is_datetime(value):
        return None
    try:
        dt = datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ============================================================
# 🔙 AÇÕES RÁPIDAS
# ============================================================
async def go_back_to_menu(
    target: Union[CallbackQuery, Message],
    admin_id: int,
) -> Optional[Message]:
    """Volta para o menu principal do admin."""
    try:
        from bot.handlers.admin.menu import render_menu

        return await render_menu(target, admin_id)
    except Exception as exc:
        logger.debug(f"go_back_to_menu falhou: {exc}")
        return None


async def cancel_and_home(
    target: Union[CallbackQuery, Message],
    admin_id: int,
) -> Optional[Message]:
    """Limpa o estado e volta para o menu."""
    await AdminFlow.clear(admin_id)
    return await go_back_to_menu(target, admin_id)


# ============================================================
# 🧩 CONTEXTO DO ADMIN (decora handlers)
# ============================================================
@dataclass
class AdminCtx:
    """Contexto útil para handlers admin."""

    admin_id: int
    admin_row: Optional[AdminModel] = None
    permission: Any = None

    @property
    def is_main(self) -> bool:
        if self.admin_row is not None:
            return bool(getattr(self.admin_row, "is_main_admin", False))
        return self.admin_id == settings.MAIN_ADMIN_ID

    def has(self, required: str) -> bool:
        if self.permission is None:
            return self.is_main or self.admin_id in settings.admin_ids_list
        try:
            return bool(self.permission.has(required))
        except Exception:
            return False


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    # Formatação
    "fmt_brl",
    "fmt_num",
    "fmt_dt",
    "fmt_ago",
    "status_emoji",
    "truncate",
    "mask_secret",
    "humanize_bool",
    "table",
    # Estado do admin
    "AdminFlow",
    # Navegação
    "edit_or_send",
    "answer",
    "toast_ok",
    "toast_err",
    # Botões
    "btn",
    "kb",
    "build_back_row",
    "build_confirm_keyboard",
    "build_pagination_row",
    # Inputs
    "ask_input",
    "consume_input",
    # Render
    "render_screen",
    # Paginação
    "total_pages",
    "page_offset",
    # Entidades
    "load_user_by_id",
    "load_user_by_tg",
    "load_product",
    "load_admin",
    "count_rows",
    # Auditoria / notificação
    "log_action",
    "notify_admins",
    # Exportação
    "to_csv_bytes",
    "to_json_bytes",
    "send_csv",
    # Validadores
    "is_number",
    "is_int",
    "is_date",
    "is_datetime",
    "parse_number",
    "parse_int",
    "parse_date",
    "parse_datetime",
    # Atalhos
    "go_back_to_menu",
    "cancel_and_home",
    # Contexto
    "AdminCtx",
    # Constantes
    "SYM_OK",
    "SYM_OFF",
    "SYM_WARN",
    "SYM_ERR",
    "SYM_PENDING",
    "SYM_PROCESSING",
    "SYM_DONE",
    "SYM_CANCEL",
    "SYM_BANNED",
    "SYM_BLOCKED",
    "SYM_ACTIVE",
    "ADMIN_PAGE_SIZE_DEFAULT",
    "ADMIN_FLOW_NAMESPACE",
]
