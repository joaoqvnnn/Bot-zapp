"""
Larizinha Store — Módulo Admin: Gift Cards
Implementa CRUD completo de Gift Cards conforme DIM.

Telas:
- Lista de gift cards (filtros: ativos/usados/expirados/todos, paginação DIM)
- Criar (código automático ou personalizado, valor, usos máx, validade, ativo)
- Editar campos individuais
- Detalhes (usos, expira, quem resgatou)
- Revogar (desativa o cartão)
- Excluir com confirmação
- Exportar CSV

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    GiftCard,
    GiftCardRedemption,
    User,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_OFF,
    SYM_OK,
    SYM_WARN,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_brl,
    fmt_dt,
    fmt_num,
    humanize_bool,
    is_date,
    is_int,
    is_number,
    kb,
    log_action,
    page_offset,
    parse_date,
    parse_int,
    parse_number,
    send_csv,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_gifts")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_gifts_list"
MSG_KEY_EMPTY = "admin_gifts_empty"
MSG_KEY_DETAIL = "admin_gifts_detail"

SCREEN_LIST = "admin_gifts_list"
SCREEN_DETAIL = "admin_gifts_detail"

# Callbacks base
CB_ROOT = "admin:gifts"
CB_PAGE = "admin:gifts:page"              # :filter:N
CB_FILTER = "admin:gifts:filter"          # :key
CB_NEW = "admin:gifts:new"
CB_VIEW = "admin:gifts:view"              # :ID
CB_SET = "admin:gifts:set"                # :field:ID
CB_TOGGLE = "admin:gifts:toggle"          # :ID
CB_REVOKE = "admin:gifts:revoke"          # :ID
CB_REVOKE_YES = "admin:gifts:revoke:yes"  # :ID
CB_DELETE = "admin:gifts:delete"          # :ID
CB_DELETE_YES = "admin:gifts:delete:yes"  # :ID
CB_REDEMPTIONS = "admin:gifts:redemptions"  # :ID
CB_EXPORT = "admin:gifts:export"

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "active": "🟢 Ativos",
    "used": "✅ Totalmente usados",
    "expired": "⌛️ Expirados",
    "inactive": "🔴 Desativados",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "code": {"label": "Código", "validate": "text", "max": 40},
    "amount": {"label": "Valor", "validate": "money"},
    "uses_max": {"label": "Usos máximos", "validate": "int"},
    "expires_at": {"label": "Validade (DD/MM/AAAA)", "validate": "date"},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _gen_code(length: int = 12) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "active":
        return stmt.where(GiftCard.active == True)  # noqa: E712
    if filter_key == "inactive":
        return stmt.where(GiftCard.active == False)  # noqa: E712
    if filter_key == "used":
        return stmt.where(
            GiftCard.uses_max > 0,
            GiftCard.uses_count >= GiftCard.uses_max,
        )
    if filter_key == "expired":
        return stmt.where(GiftCard.expires_at <= now)
    return stmt


async def _count_gifts(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(GiftCard.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_gifts(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[GiftCard]:
    try:
        async with session_scope() as session:
            stmt = select(GiftCard).order_by(GiftCard.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_gift(gid: int) -> Optional[GiftCard]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(GiftCard).where(GiftCard.id == gid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_redemptions(gid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(GiftCardRedemption.id)).where(
                    GiftCardRedemption.gift_card_id == gid
                )
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
    filter_key: str = "all",
) -> Optional[Message]:
    total = await _count_gifts(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    gifts = await _list_gifts(page, filter_key)

    if not gifts:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🎁 <b>GIFT CARDS</b>\n\n"
                "<i>Nenhum gift card encontrado com este filtro.</i>"
            ),
        )
    else:
        now = datetime.now(timezone.utc)
        rows: List[Sequence[str]] = []
        for g in gifts:
            status = SYM_OK if g.active else SYM_OFF
            if g.expires_at and g.expires_at <= now:
                status = SYM_WARN
            uses = f"{int(g.uses_count or 0)}/{int(g.uses_max or 0)}"
            rows.append(
                [
                    str(g.id),
                    truncate(g.code, 14),
                    fmt_brl(g.amount),
                    uses,
                    status,
                ]
            )

        lista = table(rows, ["ID", "Código", "Valor", "Usos", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🎁 <b>GIFT CARDS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
        )

    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="➕ Criar Gift Card", callback_data=CB_NEW),
                InlineKeyboardButton(text="📥 Exportar CSV", callback_data=CB_EXPORT),
            ],
            [
                InlineKeyboardButton(text="🟢 Ativos", callback_data=f"{CB_FILTER}:active"),
                InlineKeyboardButton(text="✅ Usados", callback_data=f"{CB_FILTER}:used"),
            ],
            [
                InlineKeyboardButton(text="⌛️ Expirados", callback_data=f"{CB_FILTER}:expired"),
                InlineKeyboardButton(text="🔴 Desativados", callback_data=f"{CB_FILTER}:inactive"),
            ],
            [
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for g in gifts:
        status = SYM_OK if g.active else SYM_OFF
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {g.code} · {fmt_brl(g.amount)}",
                    callback_data=f"{CB_VIEW}:{g.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=f"{CB_PAGE}:{filter_key}",
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(build_back_row("admin:menu"))

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data == CB_ROOT)
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_list(callback, page=1, filter_key="all")
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # admin:gifts:page:FILTER:N
        parts = callback.data.split(":")
        filter_key = "all"
        page = 1
        if len(parts) >= 4:
            filter_key = parts[3]
        if len(parts) >= 5:
            try:
                page = int(parts[4])
            except Exception:
                page = 1
        if filter_key not in FILTERS:
            filter_key = "all"
        await render_list(callback, page=page, filter_key=filter_key)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_page falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_FILTER}:"))
async def on_filter(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        filter_key = callback.data.split(":")[-1]
        if filter_key not in FILTERS:
            filter_key = "all"
        await render_list(callback, page=1, filter_key=filter_key)
        await toast_ok(callback, f"{FILTERS[filter_key]} selecionado.")
    except Exception as exc:
        logger.exception(f"gifts on_filter falhou: {exc}")


# ============================================================
# CRIAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVO GIFT CARD</b>\n\n"
            "Envie o <b>valor</b> do gift card (ex.: <code>20</code> ou <code>50.00</code>).\n"
            "Depois vou gerar um código automático.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="gift_new_amount",
            screen="gifts:new",
            use_force_reply=True,
            reply_placeholder="Ex.: 20",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_new falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_gifts_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Criar gift card (valor)
    if awaiting == "gift_new_amount":
        value, _extra = await consume_input(message, admin_id, expected_field="gift_new_amount")
        if value is None:
            return
        await _create_gift(message, admin_id, value)
        return

    # Editar campos
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        gid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, gid, field, value)
        return


async def _create_gift(message: Message, admin_id: int, raw_value: str) -> None:
    valor = parse_number(raw_value)
    if valor is None or valor <= 0:
        await message.answer("❌ Valor inválido.")
        return

    code = _gen_code()

    # Garante unicidade
    for _ in range(5):
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(func.count(GiftCard.id)).where(GiftCard.code == code)
                )
                if int(r.scalar() or 0) == 0:
                    break
        except Exception:
            pass
        code = _gen_code()

    try:
        async with session_scope() as session:
            g = GiftCard(
                code=code,
                amount=Decimal(str(round(valor, 2))),
                uses_max=1,
                uses_count=0,
                active=True,
                created_by_id=admin_id,
            )
            session.add(g)
            await session.flush()
            gid = int(g.id)
    except Exception as exc:
        logger.exception(f"_create_gift falhou: {exc}")
        await message.answer("❌ Erro ao criar gift card.")
        return

    await log_action(
        admin_id=admin_id,
        action="gift.created",
        category="admin",
        details={"gift_id": gid, "code": code, "amount": valor},
    )

    await message.answer(
        f"✅ <b>Gift Card criado!</b>\n\n"
        f"🎁 Código: <code>{code}</code>\n"
        f"💵 Valor: <b>{fmt_brl(valor)}</b>\n"
        f"🎫 ID: <code>#{gid}</code>\n\n"
        f"Use os botões abaixo para editar.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="✏️ Editar",
                        callback_data=f"{CB_VIEW}:{gid}",
                    )
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(gid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    g = await _load_gift(gid)
    if g is None:
        return None, None

    redemptions = await _count_redemptions(gid)

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"🎁 <b>GIFT CARD #{g.id}</b>\n\n"
            f"├ Código: <code>{g.code}</code>\n"
            f"├ Valor: <b>{fmt_brl(g.amount)}</b>\n"
            f"├ Usos: {int(g.uses_count or 0)}/{int(g.uses_max or 0)}\n"
            f"├ Validade: {fmt_dt(g.expires_at, with_time=False) if g.expires_at else '—'}\n"
            f"├ Ativo: {humanize_bool(g.active)}\n"
            f"├ Criado em: {fmt_dt(g.created_at)}\n"
            f"└ Resgates: {redemptions}"
        ),
        id=g.id,
        code=g.code,
        amount=fmt_brl(g.amount),
        uses=f"{int(g.uses_count or 0)}/{int(g.uses_max or 0)}",
        expires=fmt_dt(g.expires_at, with_time=False) if g.expires_at else "—",
        active=humanize_bool(g.active),
        created=fmt_dt(g.created_at),
        redemptions=redemptions,
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(g))

    return rendered.content, keyboard


def _default_detail_keyboard(g: GiftCard) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Código", f"{CB_SET}:code:{g.id}"), b("✏️ Valor", f"{CB_SET}:amount:{g.id}")],
        [b("✏️ Usos máx", f"{CB_SET}:uses_max:{g.id}"), b("✏️ Validade", f"{CB_SET}:expires_at:{g.id}")],
        [b("🔄 Ativo/Inativo", f"{CB_TOGGLE}:{g.id}")],
        [b("📜 Ver Resgates", f"{CB_REDEMPTIONS}:{g.id}")],
        [b("🚫 Revogar", f"{CB_REVOKE}:{g.id}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{g.id}")],
        [b("🔙 Voltar", CB_ROOT)],
    ]


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(gid)
        if text is None:
            await toast_err(callback, "❌ Gift card não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_view falhou: {exc}")


# ============================================================
# SET FIELD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:gifts:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        gid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "money":
            hint = "Ex.: <code>20.00</code>"
        elif meta["validate"] == "int":
            hint = "Ex.: <code>1</code>"
        elif meta["validate"] == "date":
            hint = "Ex.: <code>31/12/2026</code>"
        else:
            hint = f"Até {meta.get('max', 40)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{gid}",
            screen=f"gifts:edit:{gid}",
            extra={"gift_id": gid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_set_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    gid: int,
    field: str,
    value: str,
) -> None:
    meta = TEXT_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if meta["validate"] == "money":
        num = parse_number(val)
        if num is None or num <= 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = Decimal(str(round(num, 2)))
    elif meta["validate"] == "int":
        num = parse_int(val)
        if num is None or num < 1:
            await message.answer("❌ Número inválido.")
            return
        parsed = int(num)
    elif meta["validate"] == "date":
        dt = parse_date(val)
        if dt is None:
            await message.answer("❌ Data inválida.")
            return
        parsed = dt
    else:
        if field == "code":
            parsed = val.upper()
            # garante único
            try:
                async with session_scope() as session:
                    r = await session.execute(
                        select(func.count(GiftCard.id)).where(
                            GiftCard.code == parsed,
                            GiftCard.id != gid,
                        )
                    )
                    if int(r.scalar() or 0) > 0:
                        await message.answer("❌ Código já em uso.")
                        return
            except Exception:
                pass
        if len(val) > meta.get("max", 200):
            await message.answer("❌ Texto muito longo.")
            return

    try:
        async with session_scope() as session:
            r = await session.execute(select(GiftCard).where(GiftCard.id == gid))
            g: Optional[GiftCard] = r.scalar_one_or_none()
            if g is None:
                await message.answer("❌ Gift card não encontrado.")
                return
            setattr(g, field, parsed)
            session.add(g)
    except Exception as exc:
        logger.exception(f"_apply_field_value (gift) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"gift.set.{field}",
        category="admin",
        details={"gift_id": gid, "value": str(parsed)},
    )

    text, keyboard = await _render_detail(gid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE ATIVO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(GiftCard).where(GiftCard.id == gid))
                g: Optional[GiftCard] = r.scalar_one_or_none()
                if g is None:
                    await toast_err(callback, "❌ Gift card não encontrado.")
                    return
                g.active = not bool(g.active)
                session.add(g)
                new_val = bool(g.active)
        except Exception as exc:
            logger.exception(f"toggle gift falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="gift.toggle",
            category="admin",
            details={"gift_id": gid, "active": new_val},
        )

        text, keyboard = await _render_detail(gid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔄 Alterado.")
    except Exception as exc:
        logger.exception(f"gifts on_toggle falhou: {exc}")


# ============================================================
# REVOGAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REVOKE}:"))
async def on_revoke(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            gid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        g = await _load_gift(gid)
        if g is None:
            await toast_err(callback, "❌ Gift card não encontrado.")
            return

        text = (
            f"🚫 <b>REVOGAR GIFT CARD</b>\n\n"
            f"Código: <code>{g.code}</code>\n"
            f"Valor: <b>{fmt_brl(g.amount)}</b>\n\n"
            "Isso desativará o cartão para futuros resgates.\n"
            "Resgates anteriores não serão revertidos.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REVOKE_YES}:{gid}",
            cancel_callback=f"{CB_VIEW}:{gid}",
            confirm_text="🚫 Revogar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_revoke falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REVOKE_YES}:"))
async def on_revoke_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(GiftCard).where(GiftCard.id == gid))
                g: Optional[GiftCard] = r.scalar_one_or_none()
                if g is None:
                    await toast_err(callback, "❌ Gift card não encontrado.")
                    return
                g.active = False
                session.add(g)
        except Exception as exc:
            logger.exception(f"revogar falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="gift.revoked",
            category="admin",
            details={"gift_id": gid},
        )

        text, keyboard = await _render_detail(gid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🚫 Revogado.")
    except Exception as exc:
        logger.exception(f"gifts on_revoke_confirm falhou: {exc}")


# ============================================================
# EXCLUIR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            gid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        g = await _load_gift(gid)
        if g is None:
            await toast_err(callback, "❌ Gift card não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR GIFT CARD</b>\n\n"
            f"Código: <code>{g.code}</code>\n"
            f"Valor: <b>{fmt_brl(g.amount)}</b>\n\n"
            "Isso removerá o cartão do banco (incluindo resgates).\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{gid}",
            cancel_callback=f"{CB_VIEW}:{gid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Remove resgates
                await session.execute(
                    GiftCardRedemption.__table__.delete().where(
                        GiftCardRedemption.gift_card_id == gid
                    )
                )
                # Remove cartão
                await session.execute(
                    GiftCard.__table__.delete().where(GiftCard.id == gid)
                )
        except Exception as exc:
            logger.exception(f"delete gift falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="gift.deleted",
            category="admin",
            details={"gift_id": gid},
        )

        await render_list(callback, page=1, filter_key="all")
        await toast_ok(callback, "🗑️ Excluído.")
    except Exception as exc:
        logger.exception(f"gifts on_delete_confirm falhou: {exc}")


# ============================================================
# RESGATES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REDEMPTIONS}:"))
async def on_redemptions(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(GiftCardRedemption)
                    .where(GiftCardRedemption.gift_card_id == gid)
                    .order_by(GiftCardRedemption.created_at.desc())
                    .limit(30)
                )
                redemptions = list(res.scalars().all())
        except Exception:
            redemptions = []

        if not redemptions:
            text = "📜 <i>Nenhum resgate registrado.</i>"
        else:
            lines = [f"📜 <b>RESGATES · GIFT #{gid}</b> ({len(redemptions)})", ""]
            for r in redemptions:
                lines.append(
                    f"👤 User <code>{r.user_id}</code> · "
                    f"{fmt_brl(r.amount)} · {fmt_dt(r.created_at)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"gifts on_redemptions falhou: {exc}")


# ============================================================
# EXPORTAR CSV
# ============================================================
@router.callback_query(F.data == CB_EXPORT)
async def on_export(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(GiftCard).order_by(GiftCard.id.asc())
                )
                gifts = list(res.scalars().all())
        except Exception:
            gifts = []

        headers = ["ID", "Código", "Valor", "Usos", "Usos Max", "Ativo", "Expira em", "Criado em"]
        rows: List[List[Any]] = []
        for g in gifts:
            rows.append(
                [
                    g.id,
                    g.code,
                    float(g.amount or 0),
                    int(g.uses_count or 0),
                    int(g.uses_max or 0),
                    "sim" if g.active else "não",
                    fmt_dt(g.expires_at) if g.expires_at else "",
                    fmt_dt(g.created_at),
                ]
            )

        filename = f"gift_cards_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📥 <b>Gift Cards</b> ({len(rows)})",
        )
        if ok:
            await toast_ok(callback, "📥 CSV enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action="gift.export_csv",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"gifts on_export falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_filter",
    "on_new",
    "on_gifts_text_input",
    "on_view",
    "on_set_field",
    "on_toggle",
    "on_revoke",
    "on_revoke_confirm",
    "on_delete",
    "on_delete_confirm",
    "on_redemptions",
    "on_export",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_NEW",
    "CB_VIEW",
    "CB_SET",
    "CB_TOGGLE",
    "CB_REVOKE",
    "CB_REVOKE_YES",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_REDEMPTIONS",
    "CB_EXPORT",
    "FILTERS",
    "TEXT_FIELDS",
]
