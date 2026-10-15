"""
Larizinha Store — Módulo Admin: Cupons
Implementa CRUD completo de cupons conforme DIM.

Telas:
- Lista de cupons (filtros: ativos/expirados/esgotados/todos, paginação DIM)
- Criar cupom (fixo / percent / bônus)
- Editar campos individuais
- Detalhes (valor, tipo, usos, limite, mínimo, validade)
- Ativar/Desativar
- Excluir com confirmação
- Histórico de usos
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
    Coupon,
    CouponType,
    CouponUsage,
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
router = Router(name="admin_coupons")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_coupons_list"
MSG_KEY_EMPTY = "admin_coupons_empty"
MSG_KEY_DETAIL = "admin_coupons_detail"

SCREEN_LIST = "admin_coupons_list"
SCREEN_DETAIL = "admin_coupons_detail"

# Callbacks base
CB_ROOT = "admin:coupons"
CB_PAGE = "admin:coupons:page"              # :filter:N
CB_FILTER = "admin:coupons:filter"          # :key
CB_NEW = "admin:coupons:new"
CB_NEW_TYPE = "admin:coupons:newtype"       # :type
CB_VIEW = "admin:coupons:view"              # :ID
CB_SET = "admin:coupons:set"                # :field:ID
CB_TYPE_SET = "admin:coupons:typeset"       # :ID:type
CB_TOGGLE = "admin:coupons:toggle"          # :ID
CB_DELETE = "admin:coupons:delete"          # :ID
CB_DELETE_YES = "admin:coupons:delete:yes"  # :ID
CB_USAGES = "admin:coupons:usages"          # :ID
CB_EXPORT = "admin:coupons:export"

# Filtros
FILTERS: Dict[str, str] = {
    "all": "📋 Todos",
    "active": "🟢 Ativos",
    "inactive": "🔴 Desativados",
    "expired": "⌛️ Expirados",
    "exhausted": "🚫 Esgotados",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "code": {"label": "Código", "validate": "text", "max": 40},
    "value": {"label": "Valor", "validate": "money"},
    "min_purchase": {"label": "Valor mínimo de compra", "validate": "money"},
    "uses_max": {"label": "Usos máximos", "validate": "int"},
    "per_user_limit": {"label": "Limite por usuário", "validate": "int"},
    "expires_at": {"label": "Validade (DD/MM/AAAA)", "validate": "date"},
}

# Tipo (para exibição)
TYPE_LABELS: Dict[str, str] = {
    "FIXED": "💰 Fixo",
    "PERCENT": "📊 Percentual",
    "BONUS": "🎁 Bônus saldo",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _gen_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _apply_filter(stmt, filter_key: str):
    now = datetime.now(timezone.utc)
    if filter_key == "active":
        return stmt.where(Coupon.active == True)  # noqa: E712
    if filter_key == "inactive":
        return stmt.where(Coupon.active == False)  # noqa: E712
    if filter_key == "expired":
        return stmt.where(Coupon.expires_at <= now)
    if filter_key == "exhausted":
        return stmt.where(
            Coupon.uses_max > 0,
            Coupon.uses_count >= Coupon.uses_max,
        )
    return stmt


async def _count_coupons(filter_key: str = "all") -> int:
    try:
        async with session_scope() as session:
            stmt = select(func.count(Coupon.id))
            stmt = _apply_filter(stmt, filter_key)
            res = await session.execute(stmt)
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_coupons(
    page: int,
    filter_key: str = "all",
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Coupon]:
    try:
        async with session_scope() as session:
            stmt = select(Coupon).order_by(Coupon.created_at.desc())
            stmt = _apply_filter(stmt, filter_key)
            stmt = stmt.offset(page_offset(page, per_page)).limit(per_page)
            res = await session.execute(stmt)
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_coupon(cid: int) -> Optional[Coupon]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Coupon).where(Coupon.id == cid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_usages(cid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(CouponUsage.id)).where(CouponUsage.coupon_id == cid)
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
    total = await _count_coupons(filter_key)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    coupons = await _list_coupons(page, filter_key)

    if not coupons:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🎟️ <b>CUPONS</b>\n\n"
                "<i>Nenhum cupom encontrado com este filtro.</i>"
            ),
        )
    else:
        now = datetime.now(timezone.utc)
        rows: List[Sequence[str]] = []
        for c in coupons:
            status = SYM_OK if c.active else SYM_OFF
            if c.expires_at and c.expires_at <= now:
                status = SYM_WARN
            tipo = TYPE_LABELS.get(str(getattr(c.type, "value", c.type)), str(c.type))
            rows.append(
                [
                    str(c.id),
                    truncate(c.code, 12),
                    tipo,
                    fmt_brl(c.value),
                    f"{int(c.uses_count or 0)}/{int(c.uses_max or 0)}",
                    status,
                ]
            )

        lista = table(rows, ["ID", "Código", "Tipo", "Valor", "Usos", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🎟️ <b>CUPONS</b> ({total})\n\n"
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
                InlineKeyboardButton(text="➕ Criar Cupom", callback_data=CB_NEW),
                InlineKeyboardButton(text="📥 Exportar CSV", callback_data=CB_EXPORT),
            ],
            [
                InlineKeyboardButton(text="🟢 Ativos", callback_data=f"{CB_FILTER}:active"),
                InlineKeyboardButton(text="🔴 Inativos", callback_data=f"{CB_FILTER}:inactive"),
            ],
            [
                InlineKeyboardButton(text="⌛️ Expirados", callback_data=f"{CB_FILTER}:expired"),
                InlineKeyboardButton(text="🚫 Esgotados", callback_data=f"{CB_FILTER}:exhausted"),
            ],
            [
                InlineKeyboardButton(text="📋 Todos", callback_data=f"{CB_FILTER}:all"),
            ],
        ]

    for c in coupons:
        status = SYM_OK if c.active else SYM_OFF
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {c.code} · {fmt_brl(c.value)}",
                    callback_data=f"{CB_VIEW}:{c.id}",
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
        logger.exception(f"coupons on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
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
        logger.exception(f"coupons on_page falhou: {exc}")


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
        logger.exception(f"coupons on_filter falhou: {exc}")


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

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="💰 Fixo (R$)",
                    callback_data=f"{CB_NEW_TYPE}:FIXED",
                ),
                InlineKeyboardButton(
                    text="📊 Percentual (%)",
                    callback_data=f"{CB_NEW_TYPE}:PERCENT",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎁 Bônus saldo",
                    callback_data=f"{CB_NEW_TYPE}:BONUS",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                )
            ],
        ]

        text = (
            "➕ <b>NOVO CUPOM</b>\n\n"
            "Escolha o tipo de desconto:"
        )
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_new falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_NEW_TYPE}:"))
async def on_new_type(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            ctype = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Tipo inválido.")
            return
        if ctype not in ("FIXED", "PERCENT", "BONUS"):
            await toast_err(callback, "❌ Tipo inválido.")
            return

        label = TYPE_LABELS.get(ctype, ctype)

        prompt = (
            f"➕ <b>NOVO CUPOM · {label}</b>\n\n"
            "Envie o <b>valor</b> do desconto.\n"
            "Ex.: <code>10</code> ou <code>15.5</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await AdminFlow.set_awaiting(
            admin_id,
            field=f"coupon_new_value:{ctype}",
            screen=f"coupons:new:{ctype}",
            extra={"type": ctype},
        )
        await ask_input(
            callback,
            prompt,
            field=f"coupon_new_value:{ctype}",
            screen=f"coupons:new:{ctype}",
            extra={"type": ctype},
            use_force_reply=True,
            reply_placeholder="Ex.: 10",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_new_type falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_coupons_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Criar cupom (valor)
    if awaiting.startswith("coupon_new_value:"):
        ctype = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _create_coupon(message, admin_id, ctype, value)
        return

    # Editar campos
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        cid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, cid, field, value)
        return


async def _create_coupon(message: Message, admin_id: int, ctype: str, raw_value: str) -> None:
    valor = parse_number(raw_value)
    if valor is None or valor <= 0:
        await message.answer("❌ Valor inválido.")
        return

    if ctype == "PERCENT" and valor > 100:
        await message.answer("❌ Percentual não pode passar de 100.")
        return

    code = _gen_code()
    # garante único
    for _ in range(5):
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(func.count(Coupon.id)).where(Coupon.code == code)
                )
                if int(r.scalar() or 0) == 0:
                    break
        except Exception:
            pass
        code = _gen_code()

    try:
        async with session_scope() as session:
            c = Coupon(
                code=code,
                type=CouponType(ctype),
                value=Decimal(str(round(valor, 2))),
                min_purchase=Decimal("0.00"),
                uses_max=0,
                uses_count=0,
                per_user_limit=1,
                active=True,
            )
            session.add(c)
            await session.flush()
            cid = int(c.id)
    except Exception as exc:
        logger.exception(f"_create_coupon falhou: {exc}")
        await message.answer("❌ Erro ao criar cupom.")
        return

    await log_action(
        admin_id=admin_id,
        action="coupon.created",
        category="admin",
        details={"coupon_id": cid, "code": code, "type": ctype, "value": valor},
    )

    await message.answer(
        f"✅ <b>Cupom criado!</b>\n\n"
        f"🎟️ Código: <code>{code}</code>\n"
        f"🏷️ Tipo: <b>{TYPE_LABELS.get(ctype, ctype)}</b>\n"
        f"💵 Valor: <b>{fmt_brl(valor)}</b>\n"
        f"🎫 ID: <code>#{cid}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="✏️ Editar", callback_data=f"{CB_VIEW}:{cid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(cid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    c = await _load_coupon(cid)
    if c is None:
        return None, None

    usages = await _count_usages(cid)

    tipo = TYPE_LABELS.get(str(getattr(c.type, "value", c.type)), str(c.type))

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"🎟️ <b>CUPOM #{c.id}</b>\n\n"
            f"├ Código: <code>{c.code}</code>\n"
            f"├ Tipo: {tipo}\n"
            f"├ Valor: <b>{fmt_brl(c.value)}</b>\n"
            f"├ Usos: {int(c.uses_count or 0)}/{int(c.uses_max or 0)}\n"
            f"├ Limite/usuário: {int(c.per_user_limit or 0)}\n"
            f"├ Valor mínimo: {fmt_brl(c.min_purchase)}\n"
            f"├ Validade: {fmt_dt(c.expires_at, with_time=False) if c.expires_at else '—'}\n"
            f"├ Ativo: {humanize_bool(c.active)}\n"
            f"├ Criado em: {fmt_dt(c.created_at)}\n"
            f"└ Usos registrados: {usages}"
        ),
        id=c.id,
        codigo=c.code,
        tipo=tipo,
        valor=fmt_brl(c.value),
        usos=f"{int(c.uses_count or 0)}/{int(c.uses_max or 0)}",
        limite=int(c.per_user_limit or 0),
        min=fmt_brl(c.min_purchase),
        exp=fmt_dt(c.expires_at, with_time=False) if c.expires_at else "—",
        ativo=humanize_bool(c.active),
        created=fmt_dt(c.created_at),
        usages=usages,
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(c))

    return rendered.content, keyboard


def _default_detail_keyboard(c: Coupon) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Código", f"{CB_SET}:code:{c.id}"), b("✏️ Valor", f"{CB_SET}:value:{c.id}")],
        [b("🏷️ Tipo", f"{CB_TYPE_SET}:{c.id}:toggle")],
        [b("✏️ Usos máx", f"{CB_SET}:uses_max:{c.id}"), b("✏️ Limite/user", f"{CB_SET}:per_user_limit:{c.id}")],
        [b("✏️ Valor mínimo", f"{CB_SET}:min_purchase:{c.id}"), b("✏️ Validade", f"{CB_SET}:expires_at:{c.id}")],
        [b("🔄 Ativo/Inativo", f"{CB_TOGGLE}:{c.id}")],
        [b("📜 Ver Usos", f"{CB_USAGES}:{c.id}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{c.id}")],
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
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(cid)
        if text is None:
            await toast_err(callback, "❌ Cupom não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_view falhou: {exc}")


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
        # admin:coupons:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        cid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "money":
            hint = "Ex.: <code>10.00</code>"
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
            field=f"set:{field}:{cid}",
            screen=f"coupons:edit:{cid}",
            extra={"coupon_id": cid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_set_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    cid: int,
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
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = Decimal(str(round(num, 2)))
    elif meta["validate"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
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
            try:
                async with session_scope() as session:
                    r = await session.execute(
                        select(func.count(Coupon.id)).where(
                            Coupon.code == parsed,
                            Coupon.id != cid,
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

    # Se é percentual, limita
    if field == "value":
        try:
            async with session_scope() as session:
                r = await session.execute(select(Coupon).where(Coupon.id == cid))
                c = r.scalar_one_or_none()
                if c is not None:
                    t = str(getattr(c.type, "value", c.type)).upper()
                    if t == "PERCENT" and float(parsed) > 100:
                        await message.answer("❌ Percentual não pode passar de 100.")
                        return
        except Exception:
            pass

    try:
        async with session_scope() as session:
            r = await session.execute(select(Coupon).where(Coupon.id == cid))
            c: Optional[Coupon] = r.scalar_one_or_none()
            if c is None:
                await message.answer("❌ Cupom não encontrado.")
                return
            setattr(c, field, parsed)
            session.add(c)
    except Exception as exc:
        logger.exception(f"_apply_field_value (coupon) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"coupon.set.{field}",
        category="admin",
        details={"coupon_id": cid, "value": str(parsed)},
    )

    text, keyboard = await _render_detail(cid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE TIPO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TYPE_SET}:"))
async def on_type_set(callback: CallbackQuery) -> None:
    """
    Formato:
      admin:coupons:typeset:ID:toggle  → mostra opções
      admin:coupons:typeset:ID:FIXED|PERCENT|BONUS → aplica
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:coupons:typeset:ID:[toggle|FIXED|PERCENT|BONUS]
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        cid = int(parts[3])
        action = parts[4]

        if action == "toggle":
            rows_kb = [
                [
                    InlineKeyboardButton(
                        text="💰 Fixo (R$)",
                        callback_data=f"{CB_TYPE_SET}:{cid}:FIXED",
                    ),
                    InlineKeyboardButton(
                        text="📊 Percentual (%)",
                        callback_data=f"{CB_TYPE_SET}:{cid}:PERCENT",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🎁 Bônus saldo",
                        callback_data=f"{CB_TYPE_SET}:{cid}:BONUS",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=f"{CB_VIEW}:{cid}",
                    )
                ],
            ]
            text = "🏷️ <b>ESCOLHER TIPO</b>\n\nEscolha o novo tipo do cupom:"
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        if action not in ("FIXED", "PERCENT", "BONUS"):
            await toast_err(callback, "❌ Tipo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Coupon).where(Coupon.id == cid))
                c: Optional[Coupon] = r.scalar_one_or_none()
                if c is None:
                    await toast_err(callback, "❌ Cupom não encontrado.")
                    return
                c.type = CouponType(action)
                session.add(c)
        except Exception as exc:
            logger.exception(f"type set falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="coupon.type.set",
            category="admin",
            details={"coupon_id": cid, "type": action},
        )

        text, keyboard = await _render_detail(cid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🏷️ Tipo alterado.")
    except Exception as exc:
        logger.exception(f"coupons on_type_set falhou: {exc}")


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
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Coupon).where(Coupon.id == cid))
                c: Optional[Coupon] = r.scalar_one_or_none()
                if c is None:
                    await toast_err(callback, "❌ Cupom não encontrado.")
                    return
                c.active = not bool(c.active)
                session.add(c)
                new_val = bool(c.active)
        except Exception as exc:
            logger.exception(f"toggle coupon falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="coupon.toggle",
            category="admin",
            details={"coupon_id": cid, "active": new_val},
        )

        text, keyboard = await _render_detail(cid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "🔄 Alterado.")
    except Exception as exc:
        logger.exception(f"coupons on_toggle falhou: {exc}")


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
            cid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        c = await _load_coupon(cid)
        if c is None:
            await toast_err(callback, "❌ Cupom não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR CUPOM</b>\n\n"
            f"Código: <code>{c.code}</code>\n"
            f"Valor: <b>{fmt_brl(c.value)}</b>\n\n"
            "Isso removerá o cupom e o histórico de usos.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{cid}",
            cancel_callback=f"{CB_VIEW}:{cid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Remove usos
                await session.execute(
                    CouponUsage.__table__.delete().where(
                        CouponUsage.coupon_id == cid
                    )
                )
                # Remove cupom
                await session.execute(
                    Coupon.__table__.delete().where(Coupon.id == cid)
                )
        except Exception as exc:
            logger.exception(f"delete coupon falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="coupon.deleted",
            category="admin",
            details={"coupon_id": cid},
        )

        await render_list(callback, page=1, filter_key="all")
        await toast_ok(callback, "🗑️ Excluído.")
    except Exception as exc:
        logger.exception(f"coupons on_delete_confirm falhou: {exc}")


# ============================================================
# USOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_USAGES}:"))
async def on_usages(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            cid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(CouponUsage)
                    .where(CouponUsage.coupon_id == cid)
                    .order_by(CouponUsage.created_at.desc())
                    .limit(30)
                )
                usages = list(res.scalars().all())
        except Exception:
            usages = []

        if not usages:
            text = "📜 <i>Nenhum uso registrado.</i>"
        else:
            lines = [f"📜 <b>USOS · CUPOM #{cid}</b> ({len(usages)})", ""]
            for u in usages:
                lines.append(
                    f"👤 User <code>{u.user_id}</code> · "
                    f"-{fmt_brl(u.discount)} · {fmt_dt(u.created_at)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{cid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"coupons on_usages falhou: {exc}")


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
                res = await session.execute(select(Coupon).order_by(Coupon.id.asc()))
                coupons = list(res.scalars().all())
        except Exception:
            coupons = []

        headers = [
            "ID", "Código", "Tipo", "Valor", "Usos", "Usos Max",
            "Limite/user", "Mínimo", "Expira", "Ativo", "Criado",
        ]
        rows: List[List[Any]] = []
        for c in coupons:
            rows.append(
                [
                    c.id,
                    c.code,
                    str(getattr(c.type, "value", c.type)),
                    float(c.value or 0),
                    int(c.uses_count or 0),
                    int(c.uses_max or 0),
                    int(c.per_user_limit or 0),
                    float(c.min_purchase or 0),
                    fmt_dt(c.expires_at) if c.expires_at else "",
                    "sim" if c.active else "não",
                    fmt_dt(c.created_at),
                ]
            )

        filename = f"coupons_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📥 <b>Cupons</b> ({len(rows)})",
        )
        if ok:
            await toast_ok(callback, "📥 CSV enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action="coupon.export_csv",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"coupons on_export falhou: {exc}")


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
    "on_new_type",
    "on_coupons_text_input",
    "on_view",
    "on_set_field",
    "on_type_set",
    "on_toggle",
    "on_delete",
    "on_delete_confirm",
    "on_usages",
    "on_export",
    "CB_ROOT",
    "CB_PAGE",
    "CB_FILTER",
    "CB_NEW",
    "CB_NEW_TYPE",
    "CB_VIEW",
    "CB_SET",
    "CB_TYPE_SET",
    "CB_TOGGLE",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_USAGES",
    "CB_EXPORT",
    "FILTERS",
    "TEXT_FIELDS",
    "TYPE_LABELS",
]
