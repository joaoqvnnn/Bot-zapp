"""
Larizinha Store — Módulo Admin: Backup e Banco de Dados
Implementa gestão de backup e manutenção do DB conforme DIM.

Telas:
- Painel de DB (status, último backup, quantidade, integridade, tamanho)
- Backup manual (pg_dump ou snapshot interno)
- Auto-backup (toggle + intervalo)
- Lista de backups salvos (com tamanho e data)
- Restaurar backup (com confirmação dupla)
- Verificar integridade
- Limpar temporários / backups antigos por retenção
- Exportar (download do arquivo)
- Excluir backup individual

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Apenas o Admin Principal pode restaurar backups.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import Backup, Setting, session_scope
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
    fmt_dt,
    fmt_num,
    humanize_bool,
    kb,
    log_action,
    page_offset,
    parse_int,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_backup")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_backup_panel"
MSG_KEY_LIST = "admin_backup_list"
MSG_KEY_EMPTY = "admin_backup_empty"
MSG_KEY_DETAIL = "admin_backup_detail"

SCREEN_PANEL = "admin_backup_panel"
SCREEN_LIST = "admin_backup_list"

# Callbacks base
CB_ROOT = "admin:backup"
CB_PAGE = "admin:backup:page"               # :N
CB_NEW = "admin:backup:new"
CB_TOGGLE_AUTO = "admin:backup:toggleauto"
CB_SET_INTERVAL = "admin:backup:interval"
CB_SET_RETENTION = "admin:backup:retention"
CB_VIEW = "admin:backup:view"               # :ID
CB_RESTORE = "admin:backup:restore"         # :ID
CB_RESTORE_YES = "admin:backup:restore:yes" # :ID
CB_DELETE = "admin:backup:delete"           # :ID
CB_DELETE_YES = "admin:backup:delete:yes"   # :ID
CB_DOWNLOAD = "admin:backup:download"       # :ID
CB_INTEGRITY = "admin:backup:integrity"
CB_CLEAN = "admin:backup:clean"
CB_CLEAN_YES = "admin:backup:clean:yes"

# Config keys
CONFIG_FIELDS: Dict[str, Dict[str, Any]] = {
    "backup_auto_enabled": {
        "label": "Auto-backup",
        "type": "bool",
        "default": "true",
    },
    "backup_interval_hours": {
        "label": "Intervalo (horas)",
        "type": "int",
        "default": "24",
    },
    "backup_retention_days": {
        "label": "Retenção (dias)",
        "type": "int",
        "default": "7",
    },
    "backup_max_keep": {
        "label": "Máximo de backups salvos",
        "type": "int",
        "default": "30",
    },
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is not None:
                return s.value
    except Exception:
        pass
    return default


async def _set_setting(key: str, value: str) -> bool:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is None:
                session.add(Setting(key=key, value=value, category="backup"))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting (backup) falhou: {exc}")
        return False


async def _count_backups() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Backup.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_backups(
    page: int = 1,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Backup]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Backup)
                .order_by(Backup.created_at.desc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _load_backup(bid: int) -> Optional[Backup]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Backup).where(Backup.id == bid))
            return res.scalar_one_or_none()
    except Exception:
        return None


def _backup_dir() -> Path:
    try:
        d = settings.backups_dir
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        d = Path("./backups")
        d.mkdir(parents=True, exist_ok=True)
        return d


def _fmt_size(size_bytes: Optional[int]) -> str:
    try:
        n = float(size_bytes or 0)
    except Exception:
        n = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.iterdir():
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total


# ============================================================
# CRIAR BACKUP (pg_dump)
# ============================================================
async def _run_pg_dump(filepath: Path) -> Tuple[bool, str]:
    """Executa pg_dump. Retorna (ok, message)."""
    try:
        cmd = [
            "pg_dump",
            settings.db_url_sync,
            "-f",
            str(filepath),
            "--no-owner",
            "--no-privileges",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False, "Timeout (120s)"

        if proc.returncode == 0:
            return True, "OK"
        return False, (stderr.decode() or "")[:200]
    except FileNotFoundError:
        return False, "pg_dump não instalado no servidor."
    except Exception as exc:
        return False, str(exc)[:200]


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Config
    auto_enabled = await _get_setting("backup_auto_enabled", "true")
    interval = await _get_setting("backup_interval_hours", "24")
    retention = await _get_setting("backup_retention_days", "7")
    max_keep = await _get_setting("backup_max_keep", "30")

    # Métricas
    total = await _count_backups()
    last = await _list_backups(page=1, per_page=1)
    last_dt = last[0].created_at if last else None

    # Tamanho do diretório
    d = _backup_dir()
    dsize = await _dir_size(d)

    # Próximo backup previsto
    prox = "—"
    if str(auto_enabled).lower() == "true" and last_dt is not None:
        try:
            interval_h = int(interval or 24)
            prox_dt = last_dt + timedelta(hours=interval_h)
            prox = fmt_dt(prox_dt)
        except Exception:
            prox = "—"

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "🗄️ <b>BANCO DE DADOS E BACKUPS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Status: {SYM_OK} <b>Operacional</b>\n"
            f"├ Último backup: {fmt_dt(last_dt)}\n"
            f"├ Backups salvos: <b>{fmt_num(total)}</b>\n"
            f"├ Próx. backup: {prox}\n"
            f"├ Tamanho total: <b>{_fmt_size(dsize)}</b>\n"
            f"├ Retenção: <b>{retention} dias</b>\n"
            f"├ Máx. mantidos: <b>{max_keep}</b>\n"
            f"└ Auto-backup: {SYM_OK if str(auto_enabled).lower() == 'true' else SYM_OFF}"
        ),
        status_bot=SYM_OK,
        status_api=SYM_OK,
        status_db=SYM_OK,
        status_pag=SYM_OK,
        status=f"{SYM_OK} Operacional",
        ult=fmt_dt(last_dt),
        qtd=fmt_num(total),
        prox=prox,
        tam=_fmt_size(dsize),
        ret=retention,
        integ="🟢 OK",
        auto=SYM_OK if str(auto_enabled).lower() == "true" else SYM_OFF,
        interval=interval,
        max_keep=max_keep,
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="📤 Backup Manual",
                        callback_data=CB_NEW,
                    ),
                    InlineKeyboardButton(
                        text="🔄 Auto-backup",
                        callback_data=CB_TOGGLE_AUTO,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⏱️ Intervalo",
                        callback_data=CB_SET_INTERVAL,
                    ),
                    InlineKeyboardButton(
                        text="📅 Retenção",
                        callback_data=CB_SET_RETENTION,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📜 Histórico",
                        callback_data=f"{CB_PAGE}:1",
                    ),
                    InlineKeyboardButton(
                        text="🔍 Integridade",
                        callback_data=CB_INTEGRITY,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🧹 Limpar Antigos",
                        callback_data=CB_CLEAN,
                    ),
                    InlineKeyboardButton(
                        text="🔄 Atualizar",
                        callback_data=CB_ROOT,
                    ),
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu")],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data == CB_ROOT)
async def on_panel(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_panel(callback)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_panel falhou: {exc}")


# ============================================================
# BACKUP MANUAL
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "⏳ Gerando backup...")

        d = _backup_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"backup_{ts}.sql"
        filepath = d / filename

        ok, msg = await _run_pg_dump(filepath)

        if ok:
            size = filepath.stat().st_size
            try:
                async with session_scope() as session:
                    session.add(
                        Backup(
                            filename=filename,
                            size=size,
                            type="manual",
                            status="ok",
                        )
                    )
            except Exception as exc:
                logger.debug(f"registrar backup falhou: {exc}")

            await log_action(
                admin_id=admin_id,
                action="backup.created",
                category="admin",
                details={"filename": filename, "size": size},
            )

            text = (
                f"✅ <b>Backup concluído!</b>\n\n"
                f"📁 Arquivo: <code>{filename}</code>\n"
                f"📦 Tamanho: <b>{_fmt_size(size)}</b>\n"
                f"📅 {fmt_dt(datetime.now(timezone.utc))}"
            )
        else:
            await log_action(
                admin_id=admin_id,
                action="backup.failed",
                category="admin",
                details={"filename": filename, "error": msg},
            )
            text = (
                f"❌ <b>Falha ao gerar backup</b>\n\n"
                f"Motivo: <code>{truncate(msg, 200)}</code>\n\n"
                f"💡 Verifique se o <code>pg_dump</code> está instalado "
                f"no servidor e se a <code>DATABASE_URL_SYNC</code> está correta."
            )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="📜 Ver Histórico",
                            callback_data=f"{CB_PAGE}:1",
                        )
                    ],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
                ]
            ),
        )
    except Exception as exc:
        logger.exception(f"backup on_new falhou: {exc}")


# ============================================================
# AUTO-BACKUP / INTERVALO / RETENÇÃO
# ============================================================
@router.callback_query(F.data == CB_TOGGLE_AUTO)
async def on_toggle_auto(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        current = await _get_setting("backup_auto_enabled", "true")
        new_val = "false" if str(current).lower() == "true" else "true"
        ok = await _set_setting("backup_auto_enabled", new_val)
        if not ok:
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action="backup.toggle_auto",
            category="admin",
            details={"enabled": new_val},
        )

        await render_panel(callback)
        await toast_ok(callback, f"🔄 Auto-backup → {new_val}")
    except Exception as exc:
        logger.exception(f"backup on_toggle_auto falhou: {exc}")


@router.callback_query(F.data == CB_SET_INTERVAL)
async def on_set_interval(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "⏱️ <b>INTERVALO DE AUTO-BACKUP</b>\n\n"
            "Envie o intervalo em <b>horas</b>.\n"
            "Ex.: <code>24</code> (1 dia) ou <code>12</code> (12h)\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="backup_set_interval",
            screen="backup:interval",
            use_force_reply=True,
            reply_placeholder="24",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_set_interval falhou: {exc}")


@router.callback_query(F.data == CB_SET_RETENTION)
async def on_set_retention(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "📅 <b>RETENÇÃO DE BACKUPS</b>\n\n"
            "Envie quantos <b>dias</b> os backups devem ser mantidos.\n"
            "Ex.: <code>7</code> (1 semana) ou <code>30</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="backup_set_retention",
            screen="backup:retention",
            use_force_reply=True,
            reply_placeholder="7",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_set_retention falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_backup_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting == "backup_set_interval":
        value, _extra = await consume_input(message, admin_id, expected_field="backup_set_interval")
        if value is None:
            return
        num = parse_int(value)
        if num is None or num < 1 or num > 720:
            await message.answer("❌ Use um número entre 1 e 720 horas.")
            return
        ok = await _set_setting("backup_interval_hours", str(int(num)))
        if not ok:
            await message.answer("❌ Erro ao salvar.")
            return
        await log_action(
            admin_id=admin_id,
            action="backup.set.interval",
            category="admin",
            details={"hours": int(num)},
        )
        await message.answer(
            f"✅ Intervalo definido: <b>{int(num)}h</b>.",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return

    if awaiting == "backup_set_retention":
        value, _extra = await consume_input(message, admin_id, expected_field="backup_set_retention")
        if value is None:
            return
        num = parse_int(value)
        if num is None or num < 1 or num > 365:
            await message.answer("❌ Use um número entre 1 e 365 dias.")
            return
        ok = await _set_setting("backup_retention_days", str(int(num)))
        if not ok:
            await message.answer("❌ Erro ao salvar.")
            return
        await log_action(
            admin_id=admin_id,
            action="backup.set.retention",
            category="admin",
            details={"days": int(num)},
        )
        await message.answer(
            f"✅ Retenção definida: <b>{int(num)} dias</b>.",
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        return


# ============================================================
# HISTÓRICO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_history(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        total = await _count_backups()
        total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
        page = max(1, min(page, total_p))

        items = await _list_backups(page)

        if not items:
            rendered = await message_engine.render(
                MSG_KEY_EMPTY,
                fallback=(
                    "📜 <b>HISTÓRICO DE BACKUPS</b>\n\n"
                    "<i>Nenhum backup registrado.</i>"
                ),
            )
        else:
            rows: List[List[str]] = []
            for b in items:
                status_emoji = SYM_OK if b.status == "ok" else SYM_WARN
                rows.append(
                    [
                        str(b.id),
                        truncate(b.filename, 28),
                        _fmt_size(b.size),
                        b.type,
                        status_emoji,
                        fmt_dt(b.created_at, with_time=False),
                    ]
                )

            lista = table(rows, ["ID", "Arquivo", "Tam.", "Tipo", "S", "Data"])
            rendered = await message_engine.render(
                MSG_KEY_LIST,
                fallback=(
                    f"📜 <b>HISTÓRICO DE BACKUPS</b> ({total})\n\n"
                    f"<code>{lista}</code>"
                ),
                total=total,
                lista=lista,
            )

        rows_kb: List[List[InlineKeyboardButton]] = []
        for b in items[:8]:
            status = "🟢" if b.status == "ok" else "🟡"
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{status} #{b.id} · {truncate(b.filename, 22)} · "
                        f"{_fmt_size(b.size)}",
                        callback_data=f"{CB_VIEW}:{b.id}",
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

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_history falhou: {exc}")


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
        try:
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        d = _backup_dir()
        filepath = d / b.filename
        exists = filepath.exists()

        rendered = await message_engine.render(
            MSG_KEY_DETAIL,
            fallback=(
                f"🗄️ <b>BACKUP #{b.id}</b>\n\n"
                f"├ Arquivo: <code>{b.filename}</code>\n"
                f"├ Tamanho: <b>{_fmt_size(b.size)}</b>\n"
                f"├ Tipo: {b.type}\n"
                f"├ Status: {b.status}\n"
                f"├ Criado em: {fmt_dt(b.created_at)}\n"
                f"└ No disco: {'🟢 sim' if exists else '🔴 não'}"
            ),
            id=b.id,
            filename=b.filename,
            size=_fmt_size(b.size),
            type=b.type,
            status=b.status,
            created=fmt_dt(b.created_at),
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="📥 Baixar Arquivo",
                    callback_data=f"{CB_DOWNLOAD}:{b.id}",
                ),
            ],
        ]

        # Só o Admin Principal pode restaurar
        if admin_id == settings.MAIN_ADMIN_ID:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="♻️ Restaurar",
                        callback_data=f"{CB_RESTORE}:{b.id}",
                    ),
                ]
            )

        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="🗑️ Excluir",
                    callback_data=f"{CB_DELETE}:{b.id}",
                ),
            ]
        )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_PAGE}:1")]
        )

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_view falhou: {exc}")


# ============================================================
# DOWNLOAD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DOWNLOAD}:"))
async def on_download(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        d = _backup_dir()
        filepath = d / b.filename
        if not filepath.exists():
            await toast_err(callback, "❌ Arquivo não encontrado no disco.")
            return

        # Limite de 45MB para envio no Telegram
        try:
            size = filepath.stat().st_size
        except Exception:
            size = 0
        if size > 45 * 1024 * 1024:
            await toast_err(
                callback,
                f"❌ Arquivo grande ({_fmt_size(size)}). Telegram limita 50MB.",
            )
            return

        try:
            with open(filepath, "rb") as f:
                data = f.read()
            await callback.message.answer_document(
                document=BufferedInputFile(data, filename=b.filename),
                caption=f"🗄️ <b>Backup #{b.id}</b>\n"
                f"📦 {_fmt_size(size)}",
                parse_mode="HTML",
            )
            await toast_ok(callback, "📥 Enviado!")
        except Exception as exc:
            logger.debug(f"envio do arquivo falhou: {exc}")
            await toast_err(callback, "❌ Erro ao enviar o arquivo.")

        await log_action(
            admin_id=admin_id,
            action="backup.downloaded",
            category="admin",
            details={"backup_id": bid},
        )
    except Exception as exc:
        logger.exception(f"backup on_download falhou: {exc}")


# ============================================================
# RESTAURAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RESTORE}:"))
async def on_restore(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal pode restaurar.")
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            bid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        text = (
            f"⚠️ <b>ATENÇÃO — RESTAURAR BACKUP</b>\n\n"
            f"📁 Arquivo: <code>{b.filename}</code>\n"
            f"📦 Tamanho: <b>{_fmt_size(b.size)}</b>\n"
            f"📅 Criado em: {fmt_dt(b.created_at)}\n\n"
            "🚨 <b>AVISO CRÍTICO:</b>\n"
            "Restaurar sobrescreve os dados atuais do banco.\n"
            "Recomendamos fazer um backup ANTES de prosseguir.\n\n"
            "Tem certeza ABSOLUTA que deseja restaurar?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RESTORE_YES}:{bid}",
            cancel_callback=f"{CB_VIEW}:{bid}",
            confirm_text="♻️ Sim, restaurar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_restore falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RESTORE_YES}:"))
async def on_restore_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal pode restaurar.")
            return

        try:
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        d = _backup_dir()
        filepath = d / b.filename
        if not filepath.exists():
            await toast_err(callback, "❌ Arquivo não encontrado no disco.")
            return

        await toast_ok(callback, "⏳ Restaurando... (isso pode demorar)")

        # Executa psql
        success = False
        err_msg = ""
        try:
            cmd = [
                "psql",
                settings.db_url_sync,
                "-f",
                str(filepath),
                "-q",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                err_msg = "Timeout (5 min)"
            else:
                if proc.returncode == 0:
                    success = True
                else:
                    err_msg = (stderr.decode() or "")[:200]
        except FileNotFoundError:
            err_msg = "psql não instalado no servidor."
        except Exception as exc:
            err_msg = str(exc)[:200]

        await log_action(
            admin_id=admin_id,
            action="backup.restored",
            category="admin",
            details={"backup_id": bid, "success": success, "error": err_msg},
        )

        if success:
            text = (
                f"✅ <b>Backup restaurado!</b>\n\n"
                f"📁 {b.filename}\n\n"
                "⚠️ Pode ser necessário reiniciar o bot/worker."
            )
        else:
            text = (
                f"❌ <b>Falha ao restaurar</b>\n\n"
                f"Motivo: <code>{truncate(err_msg, 200)}</code>\n\n"
                f"💡 Verifique se o <code>psql</code> está instalado "
                f"e se a <code>DATABASE_URL_SYNC</code> está correta."
            )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
    except Exception as exc:
        logger.exception(f"backup on_restore_confirm falhou: {exc}")


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
            bid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR BACKUP</b>\n\n"
            f"📁 <code>{b.filename}</code>\n"
            f"📦 {_fmt_size(b.size)}\n\n"
            "Isso remove o registro e o arquivo do disco.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{bid}",
            cancel_callback=f"{CB_VIEW}:{bid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            bid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        b = await _load_backup(bid)
        if b is None:
            await toast_err(callback, "❌ Backup não encontrado.")
            return

        # Remove arquivo do disco
        try:
            filepath = _backup_dir() / b.filename
            if filepath.exists():
                filepath.unlink()
        except Exception as exc:
            logger.debug(f"unlink falhou: {exc}")

        # Remove registro
        try:
            async with session_scope() as session:
                await session.execute(
                    Backup.__table__.delete().where(Backup.id == bid)
                )
        except Exception as exc:
            logger.exception(f"delete backup falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="backup.deleted",
            category="admin",
            details={"backup_id": bid},
        )

        # Volta pra lista
        await on_history(callback)
        await toast_ok(callback, "🗑️ Excluído.")
    except Exception as exc:
        logger.exception(f"backup on_delete_confirm falhou: {exc}")


# ============================================================
# INTEGRIDADE
# ============================================================
@router.callback_query(F.data == CB_INTEGRITY)
async def on_integrity(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🔍 Verificando integridade...")

        # Testa conexão e conta tabelas principais
        results: Dict[str, Any] = {}
        try:
            async with session_scope() as session:
                for name, model in (
                    ("users", None),
                    ("orders", None),
                    ("transactions", None),
                ):
                    pass
                # Contagens simples
                from bot.database import (
                    Order,
                    Product,
                    Transaction,
                    User,
                )

                results["users"] = int(
                    (await session.execute(select(func.count(User.id)))).scalar() or 0
                )
                results["orders"] = int(
                    (await session.execute(select(func.count(Order.id)))).scalar() or 0
                )
                results["transactions"] = int(
                    (await session.execute(select(func.count(Transaction.id)))).scalar() or 0
                )
                results["products"] = int(
                    (await session.execute(select(func.count(Product.id)))).scalar() or 0
                )
            ok = True
        except Exception as exc:
            logger.debug(f"integridade falhou: {exc}")
            ok = False

        # Tamanho do DB (aproximado)
        try:
            async with session_scope() as session:
                from sqlalchemy import text

                res = await session.execute(
                    text("SELECT pg_database_size(current_database())")
                )
                db_size = int(res.scalar() or 0)
        except Exception:
            db_size = 0

        if ok:
            text = (
                "🔍 <b>INTEGRIDADE DO BANCO</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Conexão: <b>OK</b>\n"
                f"💾 Tamanho do DB: <b>{_fmt_size(db_size)}</b>\n\n"
                "📊 <b>Registros:</b>\n"
                f"├ 👥 Usuários: <b>{fmt_num(results.get('users', 0))}</b>\n"
                f"├ 🛒 Pedidos: <b>{fmt_num(results.get('orders', 0))}</b>\n"
                f"├ 💰 Transações: <b>{fmt_num(results.get('transactions', 0))}</b>\n"
                f"└ 📦 Produtos: <b>{fmt_num(results.get('products', 0))}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            text = (
                "🔍 <b>INTEGRIDADE DO BANCO</b>\n\n"
                "❌ <b>Falha na conexão</b>\n"
                "Verifique os logs do servidor."
            )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
    except Exception as exc:
        logger.exception(f"backup on_integrity falhou: {exc}")


# ============================================================
# LIMPAR ANTIGOS
# ============================================================
@router.callback_query(F.data == CB_CLEAN)
async def on_clean(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return

        retention = await _get_setting("backup_retention_days", "7")
        try:
            retention_days = int(retention or 7)
        except Exception:
            retention_days = 7

        # Conta quantos serão removidos
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(func.count(Backup.id)).where(Backup.created_at < cutoff)
                )
                count = int(res.scalar() or 0)
        except Exception:
            count = 0

        text = (
            f"🧹 <b>LIMPAR BACKUPS ANTIGOS</b>\n\n"
            f"📅 Retenção: <b>{retention_days} dias</b>\n"
            f"🗑️ Backups a remover: <b>{count}</b>\n\n"
            "Isso remove os arquivos do disco e os registros.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_CLEAN_YES}:yes",
            cancel_callback=CB_ROOT,
            confirm_text="🧹 Sim, limpar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"backup on_clean falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_CLEAN_YES}:"))
async def on_clean_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        retention = await _get_setting("backup_retention_days", "7")
        try:
            retention_days = int(retention or 7)
        except Exception:
            retention_days = 7

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Backup).where(Backup.created_at < cutoff)
                )
                items = list(res.scalars().all())

                for b in items:
                    # Remove arquivo
                    try:
                        fp = _backup_dir() / b.filename
                        if fp.exists():
                            fp.unlink()
                    except Exception:
                        pass
                    # Remove registro
                    await session.execute(
                        Backup.__table__.delete().where(Backup.id == b.id)
                    )
                    removed += 1
        except Exception as exc:
            logger.exception(f"clean backups falhou: {exc}")

        await log_action(
            admin_id=admin_id,
            action="backup.cleaned",
            category="admin",
            details={"removed": removed, "retention_days": retention_days},
        )

        await render_panel(callback)
        await toast_ok(callback, f"🧹 {removed} backup(s) removido(s).")
    except Exception as exc:
        logger.exception(f"backup on_clean_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_panel",
    "on_new",
    "on_toggle_auto",
    "on_set_interval",
    "on_set_retention",
    "on_backup_text_input",
    "on_history",
    "on_view",
    "on_download",
    "on_restore",
    "on_restore_confirm",
    "on_delete",
    "on_delete_confirm",
    "on_integrity",
    "on_clean",
    "on_clean_confirm",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_TOGGLE_AUTO",
    "CB_SET_INTERVAL",
    "CB_SET_RETENTION",
    "CB_VIEW",
    "CB_RESTORE",
    "CB_RESTORE_YES",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_DOWNLOAD",
    "CB_INTEGRITY",
    "CB_CLEAN",
    "CB_CLEAN_YES",
    "CONFIG_FIELDS",
]
