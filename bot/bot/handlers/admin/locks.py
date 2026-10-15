"""
Larizinha Store — Módulo Admin: Travas de Concorrência
Implementa o monitor e gestor de travas conforme DIM.

Telas:
- Lista de travas ativas (Redis: product:{id}:lock)
- Detalhes de uma trava (produto, usuário, início, expira, TTL)
- Liberar trava individual
- Liberar TODAS as travas (com confirmação)
- Ver locks órfãos (Redis sem TTL / tabela `locks` sem release)
- Limpar locks órfãos
- Estatísticas de concorrência
- Filtro por expirados / ativos

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Fonte real: Redis (chaves product:{id}:lock) + tabela `locks`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import Lock, Product, User, session_scope
from bot.logger import logger
from bot.redis_client import (
    Keys,
    LockManager,
    get_redis,
)
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_ERR,
    SYM_OK,
    SYM_WARN,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_dt,
    fmt_num,
    kb,
    log_action,
    page_offset,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_locks")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_locks_list"
MSG_KEY_EMPTY = "admin_locks_empty"
MSG_KEY_DETAIL = "admin_locks_detail"
MSG_KEY_STATS = "admin_locks_stats"
MSG_KEY_ORPHANS = "admin_locks_orphans"

SCREEN_LIST = "admin_locks_list"
SCREEN_DETAIL = "admin_locks_detail"

# Callbacks base
CB_ROOT = "admin:locks"
CB_PAGE = "admin:locks:page"           # :N
CB_VIEW = "admin:locks:view"           # :PID:UID
CB_RELEASE = "admin:locks:release"     # :PID:UID
CB_RELEASE_YES = "admin:locks:release:yes"  # :PID:UID
CB_RELEASE_ALL = "admin:locks:releaseall"
CB_RELEASE_ALL_YES = "admin:locks:releaseall:yes"
CB_ORPHANS = "admin:locks:orphans"
CB_CLEAN_ORPHANS = "admin:locks:cleanorphans"
CB_CLEAN_ORPHANS_YES = "admin:locks:cleanorphans:yes"
CB_STATS = "admin:locks:stats"
CB_RELOAD = "admin:locks:reload"


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _get_locks_from_redis() -> List[Dict[str, Any]]:
    """
    Retorna a lista REAL de locks ativos (Redis).
    Cada item: {product_id, user_id, quantity, created_at, ttl, token}
    """
    locks: List[Dict[str, Any]] = []
    try:
        r = await get_redis()
        pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
        async for key in r.scan_iter(match=pattern, count=200):
            raw = await r.get(key)
            if not raw:
                continue
            ttl = await r.ttl(key)
            try:
                import json as _json

                data = _json.loads(raw)
            except Exception:
                continue

            # Extrai product_id da chave
            product_id = None
            try:
                parts = str(key).split(":")
                # procura "product" no path
                for i, p in enumerate(parts):
                    if p == "product" and i + 1 < len(parts):
                        product_id = int(parts[i + 1])
                        break
            except Exception:
                product_id = None

            locks.append(
                {
                    "product_id": product_id,
                    "user_id": data.get("user_id"),
                    "quantity": data.get("quantity", 1),
                    "created_at": data.get("created_at"),
                    "token": data.get("token"),
                    "ttl": int(ttl) if ttl is not None else -1,
                    "key": key,
                }
            )
    except Exception as exc:
        logger.debug(f"_get_locks_from_redis falhou: {exc}")

    return locks


async def _resolve_names(locks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enriquece a lista com nome do produto e do usuário.
    """
    if not locks:
        return locks

    product_ids = {int(l["product_id"]) for l in locks if l.get("product_id")}
    user_ids = {int(l["user_id"]) for l in locks if l.get("user_id")}

    products_map: Dict[int, str] = {}
    users_map: Dict[int, str] = {}

    try:
        async with session_scope() as session:
            if product_ids:
                res = await session.execute(
                    select(Product.id, Product.name).where(
                        Product.id.in_(product_ids)
                    )
                )
                for pid, name in res.all():
                    products_map[int(pid)] = str(name or "—")

            if user_ids:
                res = await session.execute(
                    select(User.id, User.first_name).where(User.id.in_(user_ids))
                )
                for uid, name in res.all():
                    users_map[int(uid)] = str(name or "—")
    except Exception as exc:
        logger.debug(f"_resolve_names falhou: {exc}")

    for l in locks:
        l["product_name"] = products_map.get(int(l["product_id"] or 0), "—")
        l["user_name"] = users_map.get(int(l["user_id"] or 0), "—")

    return locks


async def _count_orphans() -> Tuple[int, int]:
    """
    Conta locks órfãos:
      - Redis: chaves sem TTL (ttl < 0)
      - DB: registros com expires_at <= now e released_at NULL
    """
    redis_orphans = 0
    db_orphans = 0

    try:
        r = await get_redis()
        pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
        async for key in r.scan_iter(match=pattern, count=200):
            ttl = await r.ttl(key)
            if ttl is None or ttl < 0:
                redis_orphans += 1
    except Exception:
        pass

    try:
        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Lock.id))
                .where(Lock.expires_at <= now)
                .where(Lock.released_at.is_(None))
            )
            db_orphans = int(res.scalar() or 0)
    except Exception:
        pass

    return redis_orphans, db_orphans


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    raw_locks = await _get_locks_from_redis()
    locks = await _resolve_names(raw_locks)

    total = len(locks)
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    start = page_offset(page, ADMIN_PAGE_SIZE_DEFAULT)
    page_locks = locks[start : start + ADMIN_PAGE_SIZE_DEFAULT]

    if not locks:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🔒 <b>TRAVAS DE CONCORRÊNCIA</b>\n\n"
                "<i>Nenhuma trava ativa no momento.</i>\n\n"
                "✅ Produtos estão livres para compra."
            ),
        )
    else:
        rows: List[List[str]] = []
        now = datetime.now(timezone.utc)
        for l in page_locks:
            ttl = int(l.get("ttl") or 0)
            if ttl > 0:
                status = SYM_OK
            else:
                status = SYM_WARN
            rows.append(
                [
                    str(l.get("product_id") or "—"),
                    truncate(l.get("product_name") or "—", 18),
                    str(l.get("user_id") or "—"),
                    truncate(l.get("user_name") or "—", 14),
                    f"{ttl}s",
                    status,
                ]
            )

        lista = table(rows, ["PID", "Produto", "UID", "Usuário", "TTL", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🔒 <b>TRAVAS ATIVAS</b> ({total})\n\n"
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
                InlineKeyboardButton(
                    text="🔄 Atualizar",
                    callback_data=CB_RELOAD,
                ),
                InlineKeyboardButton(
                    text="❌ Liberar Todas",
                    callback_data=CB_RELEASE_ALL,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Órfãos",
                    callback_data=CB_ORPHANS,
                ),
                InlineKeyboardButton(
                    text="📊 Estatísticas",
                    callback_data=CB_STATS,
                ),
            ],
        ]

    # Botões por trava
    for l in page_locks:
        pid = int(l.get("product_id") or 0)
        uid = int(l.get("user_id") or 0)
        ttl = int(l.get("ttl") or 0)
        emoji = "🟢" if ttl > 0 else "🟡"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{pid} → #{uid} ({ttl}s)",
                    callback_data=f"{CB_VIEW}:{pid}:{uid}",
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


@router.callback_query(F.data.in_({CB_ROOT, CB_RELOAD}))
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_list(callback, page=1)
        if callback.data == CB_RELOAD:
            await toast_ok(callback, "🔄 Atualizado!")
        else:
            await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_root falhou: {exc}")


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
        logger.exception(f"locks on_page falhou: {exc}")


# ============================================================
# DETALHE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:locks:view:PID:UID
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        try:
            pid = int(parts[4])
            uid = int(parts[5])
        except Exception:
            await toast_err(callback, "❌ IDs inválidos.")
            return

        raw_locks = await _get_locks_from_redis()
        target_lock = None
        for l in raw_locks:
            if int(l.get("product_id") or 0) == pid and int(l.get("user_id") or 0) == uid:
                target_lock = l
                break

        if target_lock is None:
            await toast_err(callback, "❌ Trava não encontrada (já expirou?).")
            return

        # Enriquecer
        enriched = await _resolve_names([target_lock])
        l = enriched[0]

        created_ts = l.get("created_at")
        try:
            created_dt = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
        except Exception:
            created_dt = None

        ttl = int(l.get("ttl") or 0)
        expires_dt = None
        if ttl > 0:
            expires_dt = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"🔒 <b>TRAVA ATIVA</b>\n\n"
                f"📦 Produto: <b>{l.get('product_name')}</b> (#{l.get('product_id')})\n"
                f"👤 Usuário: <b>{l.get('user_name')}</b> (#{l.get('user_id')})\n"
                f"📊 Quantidade: <b>{l.get('quantity')}</b>\n"
                f"🎫 Token: <code>{truncate(l.get('token'), 16)}</code>\n"
                f"📅 Início: {fmt_dt(created_dt)}\n"
                f"⏱️ TTL restante: <b>{ttl}s</b>\n"
                f"🕐 Expira em: {fmt_dt(expires_dt)}"
            ),
            product_id=l.get("product_id"),
            user_id=l.get("user_id"),
            produto=l.get("product_name") or "—",
            usuario=l.get("user_name") or "—",
            quantidade=l.get("quantity", 1),
            inicio=fmt_dt(created_dt),
            expira=fmt_dt(expires_dt),
            ttl=f"{ttl}s",
            status="🟢 Ativa" if ttl > 0 else "🟡 Órfã",
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="❌ Liberar Trava",
                    callback_data=f"{CB_RELEASE}:{pid}:{uid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Atualizar",
                    callback_data=f"{CB_VIEW}:{pid}:{uid}",
                ),
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                ),
            ],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_view falhou: {exc}")


# ============================================================
# LIBERAR INDIVIDUAL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RELEASE}:"))
async def on_release(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return  # confirmação
        # admin:locks:release:PID:UID
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        try:
            pid = int(parts[4])
            uid = int(parts[5])
        except Exception:
            await toast_err(callback, "❌ IDs inválidos.")
            return

        text = (
            f"❌ <b>LIBERAR TRAVA</b>\n\n"
            f"📦 Produto: <code>#{pid}</code>\n"
            f"👤 Usuário: <code>#{uid}</code>\n\n"
            "Isso libera o produto para outros usuários.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RELEASE_YES}:{pid}:{uid}",
            cancel_callback=f"{CB_VIEW}:{pid}:{uid}",
            confirm_text="❌ Sim, liberar",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_release falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RELEASE_YES}:"))
async def on_release_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:locks:release:yes:PID:UID
        if len(parts) < 7:
            await toast_err(callback, "❌ Callback inválido.")
            return
        try:
            pid = int(parts[5])
            uid = int(parts[6])
        except Exception:
            await toast_err(callback, "❌ IDs inválidos.")
            return

        try:
            ok = await LockManager.release_lock(pid, uid)
        except Exception as exc:
            logger.exception(f"release_lock falhou: {exc}")
            ok = False

        if not ok:
            await toast_err(callback, "❌ Não foi possível liberar.")
            return

        await log_action(
            admin_id=admin_id,
            action="lock.released",
            category="admin",
            details={"product_id": pid, "user_id": uid},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "❌ Trava liberada.")
    except Exception as exc:
        logger.exception(f"locks on_release_confirm falhou: {exc}")


# ============================================================
# LIBERAR TODAS
# ============================================================
@router.callback_query(F.data == CB_RELEASE_ALL)
async def on_release_all(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        locks = await _get_locks_from_redis()

        text = (
            f"⚠️ <b>LIBERAR TODAS AS TRAVAS</b>\n\n"
            f"🔢 Travas ativas: <b>{len(locks)}</b>\n\n"
            "Isso libera TODOS os produtos travados.\n"
            "Usuários em PIX/compra podem ser afetados.\n\n"
            "Tem certeza ABSOLUTA?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RELEASE_ALL_YES}:yes",
            cancel_callback=CB_ROOT,
            confirm_text="❌ Sim, liberar todas",
            cancel_text="↩️ Voltar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_release_all falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RELEASE_ALL_YES}:"))
async def on_release_all_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        locks = await _get_locks_from_redis()
        removed = 0
        for l in locks:
            pid = l.get("product_id")
            uid = l.get("user_id")
            try:
                if pid:
                    await LockManager.release_lock(int(pid), int(uid) if uid else None)
                    removed += 1
            except Exception:
                continue

        await log_action(
            admin_id=admin_id,
            action="lock.released_all",
            category="admin",
            details={"removed": removed},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, f"❌ {removed} trava(s) liberada(s).")
    except Exception as exc:
        logger.exception(f"locks on_release_all_confirm falhou: {exc}")


# ============================================================
# ÓRFÃOS
# ============================================================
@router.callback_query(F.data == CB_ORPHANS)
async def on_orphans(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        redis_orphans, db_orphans = await _count_orphans()

        rendered = await message_engine.render(
            MSG_KEY_ORPHANS,
            fallback=(
                "🔍 <b>TRAVAS ÓRFÃS</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ Redis (sem TTL): <b>{fmt_num(redis_orphans)}</b>\n"
                f"🗄️ Banco (expiradas): <b>{fmt_num(db_orphans)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Locks órfãos ocorrem quando um processo é interrompido\n"
                "antes de liberar a trava normalmente.\n\n"
                "Use 🧹 Limpar para removê-los com segurança."
            ),
            redis_orphans=fmt_num(redis_orphans),
            db_orphans=fmt_num(db_orphans),
            total=fmt_num(redis_orphans + db_orphans),
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="🧹 Limpar Órfãos",
                    callback_data=CB_CLEAN_ORPHANS,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Atualizar",
                    callback_data=CB_ORPHANS,
                ),
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=CB_ROOT,
                ),
            ],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_orphans falhou: {exc}")


@router.callback_query(F.data == CB_CLEAN_ORPHANS)
async def on_clean_orphans(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        redis_orphans, db_orphans = await _count_orphans()

        text = (
            f"🧹 <b>LIMPAR TRAVAS ÓRFÃS</b>\n\n"
            f"⚡ Redis: <b>{fmt_num(redis_orphans)}</b>\n"
            f"🗄️ Banco: <b>{fmt_num(db_orphans)}</b>\n\n"
            "Isso remove locks sem TTL e marca locks expiradas como liberadas.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CLEAN_ORPHANS_YES}:yes",
            cancel_callback=CB_ORPHANS,
            confirm_text="🧹 Sim, limpar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_clean_orphans falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CLEAN_ORPHANS_YES}:"))
async def on_clean_orphans_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Redis: remove chaves sem TTL
        redis_removed = 0
        try:
            r = await get_redis()
            pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
            async for key in r.scan_iter(match=pattern, count=200):
                ttl = await r.ttl(key)
                if ttl is None or ttl < 0:
                    await r.delete(key)
                    redis_removed += 1
        except Exception as exc:
            logger.debug(f"clean redis orphans falhou: {exc}")

        # Banco: marca como liberado
        db_removed = 0
        try:
            now = datetime.now(timezone.utc)
            async with session_scope() as session:
                res = await session.execute(
                    select(Lock)
                    .where(Lock.expires_at <= now)
                    .where(Lock.released_at.is_(None))
                )
                items = list(res.scalars().all())
                for l in items:
                    l.released_at = now
                    session.add(l)
                    db_removed += 1
        except Exception as exc:
            logger.debug(f"clean db orphans falhou: {exc}")

        await log_action(
            admin_id=admin_id,
            action="lock.clean_orphans",
            category="admin",
            details={"redis_removed": redis_removed, "db_removed": db_removed},
        )

        await on_orphans(callback)
        await toast_ok(
            callback,
            f"🧹 Redis: {redis_removed} · Banco: {db_removed}",
        )
    except Exception as exc:
        logger.exception(f"locks on_clean_orphans_confirm falhou: {exc}")


# ============================================================
# ESTATÍSTICAS
# ============================================================
@router.callback_query(F.data == CB_STATS)
async def on_stats(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        locks = await _get_locks_from_redis()

        ativos = sum(1 for l in locks if int(l.get("ttl") or 0) > 0)
        orfaos_redis = sum(1 for l in locks if int(l.get("ttl") or 0) <= 0)

        # Produtos únicos travados
        produtos_unicos = len({int(l.get("product_id") or 0) for l in locks})

        # Usuários únicos
        users_unicos = len({int(l.get("user_id") or 0) for l in locks})

        # TTL médio
        ttls = [int(l.get("ttl") or 0) for l in locks if int(l.get("ttl") or 0) > 0]
        ttl_medio = round(sum(ttls) / len(ttls), 1) if ttls else 0

        # Do DB
        try:
            async with session_scope() as session:
                total_db = int(
                    (
                        await session.execute(
                            select(func.count(Lock.id)).where(
                                Lock.released_at.is_(None)
                            )
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total_db = 0

        # Info do config
        lock_ttl = settings.PRODUCT_LOCK_TTL
        view_ttl = settings.USER_VIEW_TTL
        lock_enabled = settings.ENABLE_CONCURRENCY_LOCK

        rendered = await message_engine.render(
            MSG_KEY_STATS,
            fallback=(
                "📊 <b>ESTATÍSTICAS DE CONCORRÊNCIA</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔒 <b>Locks ativos (Redis)</b>\n"
                f"├ Total: <b>{fmt_num(len(locks))}</b>\n"
                f"├ 🟢 Com TTL: <b>{fmt_num(ativos)}</b>\n"
                f"├ 🟡 Órfãs: <b>{fmt_num(orfaos_redis)}</b>\n"
                f"├ 📦 Produtos únicos: <b>{fmt_num(produtos_unicos)}</b>\n"
                f"├ 👤 Usuários únicos: <b>{fmt_num(users_unicos)}</b>\n"
                f"└ ⏱️ TTL médio: <b>{ttl_medio}s</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🗄️ Locks não liberadas no DB: <b>{fmt_num(total_db)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚙️ <b>Configurações ativas</b>\n"
                f"├ Trava habilitada: {'🟢 sim' if lock_enabled else '🔴 não'}\n"
                f"├ Product Lock TTL: <b>{lock_ttl}s</b>\n"
                f"└ User View TTL: <b>{view_ttl}s</b>"
            ),
            total=fmt_num(len(locks)),
            ativos=fmt_num(ativos),
            orfaos=fmt_num(orfaos_redis),
            produtos=fmt_num(produtos_unicos),
            users=fmt_num(users_unicos),
            ttl_medio=f"{ttl_medio}s",
            db_pend=fmt_num(total_db),
            enabled="🟢 sim" if lock_enabled else "🔴 não",
            lock_ttl=f"{lock_ttl}s",
            view_ttl=f"{view_ttl}s",
        )

        await edit_or_send(
            callback,
            text=rendered.content,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🔄 Atualizar",
                            callback_data=CB_STATS,
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔙 Voltar",
                            callback_data=CB_ROOT,
                        )
                    ],
                ]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"locks on_stats falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_view",
    "on_release",
    "on_release_confirm",
    "on_release_all",
    "on_release_all_confirm",
    "on_orphans",
    "on_clean_orphans",
    "on_clean_orphans_confirm",
    "on_stats",
    "CB_ROOT",
    "CB_PAGE",
    "CB_VIEW",
    "CB_RELEASE",
    "CB_RELEASE_YES",
    "CB_RELEASE_ALL",
    "CB_RELEASE_ALL_YES",
    "CB_ORPHANS",
    "CB_CLEAN_ORPHANS",
    "CB_CLEAN_ORPHANS_YES",
    "CB_STATS",
    "CB_RELOAD",
]
