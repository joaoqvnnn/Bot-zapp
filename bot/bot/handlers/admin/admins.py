"""
Larizinha Store — Módulo Admin: Administradores
Implementa gerenciamento completo de admins + roles + permissões.

Telas:
- Lista de admins (com cargo, permissões, último login)
- Adicionar admin (por Telegram ID)
- Editar admin (nome, role, permissões, admin principal)
- Remover admin
- Gerenciar cargos (roles)
- Editor de permissões (por admin)
- Definir admin principal
- Histórico de ações (logs por admin)
- Logins recentes
- Revogar sessões (limpa cache AdminFlow + Redis admin)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Apenas o Admin Principal pode adicionar/remover admins e mexer em permissões.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    Admin,
    Log,
    Role,
    User,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_ACTIVE,
    SYM_OFF,
    SYM_OK,
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
    is_int,
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
router = Router(name="admin_admins")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_admins_list"
MSG_KEY_EMPTY = "admin_admins_empty"
MSG_KEY_DETAIL = "admin_admins_detail"
MSG_KEY_ROLES = "admin_roles_list"
MSG_KEY_PERMS = "admin_perms_editor"
MSG_KEY_LOGS = "admin_admins_logs"

SCREEN_LIST = "admin_admins_list"
SCREEN_DETAIL = "admin_admins_detail"
SCREEN_ROLES = "admin_roles_list"
SCREEN_PERMS = "admin_perms_editor"

# Callbacks base
CB_ROOT = "admin:admins"
CB_PAGE = "admin:admins:page"             # :N
CB_NEW = "admin:admins:new"
CB_VIEW = "admin:admins:view"             # :ID
CB_EDIT_FIELD = "admin:admins:set"        # :field:ID
CB_TOGGLE_MAIN = "admin:admins:main"      # :ID
CB_DELETE = "admin:admins:delete"         # :ID
CB_DELETE_YES = "admin:admins:delete:yes" # :ID
CB_ROLES = "admin:admins:roles"
CB_ROLE_EDIT = "admin:admins:role:edit"   # :ID
CB_ROLE_SET = "admin:admins:role:set"     # :field:ID
CB_ROLE_DELETE = "admin:admins:role:delete"  # :ID
CB_ROLE_DELETE_YES = "admin:admins:role:delete:yes"  # :ID
CB_ROLE_NEW = "admin:admins:role:new"
CB_PERMS = "admin:admins:perms"           # :ID (admin_id)
CB_PERM_ADD = "admin:admins:perms:add"    # :ID
CB_PERM_DEL = "admin:admins:perms:del"    # :ID:perm_slug
CB_PERM_ROLE = "admin:admins:perms:role"  # :ID:role_id
CB_LOGS = "admin:admins:logs"             # :ID
CB_REVOKE = "admin:admins:revoke"         # :ID

# Cargos padrão (criados no boot se não existirem)
DEFAULT_ROLES: List[Dict[str, Any]] = [
    {
        "name": "Admin Principal",
        "description": "Acesso total",
        "permissions": ["*"],
        "is_system": True,
    },
    {
        "name": "Gerente",
        "description": "Sem exclusões críticas",
        "permissions": [
            "dashboard", "products", "categories", "sales", "users",
            "payments", "gifts", "coupons", "promos", "affiliates",
            "messages", "buttons", "stats", "settings",
        ],
        "is_system": True,
    },
    {
        "name": "Financeiro",
        "description": "Só saldo/saques",
        "permissions": ["dashboard", "withdraws", "payments", "finance"],
        "is_system": True,
    },
    {
        "name": "Suporte",
        "description": "Só atendimento",
        "permissions": ["dashboard", "support"],
        "is_system": True,
    },
    {
        "name": "Moderador",
        "description": "Só usuários",
        "permissions": ["dashboard", "users"],
        "is_system": True,
    },
]

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "first_name": {"label": "Nome", "validate": "text", "max": 80},
    "username": {"label": "Username", "validate": "text", "max": 60},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_admin(admin_pk: int) -> Optional[Admin]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Admin).where(Admin.id == admin_pk))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_admin_by_tg(tg_id: int) -> Optional[Admin]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Admin).where(Admin.telegram_id == tg_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_role(role_id: int) -> Optional[Role]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Role).where(Role.id == role_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _list_roles() -> List[Role]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Role).order_by(Role.id.asc()))
            return list(res.scalars().all())
    except Exception:
        return []


async def _ensure_default_roles() -> None:
    """Garante que os cargos padrão existam no banco."""
    try:
        async with session_scope() as session:
            for spec in DEFAULT_ROLES:
                r = await session.execute(
                    select(Role).where(Role.name == spec["name"])
                )
                existing = r.scalar_one_or_none()
                if existing is None:
                    session.add(
                        Role(
                            name=spec["name"],
                            description=spec["description"],
                            permissions=spec["permissions"],
                            is_system=spec["is_system"],
                        )
                    )
    except Exception as exc:
        logger.debug(f"_ensure_default_roles falhou: {exc}")


async def _count_admins() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Admin.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_admins(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Admin]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Admin)
                .order_by(Admin.is_main_admin.desc(), Admin.id.asc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


def _is_main(admin_id: int) -> bool:
    return admin_id == settings.MAIN_ADMIN_ID


def _perms_to_list(perms: Any) -> List[str]:
    if isinstance(perms, list):
        return [str(p) for p in perms]
    if isinstance(perms, dict):
        return [str(k) for k, v in perms.items() if v]
    return []


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    await _ensure_default_roles()

    total = await _count_admins()
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    admins = await _list_admins(page)

    # Nomes de roles em cache
    roles_map: Dict[int, str] = {}
    for r in await _list_roles():
        roles_map[int(r.id)] = r.name

    if not admins:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "👑 <b>ADMINISTRADORES</b>\n\n"
                "<i>Nenhum administrador cadastrado.</i>"
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for a in admins:
            cargo = "👑 Principal" if a.is_main_admin else (
                roles_map.get(int(a.role_id or 0), "—")
            )
            perms_list = _perms_to_list(a.permissions)
            perms_txt = "*" if "*" in perms_list else f"{len(perms_list)}"
            rows.append(
                [
                    str(a.id),
                    truncate(a.first_name or "—", 16),
                    cargo,
                    perms_txt,
                    fmt_dt(a.last_login_at, with_time=False),
                ]
            )

        lista = table(rows, ["ID", "Nome", "Cargo", "P#", "Último login"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"👑 <b>ADMINISTRADORES</b> ({total})\n\n"
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
                InlineKeyboardButton(text="➕ Adicionar Admin", callback_data=CB_NEW),
                InlineKeyboardButton(text="👑 Cargos", callback_data=CB_ROLES),
            ],
        ]

    for a in admins:
        emoji = "👑" if a.is_main_admin else "🛡️"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{a.id} {truncate(a.first_name or '—', 18)}",
                    callback_data=f"{CB_VIEW}:{a.id}",
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

    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=kb(rows_kb),
    )


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
        logger.exception(f"admins on_root falhou: {exc}")


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
        logger.exception(f"admins on_page falhou: {exc}")


# ============================================================
# ADICIONAR ADMIN
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        # Só Admin Principal
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal pode adicionar admins.")
            return

        prompt = (
            "➕ <b>NOVO ADMIN</b>\n\n"
            "Envie o <b>Telegram ID</b> do novo admin (numérico).\n\n"
            "Exemplo: <code>123456789</code>\n\n"
            "⚠️ O usuário precisa ter aberto o bot ao menos uma vez.\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_admin_tg",
            screen="admins:new",
            use_force_reply=True,
            reply_placeholder="Telegram ID",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_new falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_admins_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # ---------- Novo admin ----------
    if awaiting == "new_admin_tg":
        value, _extra = await consume_input(message, admin_id, expected_field="new_admin_tg")
        if value is None:
            return
        await _create_admin(message, admin_id, value)
        return

    # ---------- Editar campos ----------
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        pk = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, field, pk, value)
        return

    # ---------- Permissões (adicionar) ----------
    if awaiting.startswith("perm_add:"):
        pk = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _add_permission(message, admin_id, pk, value)
        return

    # ---------- Novo cargo ----------
    if awaiting == "new_role_name":
        value, _extra = await consume_input(message, admin_id, expected_field="new_role_name")
        if value is None:
            return
        await _create_role(message, admin_id, value)
        return

    # ---------- Editar cargo ----------
    if awaiting.startswith("role_set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        rid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_role_field(message, admin_id, field, rid, value)
        return


async def _create_admin(message: Message, admin_id: int, raw: str) -> None:
    raw = (raw or "").strip()
    if not is_int(raw):
        await message.answer("❌ ID inválido.")
        return

    tg_id = int(raw)
    if tg_id <= 0:
        await message.answer("❌ ID inválido.")
        return

    # Já existe?
    existing = await _load_admin_by_tg(tg_id)
    if existing is not None:
        await message.answer("❌ Este admin já está cadastrado.")
        return

    try:
        async with session_scope() as session:
            a = Admin(
                telegram_id=tg_id,
                is_main_admin=False,
                permissions=[],
            )
            session.add(a)
            await session.flush()
            pk = int(a.id)
    except Exception as exc:
        logger.exception(f"_create_admin falhou: {exc}")
        await message.answer("❌ Erro ao criar admin.")
        return

    await log_action(
        admin_id=admin_id,
        action="admin.created",
        category="admin",
        details={"new_admin_tg": tg_id, "admin_pk": pk},
    )

    await message.answer(
        f"✅ <b>Admin adicionado!</b>\n\n"
        f"ID interno: <code>#{pk}</code>\n"
        f"Telegram: <code>{tg_id}</code>\n\n"
        f"Use os botões para configurar cargo e permissões.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="🛡️ Ver Admin",
                        callback_data=f"{CB_VIEW}:{pk}",
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


# ============================================================
# DETALHE DO ADMIN
# ============================================================
async def _render_admin_detail(a: Admin) -> Tuple[str, InlineKeyboardMarkup]:
    role_name = "—"
    if a.role_id:
        r = await _load_role(int(a.role_id))
        if r is not None:
            role_name = r.name

    perms_list = _perms_to_list(a.permissions)
    if a.is_main_admin and "*" not in perms_list:
        perms_list = ["*"]
    perms_txt = ", ".join(perms_list[:15]) if perms_list else "—"

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"👑 <b>ADMIN #{a.id}</b>\n"
            f"├ Telegram: <code>{a.telegram_id}</code>\n"
            f"├ Nome: {a.first_name or '—'}\n"
            f"├ Username: @{a.username or '—'}\n"
            f"├ Principal: {humanize_bool(a.is_main_admin)}\n"
            f"├ Cargo: {role_name}\n"
            f"├ Último login: {fmt_dt(a.last_login_at)}\n"
            f"├ Criado em: {fmt_dt(a.created_at)}\n"
            f"└ Permissões ({len(perms_list)}): <code>{perms_txt}</code>"
        ),
        id=a.id,
        telegram_id=a.telegram_id,
        nome=a.first_name or "—",
        username=a.username or "—",
        principal=humanize_bool(a.is_main_admin),
        cargo=role_name,
        login=fmt_dt(a.last_login_at),
        created=fmt_dt(a.created_at),
        perms=perms_txt,
        total=len(perms_list),
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(int(a.id), bool(a.is_main_admin)))

    return rendered.content, keyboard


def _default_detail_keyboard(pk: int, is_main: bool) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    rows: List[List[InlineKeyboardButton]] = [
        [b("✏️ Nome", f"{CB_EDIT_FIELD}:first_name:{pk}"), b("✏️ Username", f"{CB_EDIT_FIELD}:username:{pk}")],
        [b("🔐 Permissões", f"{CB_PERMS}:{pk}")],
        [b("👑 Cargos", f"{CB_ROLES}")],
    ]

    if not is_main:
        rows.append([b("👑 Tornar Principal", f"{CB_TOGGLE_MAIN}:{pk}")])
    else:
        rows.append([b("↩️ Remover Principal", f"{CB_TOGGLE_MAIN}:{pk}")])

    rows.extend([
        [b("📜 Histórico", f"{CB_LOGS}:{pk}")],
        [b("❌ Revogar Sessões", f"{CB_REVOKE}:{pk}")],
        [b("🗑️ Remover", f"{CB_DELETE}:{pk}")],
        [b("🔙 Voltar", CB_ROOT)],
    ])

    return rows


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pk = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        a = await _load_admin(pk)
        if a is None:
            await toast_err(callback, "❌ Admin não encontrado.")
            return
        text, keyboard = await _render_admin_detail(a)
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_view falhou: {exc}")


# ============================================================
# EDITAR CAMPOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EDIT_FIELD}:"))
async def on_edit_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:admins:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        pk = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{pk}",
            screen=f"admins:edit:{pk}",
            extra={"admin_pk": pk, "field": field},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_edit_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    field: str,
    pk: int,
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
    if len(val) > meta.get("max", 200):
        await message.answer("❌ Texto muito longo.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Admin).where(Admin.id == pk))
            a: Optional[Admin] = r.scalar_one_or_none()
            if a is None:
                await message.answer("❌ Admin não encontrado.")
                return
            setattr(a, field, val)
            session.add(a)
    except Exception as exc:
        logger.exception(f"_apply_field_value (admin) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"admin.set.{field}",
        category="admin",
        details={"admin_pk": pk, "value": val},
    )

    a2 = await _load_admin(pk)
    if a2 is not None:
        text, keyboard = await _render_admin_detail(a2)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE MAIN ADMIN
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE_MAIN}:"))
async def on_toggle_main(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            pk = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Admin).where(Admin.id == pk))
                a: Optional[Admin] = r.scalar_one_or_none()
                if a is None:
                    await toast_err(callback, "❌ Admin não encontrado.")
                    return
                a.is_main_admin = not bool(a.is_main_admin)
                if a.is_main_admin:
                    a.permissions = ["*"]
                session.add(a)
                new_val = bool(a.is_main_admin)
        except Exception as exc:
            logger.exception(f"toggle main falhou: {exc}")
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action="admin.toggle_main",
            category="admin",
            details={"admin_pk": pk, "value": new_val},
        )

        a2 = await _load_admin(pk)
        if a2 is not None:
            text, keyboard = await _render_admin_detail(a2)
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, "👑 Cargo alterado.")
    except Exception as exc:
        logger.exception(f"admins on_toggle_main falhou: {exc}")


# ============================================================
# PERMISSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PERMS}:"))
async def on_perms(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        # Ignora callbacks de add/del
        parts = callback.data.split(":")
        if len(parts) >= 4 and parts[3] in ("add", "del", "role"):
            return

        pk = int(parts[-1])
        a = await _load_admin(pk)
        if a is None:
            await toast_err(callback, "❌ Admin não encontrado.")
            return

        perms_list = _perms_to_list(a.permissions)
        if a.is_main_admin and "*" not in perms_list:
            perms_list = ["*"]

        # Permissões disponíveis (módulos)
        available_modules = [
            "dashboard", "products", "categories", "sales", "users",
            "admins", "wallet", "payments", "withdraws", "gifts",
            "coupons", "promos", "channel", "affiliates", "messages",
            "buttons", "credentials", "stats", "settings", "blacklist",
            "backup", "broadcast", "automations", "tests", "logs",
            "locks", "tickets", "games", "security", "alerts", "finance",
            "monitoring", "database", "api", "permissions", "tools",
        ]

        text = (
            f"🔐 <b>PERMISSÕES · #{a.id}</b>\n\n"
            f"Ativas ({len(perms_list)}):\n"
            f"<code>{', '.join(perms_list[:20]) if perms_list else '—'}</code>\n\n"
            "Toque em um módulo abaixo para <b>ativar/desativar</b>:"
        )

        rows: List[List[InlineKeyboardButton]] = []

        # Botões de toggle (2 por linha)
        buf: List[InlineKeyboardButton] = []
        for mod in available_modules:
            checked = "*" in perms_list or mod in perms_list
            label = f"{'✅' if checked else '☑️'} {mod}"
            buf.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CB_PERM_DEL if checked else CB_PERM_ADD}:{pk}:{mod}",
                )
            )
            if len(buf) == 2:
                rows.append(buf)
                buf = []
        if buf:
            rows.append(buf)

        # Ações especiais
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔓 Aplicar role",
                    callback_data=f"{CB_PERM_ROLE}:{pk}:0",
                ),
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=f"{CB_VIEW}:{pk}",
                ),
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_perms falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PERM_ADD}:"))
async def on_perm_add(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        pk = int(parts[3])
        mod = parts[4]

        try:
            async with session_scope() as session:
                r = await session.execute(select(Admin).where(Admin.id == pk))
                a: Optional[Admin] = r.scalar_one_or_none()
                if a is None:
                    await toast_err(callback, "❌ Admin não encontrado.")
                    return
                perms = _perms_to_list(a.permissions)
                if mod not in perms:
                    perms.append(mod)
                a.permissions = perms
                session.add(a)
        except Exception as exc:
            logger.exception(f"perm add falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="admin.perm.add",
            category="admin",
            details={"admin_pk": pk, "permission": mod},
        )

        # Re-render
        callback.data = f"{CB_PERMS}:{pk}"  # type: ignore[misc]
        await on_perms(callback)
    except Exception as exc:
        logger.exception(f"admins on_perm_add falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PERM_DEL}:"))
async def on_perm_del(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        pk = int(parts[3])
        mod = parts[4]

        try:
            async with session_scope() as session:
                r = await session.execute(select(Admin).where(Admin.id == pk))
                a: Optional[Admin] = r.scalar_one_or_none()
                if a is None:
                    await toast_err(callback, "❌ Admin não encontrado.")
                    return
                perms = [p for p in _perms_to_list(a.permissions) if p != mod]
                a.permissions = perms
                session.add(a)
        except Exception as exc:
            logger.exception(f"perm del falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="admin.perm.del",
            category="admin",
            details={"admin_pk": pk, "permission": mod},
        )

        callback.data = f"{CB_PERMS}:{pk}"  # type: ignore[misc]
        await on_perms(callback)
    except Exception as exc:
        logger.exception(f"admins on_perm_del falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PERM_ROLE}:"))
async def on_perm_role(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:admins:perms:role:ID:ROLEID
        pk = int(parts[4])
        role_id = int(parts[5]) if len(parts) > 5 else 0

        if role_id <= 0:
            # Mostra a lista de roles
            roles = await _list_roles()
            if not roles:
                await toast_err(callback, "❌ Nenhum cargo cadastrado.")
                return

            rows = []
            for r in roles:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"👑 {r.name}",
                            callback_data=f"{CB_PERM_ROLE}:{pk}:{r.id}",
                        )
                    ]
                )
            rows.append(
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_PERMS}:{pk}")]
            )
            await edit_or_send(
                callback,
                text="🔐 <b>APLICAR CARGO</b>\n\nEscolha um cargo para aplicar as permissões:",
                reply_markup=kb(rows),
            )
            await answer(callback)
            return

        # Aplica as permissões do role no admin
        role = await _load_role(role_id)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        role_perms = _perms_to_list(role.permissions)

        try:
            async with session_scope() as session:
                r = await session.execute(select(Admin).where(Admin.id == pk))
                a: Optional[Admin] = r.scalar_one_or_none()
                if a is None:
                    await toast_err(callback, "❌ Admin não encontrado.")
                    return
                a.role_id = role_id
                a.permissions = role_perms
                session.add(a)
        except Exception as exc:
            logger.exception(f"apply role falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="admin.role.applied",
            category="admin",
            details={"admin_pk": pk, "role_id": role_id},
        )

        callback.data = f"{CB_VIEW}:{pk}"  # type: ignore[misc]
        await on_view(callback)
        await toast_ok(callback, "✅ Cargo aplicado.")
    except Exception as exc:
        logger.exception(f"admins on_perm_role falhou: {exc}")


# ============================================================
# CARGOS (ROLES)
# ============================================================
@router.callback_query(F.data == CB_ROLES)
async def on_roles(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await _ensure_default_roles()
        roles = await _list_roles()

        if not roles:
            text = "👑 <b>CARGOS</b>\n\n<i>Nenhum cargo cadastrado.</i>"
        else:
            lines = ["👑 <b>CARGOS DISPONÍVEIS</b>", ""]
            for r in roles:
                perms = _perms_to_list(r.permissions)
                perms_txt = "*" if "*" in perms else f"{len(perms)} perms"
                lines.append(
                    f"👑 <b>{r.name}</b> · <i>{r.description or '—'}</i>\n"
                    f"    <code>{perms_txt}</code>"
                )
            text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="➕ Novo Cargo", callback_data=CB_ROLE_NEW),
            ],
        ]

        for r in roles:
            prefix = "👑" if r.is_system else "🏷️"
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{prefix} {truncate(r.name, 22)}",
                        callback_data=f"{CB_ROLE_EDIT}:{r.id}",
                    )
                ]
            )

        rows_kb.append(build_back_row(CB_ROOT))

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_roles falhou: {exc}")


@router.callback_query(F.data == CB_ROLE_NEW)
async def on_role_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        prompt = (
            "➕ <b>NOVO CARGO</b>\n\n"
            "Envie o nome do novo cargo.\n"
            "As permissões poderão ser definidas depois.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_role_name",
            screen="roles:new",
            use_force_reply=True,
            reply_placeholder="Nome do cargo",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_role_new falhou: {exc}")


async def _create_role(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        await message.answer("❌ Nome inválido.")
        return
    if len(name) > 40:
        name = name[:40]

    try:
        async with session_scope() as session:
            r = await session.execute(select(Role).where(Role.name == name))
            if r.scalar_one_or_none() is not None:
                await message.answer("❌ Já existe um cargo com este nome.")
                return
            role = Role(
                name=name,
                description="",
                permissions=[],
                is_system=False,
            )
            session.add(role)
            await session.flush()
            rid = int(role.id)
    except Exception as exc:
        logger.exception(f"_create_role falhou: {exc}")
        await message.answer("❌ Erro ao criar cargo.")
        return

    await log_action(
        admin_id=admin_id,
        action="role.created",
        category="admin",
        details={"role_id": rid, "name": name},
    )

    await message.answer(
        f"✅ Cargo <b>{name}</b> criado (#{rid}).",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [
                    InlineKeyboardButton(
                        text="👑 Ver Cargos",
                        callback_data=CB_ROLES,
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith(f"{CB_ROLE_EDIT}:"))
async def on_role_edit(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            rid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        role = await _load_role(rid)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        perms = _perms_to_list(role.permissions)
        perms_txt = ", ".join(perms[:15]) if perms else "—"

        text = (
            f"👑 <b>CARGO #{role.id}</b>\n\n"
            f"├ Nome: {role.name}\n"
            f"├ Descrição: {role.description or '—'}\n"
            f"├ Sistema: {humanize_bool(role.is_system)}\n"
            f"└ Permissões ({len(perms)}): <code>{perms_txt}</code>"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="✏️ Nome", callback_data=f"{CB_ROLE_SET}:name:{rid}"
                ),
                InlineKeyboardButton(
                    text="✏️ Descrição", callback_data=f"{CB_ROLE_SET}:description:{rid}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Permissões (JSON)",
                    callback_data=f"{CB_ROLE_SET}:permissions:{rid}",
                )
            ],
        ]

        if not role.is_system:
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="🗑️ Excluir",
                        callback_data=f"{CB_ROLE_DELETE}:{rid}",
                    )
                ]
            )

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROLES)]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_role_edit falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_ROLE_SET}:"))
async def on_role_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:admins:role:set:field:ID
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[4]
        rid = int(parts[5])

        if field not in ("name", "description", "permissions"):
            await toast_err(callback, "❌ Campo inválido.")
            return

        if field == "permissions":
            prompt = (
                "✏️ <b>PERMISSÕES (JSON)</b>\n\n"
                "Envie a lista em formato JSON:\n"
                '<code>["dashboard", "users", "sales"]</code>\n'
                'ou <code>["*"]</code> para acesso total.\n\n'
                "⚠️ A mensagem será apagada automaticamente."
            )
        else:
            prompt = (
                f"✏️ <b>EDITAR {field.upper()}</b>\n\n"
                "Envie o novo valor.\n\n"
                "⚠️ A mensagem será apagada automaticamente."
            )
        await ask_input(
            callback,
            prompt,
            field=f"role_set:{field}:{rid}",
            screen=f"roles:edit:{rid}",
            extra={"role_id": rid, "field": field},
            use_force_reply=True,
            reply_placeholder=field,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_role_set falhou: {exc}")


async def _apply_role_field(
    message: Message,
    admin_id: int,
    field: str,
    rid: int,
    value: str,
) -> None:
    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if field == "permissions":
        try:
            import json as _json

            data = _json.loads(val)
            if not isinstance(data, list):
                raise ValueError("not a list")
            parsed = [str(x) for x in data]
        except Exception:
            await message.answer("❌ JSON inválido.")
            return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Role).where(Role.id == rid))
            role: Optional[Role] = r.scalar_one_or_none()
            if role is None:
                await message.answer("❌ Cargo não encontrado.")
                return
            setattr(role, field, parsed)
            session.add(role)
    except Exception as exc:
        logger.exception(f"_apply_role_field falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"role.set.{field}",
        category="admin",
        details={"role_id": rid},
    )

    await message.answer("✅ Cargo atualizado.")


@router.callback_query(F.data.startswith(f"{CB_ROLE_DELETE}:"))
async def on_role_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        if len(parts) >= 6 and parts[5] == "yes":
            return

        rid = int(parts[-1])
        role = await _load_role(rid)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        if role.is_system:
            await toast_err(callback, "❌ Cargos do sistema não podem ser excluídos.")
            return

        text = (
            f"🗑️ <b>EXCLUIR CARGO</b>\n\n"
            f"Cargo: <b>{role.name}</b> (#{role.id})\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_ROLE_DELETE_YES}:{rid}",
            cancel_callback=f"{CB_ROLE_EDIT}:{rid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_role_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_ROLE_DELETE_YES}:"))
async def on_role_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            rid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Desvincula admins
                from sqlalchemy import update as sa_update

                await session.execute(
                    sa_update(Admin)
                    .where(Admin.role_id == rid)
                    .values(role_id=None)
                )
                await session.execute(
                    Role.__table__.delete().where(Role.id == rid)
                )
        except Exception as exc:
            logger.exception(f"delete role falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="role.deleted",
            category="admin",
            details={"role_id": rid},
        )

        callback.data = CB_ROLES  # type: ignore[misc]
        await on_roles(callback)
        await toast_ok(callback, "🗑️ Cargo excluído.")
    except Exception as exc:
        logger.exception(f"admins on_role_delete_confirm falhou: {exc}")


# ============================================================
# HISTÓRICO / LOGS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_LOGS}:"))
async def on_logs(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pk = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        a = await _load_admin(pk)
        if a is None:
            await toast_err(callback, "❌ Admin não encontrado.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Log)
                    .where(Log.admin_id == a.telegram_id)
                    .order_by(Log.created_at.desc())
                    .limit(30)
                )
                logs = list(res.scalars().all())
        except Exception:
            logs = []

        if not logs:
            text = f"📜 <i>Nenhuma ação registrada para #{a.id}.</i>"
        else:
            lines = [f"📜 <b>HISTÓRICO · ADMIN #{a.id}</b>", ""]
            for log in logs:
                lines.append(
                    f"<code>{log.action}</code> · {fmt_dt(log.created_at)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{pk}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_logs falhou: {exc}")


# ============================================================
# REVOGAR SESSÕES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REVOKE}:"))
async def on_revoke(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            pk = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        a = await _load_admin(pk)
        if a is None:
            await toast_err(callback, "❌ Admin não encontrado.")
            return

        # Limpa o AdminFlow desse admin
        try:
            await AdminFlow.clear(int(a.telegram_id))
        except Exception:
            pass

        # Limpa chaves Redis do admin
        try:
            from bot.redis_client import get_redis

            r = await get_redis()
            pattern = f"{settings.REDIS_PREFIX}:admin_flow:{a.telegram_id}"
            await r.delete(pattern)
        except Exception:
            pass

        await log_action(
            admin_id=admin_id,
            action="admin.revoke_sessions",
            category="admin",
            details={"admin_pk": pk, "admin_tg": a.telegram_id},
        )

        await toast_ok(callback, "❌ Sessões revogadas.")
    except Exception as exc:
        logger.exception(f"admins on_revoke falhou: {exc}")


# ============================================================
# REMOVER ADMIN
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return

        pk = int(parts[-1])
        a = await _load_admin(pk)
        if a is None:
            await toast_err(callback, "❌ Admin não encontrado.")
            return

        if a.is_main_admin or int(a.telegram_id) == settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Não é possível remover o Admin Principal.")
            return

        text = (
            f"🗑️ <b>REMOVER ADMIN</b>\n\n"
            f"Admin: <b>{a.first_name or '—'}</b> (TG <code>{a.telegram_id}</code>)\n\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{pk}",
            cancel_callback=f"{CB_VIEW}:{pk}",
            confirm_text="🗑️ Sim, remover",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"admins on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if not _is_main(admin_id):
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            pk = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                a = await session.execute(select(Admin).where(Admin.id == pk))
                existing = a.scalar_one_or_none()
                if existing is None:
                    await toast_err(callback, "❌ Admin não encontrado.")
                    return
                if existing.is_main_admin:
                    await toast_err(callback, "❌ Não é possível remover o Admin Principal.")
                    return
                await session.execute(
                    Admin.__table__.delete().where(Admin.id == pk)
                )
        except Exception as exc:
            logger.exception(f"delete admin falhou: {exc}")
            await toast_err(callback, "❌ Erro ao remover.")
            return

        await log_action(
            admin_id=admin_id,
            action="admin.deleted",
            category="admin",
            details={"admin_pk": pk},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "🗑️ Admin removido.")
    except Exception as exc:
        logger.exception(f"admins on_delete_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_new",
    "on_admins_text_input",
    "on_view",
    "on_edit_field",
    "on_toggle_main",
    "on_perms",
    "on_perm_add",
    "on_perm_del",
    "on_perm_role",
    "on_roles",
    "on_role_new",
    "on_role_edit",
    "on_role_set",
    "on_role_delete",
    "on_role_delete_confirm",
    "on_logs",
    "on_revoke",
    "on_delete",
    "on_delete_confirm",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_VIEW",
    "CB_EDIT_FIELD",
    "CB_TOGGLE_MAIN",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_ROLES",
    "CB_ROLE_EDIT",
    "CB_ROLE_SET",
    "CB_ROLE_DELETE",
    "CB_ROLE_DELETE_YES",
    "CB_ROLE_NEW",
    "CB_PERMS",
    "CB_PERM_ADD",
    "CB_PERM_DEL",
    "CB_PERM_ROLE",
    "CB_LOGS",
    "CB_REVOKE",
    "TEXT_FIELDS",
    "DEFAULT_ROLES",
]
