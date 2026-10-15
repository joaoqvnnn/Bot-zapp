"""
Larizinha Store — Módulo Admin: Blacklist
Implementa gestão completa da lista de usuários bloqueados conforme DIM.

Telas:
- Lista de blacklist (com paginação DIM + busca por ID)
- Adicionar (via Telegram ID + motivo obrigatório)
- Remover com confirmação
- Detalhes do item
- Exportação CSV

Integração:
- `user_loader` middleware consulta essa tabela e bloqueia o acesso ao bot.
- Tentativa de acesso por usuário bloqueado é registrada em `security_events`.

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, or_, select

from bot.config import settings
from bot.database import Blacklist, User, session_scope
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_dt,
    fmt_num,
    is_int,
    kb,
    log_action,
    page_offset,
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
router = Router(name="admin_blacklist")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_blacklist_list"
MSG_KEY_EMPTY = "admin_blacklist_empty"
MSG_KEY_DETAIL = "admin_blacklist_detail"

SCREEN_LIST = "admin_blacklist_list"
SCREEN_DETAIL = "admin_blacklist_detail"

# Callbacks base
CB_ROOT = "admin:blacklist"
CB_PAGE = "admin:blacklist:page"            # :N
CB_SEARCH = "admin:blacklist:search"
CB_NEW = "admin:blacklist:new"
CB_VIEW = "admin:blacklist:view"            # :ID
CB_EDIT_REASON = "admin:blacklist:reason"   # :ID
CB_REMOVE = "admin:blacklist:remove"        # :ID
CB_REMOVE_YES = "admin:blacklist:remove:yes"  # :ID
CB_EXPORT = "admin:blacklist:export"


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_entry(eid: int) -> Optional[Blacklist]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Blacklist).where(Blacklist.id == eid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_by_tg(tg_id: int) -> Optional[Blacklist]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Blacklist).where(Blacklist.telegram_id == tg_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_entries() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Blacklist.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_entries(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Blacklist]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Blacklist)
                .order_by(Blacklist.created_at.desc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    total = await _count_entries()
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    entries = await _list_entries(page)

    if not entries:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🚫 <b>BLACKLIST</b>\n\n"
                "<i>Nenhum usuário bloqueado.</i>\n\n"
                "Use ➕ Adicionar para incluir um Telegram ID."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for e in entries:
            rows.append(
                [
                    str(e.id),
                    str(e.telegram_id),
                    truncate(e.reason or "—", 30),
                    fmt_dt(e.created_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "Telegram", "Motivo", "Data"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🚫 <b>BLACKLIST</b> ({total})\n\n"
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
                InlineKeyboardButton(text="➕ Adicionar", callback_data=CB_NEW),
                InlineKeyboardButton(text="🔍 Buscar", callback_data=CB_SEARCH),
            ],
            [
                InlineKeyboardButton(text="📥 Exportar CSV", callback_data=CB_EXPORT),
            ],
        ]

    for e in entries:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"🚫 #{e.id} · {e.telegram_id} · "
                    f"{truncate(e.reason or '—', 15)}",
                    callback_data=f"{CB_VIEW}:{e.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=CB_PAGE,
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
        await render_list(callback, page=1)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1
        await render_list(callback, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_page falhou: {exc}")


# ============================================================
# BUSCAR POR ID
# ============================================================
@router.callback_query(F.data == CB_SEARCH)
async def on_search(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        prompt = (
            "🔍 <b>BUSCAR NA BLACKLIST</b>\n\n"
            "Envie o Telegram ID ou ID interno.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="blacklist_search_query",
            screen="blacklist:search",
            use_force_reply=True,
            reply_placeholder="Telegram ID ou ID interno",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_search falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_blacklist_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Busca
    if awaiting == "blacklist_search_query":
        value, _extra = await consume_input(message, admin_id, expected_field="blacklist_search_query")
        if value is None:
            return
        await _do_search(message, admin_id, value)
        return

    # Adicionar (PASSO 1: telegram_id)
    if awaiting == "blacklist_new_tg":
        value, _extra = await consume_input(message, admin_id, expected_field="blacklist_new_tg")
        if value is None:
            return
        await _add_step1(message, admin_id, value)
        return

    # Adicionar (PASSO 2: motivo)
    if awaiting == "blacklist_new_reason":
        value, _extra = await consume_input(message, admin_id, expected_field="blacklist_new_reason")
        if value is None:
            return
        await _add_step2(message, admin_id, value)
        return

    # Editar motivo
    if awaiting.startswith("blacklist_reason:"):
        eid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_reason(message, admin_id, eid, value)
        return


async def _do_search(message: Message, admin_id: int, raw: str) -> None:
    raw = (raw or "").strip()
    if not raw:
        await message.answer("❌ Valor vazio.")
        return

    results: List[Blacklist] = []
    try:
        async with session_scope() as session:
            if is_int(raw):
                num = int(raw)
                res = await session.execute(
                    select(Blacklist)
                    .where(
                        or_(
                            Blacklist.id == num,
                            Blacklist.telegram_id == num,
                        )
                    )
                    .limit(20)
                )
                results = list(res.scalars().all())
    except Exception:
        results = []

    if not results:
        await message.answer(
            text="🔍 <b>NENHUM RESULTADO</b>",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    rows_kb: List[List[InlineKeyboardButton]] = []
    for e in results:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"🚫 #{e.id} · {e.telegram_id}",
                    callback_data=f"{CB_VIEW}:{e.id}",
                )
            ]
        )
    rows_kb.append([InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)])

    await message.answer(
        text=f"🔍 <b>RESULTADOS ({len(results)})</b>",
        parse_mode="HTML",
        reply_markup=kb(rows_kb),
        disable_web_page_preview=True,
    )


# ============================================================
# ADICIONAR (2 passos)
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>ADICIONAR À BLACKLIST · PASSO 1/2</b>\n\n"
            "Envie o <b>Telegram ID</b> do usuário.\n"
            "Ex.: <code>123456789</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="blacklist_new_tg",
            screen="blacklist:new",
            use_force_reply=True,
            reply_placeholder="Telegram ID",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_new falhou: {exc}")


async def _add_step1(message: Message, admin_id: int, raw: str) -> None:
    raw = (raw or "").strip()
    if not is_int(raw):
        await message.answer("❌ Telegram ID inválido.")
        return
    tg_id = int(raw)
    if tg_id <= 0:
        await message.answer("❌ Telegram ID inválido.")
        return

    # Já existe?
    existing = await _load_by_tg(tg_id)
    if existing is not None:
        await message.answer(
            f"❌ Este Telegram ID já está na blacklist (#{existing.id})."
        )
        return

    # Tenta achar o usuário no banco (não bloqueia se não existir)
    user_info = ""
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(User).where(User.telegram_id == tg_id)
            )
            u = r.scalar_one_or_none()
            if u is not None:
                user_info = (
                    f"\n\n👤 Encontrado no banco:\n"
                    f"• Nome: <b>{u.first_name or '—'}</b>\n"
                    f"• Username: @{u.username or '—'}\n"
                    f"• ID interno: <code>#{u.id}</code>"
                )
    except Exception:
        pass

    await AdminFlow.update(admin_id, {"new_tg_id": tg_id})

    prompt = (
        f"➕ <b>ADICIONAR À BLACKLIST · PASSO 2/2</b>\n\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>{user_info}\n\n"
        "Agora envie o <b>motivo</b> (obrigatório).\n"
        "Ex.: <i>Fraude, spam, reincidência...</i>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="blacklist_new_reason",
        screen="blacklist:new",
        use_force_reply=True,
        reply_placeholder="Motivo",
    )


async def _add_step2(message: Message, admin_id: int, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        await message.answer("❌ Motivo é obrigatório.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    tg_id = data.get("new_tg_id")
    if not tg_id:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    try:
        async with session_scope() as session:
            e = Blacklist(
                telegram_id=int(tg_id),
                reason=reason[:300],
                added_by_id=admin_id,
            )
            session.add(e)
            await session.flush()
            eid = int(e.id)
    except Exception as exc:
        logger.exception(f"_add_step2 falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await AdminFlow.clear(admin_id)
    await log_action(
        admin_id=admin_id,
        action="blacklist.added",
        category="security",
        details={"entry_id": eid, "telegram_id": tg_id, "reason": reason},
    )

    # Marca o usuário também como bloqueado (se existir)
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(User).where(User.telegram_id == int(tg_id))
            )
            u = r.scalar_one_or_none()
            if u is not None:
                from bot.database import UserStatus

                u.status = UserStatus.BLOCKED
                u.block_reason = reason[:200]
                session.add(u)
    except Exception:
        pass

    await message.answer(
        f"✅ <b>Adicionado à blacklist</b>\n\n"
        f"🆔 Telegram: <code>{tg_id}</code>\n"
        f"📝 Motivo: {truncate(reason, 80)}\n"
        f"🎫 ID interno: <code>#{eid}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="👁️ Ver", callback_data=f"{CB_VIEW}:{eid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE
# ============================================================
async def _render_detail(eid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    e = await _load_entry(eid)
    if e is None:
        return None, None

    # Info adicional do usuário (se existir no banco)
    user_info = ""
    try:
        async with session_scope() as session:
            r = await session.execute(
                select(User).where(User.telegram_id == int(e.telegram_id))
            )
            u = r.scalar_one_or_none()
            if u is not None:
                user_info = (
                    f"\n👤 Nome: {u.first_name or '—'}\n"
                    f"🔗 Username: @{u.username or '—'}\n"
                    f"🆔 ID interno: <code>{u.id}</code>\n"
                    f"💰 Saldo: R$ {float(u.balance or 0):.2f}\n"
                )
    except Exception:
        pass

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"🚫 <b>BLACKLIST #{e.id}</b>\n\n"
            f"🆔 Telegram: <code>{e.telegram_id}</code>\n"
            f"📝 Motivo: {e.reason or '—'}\n"
            f"📅 Adicionado: {fmt_dt(e.created_at)}\n"
            f"👮 Por admin: <code>{e.added_by_id or '—'}</code>"
            f"{user_info}"
        ),
        id=e.id,
        telegram_id=e.telegram_id,
        motivo=e.reason or "—",
        created=fmt_dt(e.created_at),
        admin=e.added_by_id or "—",
    )

    keyboard = kb(
        [
            [
                InlineKeyboardButton(
                    text="✏️ Editar Motivo",
                    callback_data=f"{CB_EDIT_REASON}:{e.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Remover da Blacklist",
                    callback_data=f"{CB_REMOVE}:{e.id}",
                )
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
        ]
    )

    return rendered.content, keyboard


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            eid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(eid)
        if text is None:
            await toast_err(callback, "❌ Entrada não encontrada.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_view falhou: {exc}")


# ============================================================
# EDITAR MOTIVO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EDIT_REASON}:"))
async def on_edit_reason(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            eid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        e = await _load_entry(eid)
        if e is None:
            await toast_err(callback, "❌ Entrada não encontrada.")
            return

        prompt = (
            f"✏️ <b>EDITAR MOTIVO · #{eid}</b>\n\n"
            f"🆔 Telegram: <code>{e.telegram_id}</code>\n\n"
            "Envie o novo motivo.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"blacklist_reason:{eid}",
            screen=f"blacklist:edit:{eid}",
            extra={"entry_id": eid},
            use_force_reply=True,
            reply_placeholder="Motivo",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_edit_reason falhou: {exc}")


async def _apply_reason(
    message: Message,
    admin_id: int,
    eid: int,
    new_reason: str,
) -> None:
    new_reason = (new_reason or "").strip()
    if not new_reason:
        await message.answer("❌ Motivo vazio.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Blacklist).where(Blacklist.id == eid))
            e: Optional[Blacklist] = r.scalar_one_or_none()
            if e is None:
                await message.answer("❌ Entrada não encontrada.")
                return
            e.reason = new_reason[:300]
            session.add(e)
    except Exception as exc:
        logger.exception(f"_apply_reason falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action="blacklist.reason.updated",
        category="security",
        details={"entry_id": eid},
    )

    text, keyboard = await _render_detail(eid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# REMOVER
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REMOVE}:"))
async def on_remove(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            eid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        e = await _load_entry(eid)
        if e is None:
            await toast_err(callback, "❌ Entrada não encontrada.")
            return

        text = (
            f"✅ <b>REMOVER DA BLACKLIST</b>\n\n"
            f"🆔 Telegram: <code>{e.telegram_id}</code>\n"
            f"📝 Motivo: {truncate(e.reason or '—', 60)}\n\n"
            "Após remover, o usuário poderá usar o bot novamente.\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_REMOVE_YES}:{eid}",
            cancel_callback=f"{CB_VIEW}:{eid}",
            confirm_text="✅ Sim, remover",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"blacklist on_remove falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REMOVE_YES}:"))
async def on_remove_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            eid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        e = await _load_entry(eid)
        if e is None:
            await toast_err(callback, "❌ Entrada não encontrada.")
            return
        tg_id = int(e.telegram_id)

        try:
            async with session_scope() as session:
                await session.execute(
                    Blacklist.__table__.delete().where(Blacklist.id == eid)
                )
        except Exception as exc:
            logger.exception(f"delete blacklist falhou: {exc}")
            await toast_err(callback, "❌ Erro ao remover.")
            return

        # Reativa o usuário se estiver bloqueado por causa dessa blacklist
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(User).where(User.telegram_id == tg_id)
                )
                u = r.scalar_one_or_none()
                if u is not None:
                    from bot.database import UserStatus

                    if u.status == UserStatus.BLOCKED:
                        u.status = UserStatus.ACTIVE
                        u.block_reason = None
                        session.add(u)
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="blacklist.removed",
            category="security",
            details={"entry_id": eid, "telegram_id": tg_id},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "✅ Removido da blacklist.")
    except Exception as exc:
        logger.exception(f"blacklist on_remove_confirm falhou: {exc}")


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
                    select(Blacklist).order_by(Blacklist.id.asc())
                )
                entries = list(res.scalars().all())
        except Exception:
            entries = []

        if not entries:
            await toast_err(callback, "❌ Sem dados para exportar.")
            return

        headers = ["ID", "Telegram ID", "Motivo", "Adicionado por", "Criado em"]
        rows: List[List[Any]] = []
        for e in entries:
            rows.append(
                [
                    e.id,
                    e.telegram_id,
                    e.reason or "",
                    e.added_by_id or "",
                    fmt_dt(e.created_at),
                ]
            )

        filename = f"blacklist_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📥 <b>Blacklist</b> ({len(rows)})",
        )
        if ok:
            await toast_ok(callback, "📥 CSV enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action="blacklist.export_csv",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"blacklist on_export falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_search",
    "on_blacklist_text_input",
    "on_new",
    "on_view",
    "on_edit_reason",
    "on_remove",
    "on_remove_confirm",
    "on_export",
    "CB_ROOT",
    "CB_PAGE",
    "CB_SEARCH",
    "CB_NEW",
    "CB_VIEW",
    "CB_EDIT_REASON",
    "CB_REMOVE",
    "CB_REMOVE_YES",
    "CB_EXPORT",
]
