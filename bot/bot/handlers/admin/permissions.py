"""
Larizinha Store — Módulo Admin: Sistema de Permissões
Implementa o editor visual completo de roles + permissões conforme DIM.

Telas:
- Painel de permissões (lista de roles)
- Editor de um role (nome, descrição, is_system)
- Editor de permissões (módulos, leitura, criação, edição, exclusão,
  financeiras, saques, saldo, aprovações, restrições)
- Adicionar/remover permissões individualmente
- Aplicar role em massa a vários admins
- Ver admins com este role
- Remover role (só roles não-system)
- Criar novo role
- Histórico de alterações de permissões

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel.
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Apenas o Admin Principal pode editar permissões.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
from bot.database import Admin, Log, Role, session_scope
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
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_permissions")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_permissions_panel"
MSG_KEY_EDITOR = "admin_permissions_editor"
MSG_KEY_MODULES = "admin_permissions_modules"
MSG_KEY_APPLY = "admin_permissions_apply"
MSG_KEY_ADMINS = "admin_permissions_admins"

SCREEN_PANEL = "admin_permissions_panel"
SCREEN_EDITOR = "admin_permissions_editor"

# Callbacks base
CB_ROOT = "admin:permissions"
CB_RELOAD = "admin:permissions:reload"
CB_NEW = "admin:permissions:new"
CB_VIEW = "admin:permissions:view"              # :ID
CB_SET = "admin:permissions:set"                # :field:ID
CB_PERM_ADD = "admin:permissions:padd"          # :ID:permission
CB_PERM_DEL = "admin:permissions:pdel"          # :ID:permission
CB_PERM_MODULES = "admin:permissions:pmods"     # :ID:page:N
CB_DELETE = "admin:permissions:delete"          # :ID
CB_DELETE_YES = "admin:permissions:delete:yes"  # :ID
CB_APPLY = "admin:permissions:apply"            # :ID
CB_APPLY_TO = "admin:permissions:applyto"       # :ID
CB_APPLY_EXEC = "admin:permissions:applyexec"   # :RID:TGID
CB_ADMINS = "admin:permissions:admins"          # :ID:page:N
CB_HISTORY = "admin:permissions:history"        # :page:N

# Permissões base conhecidas (módulos)
MODULES: List[str] = [
    "dashboard",
    "products",
    "categories",
    "sales",
    "users",
    "admins",
    "wallet",
    "payments",
    "withdraws",
    "gifts",
    "coupons",
    "promos",
    "channel",
    "affiliates",
    "messages",
    "buttons",
    "credentials",
    "stats",
    "settings",
    "blacklist",
    "backup",
    "broadcast",
    "automations",
    "tests",
    "logs",
    "locks",
    "tickets",
    "games",
    "security",
    "alerts",
    "finance",
    "monitoring",
    "database",
    "api",
    "permissions",
    "tools",
]

# Permissões especiais (fora dos módulos)
SPECIAL_PERMS: Dict[str, str] = {
    "read": "👁️ Leitura",
    "create": "➕ Criação",
    "edit": "✏️ Edição",
    "delete": "🗑️ Exclusão",
    "finance": "💰 Financeiras",
    "withdraw": "💸 Saques",
    "balance": "💵 Saldo",
    "approve": "⚠️ Aprovações",
    "restrict": "🚫 Restrições",
    "*": "👑 Acesso Total",
}

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {"label": "Nome do cargo", "validate": "text", "max": 60},
    "description": {"label": "Descrição", "validate": "text", "max": 300},
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_role(rid: int) -> Optional[Role]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Role).where(Role.id == rid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_roles() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Role.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_roles() -> List[Role]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Role).order_by(Role.is_system.desc(), Role.name.asc())
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_admins_in_role(rid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Admin.id)).where(Admin.role_id == rid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


def _perms_to_list(perms: Any) -> List[str]:
    if isinstance(perms, list):
        return [str(p) for p in perms]
    if isinstance(perms, dict):
        return [str(k) for k, v in perms.items() if v]
    return []


def _validate_perms(perms: List[str]) -> List[str]:
    # Remove duplicatas preservando ordem, garante "*" sozinho
    out: List[str] = []
    seen = set()
    for p in perms:
        p = str(p).strip().lower()
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    if "*" in out:
        return ["*"]
    return out


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    roles = await _list_roles()

    if not roles:
        rendered = await message_engine.render(
            MSG_KEY_PANEL,
            fallback=(
                "🔐 <b>SISTEMA DE PERMISSÕES</b>\n\n"
                "<i>Nenhum cargo cadastrado.</i>\n\n"
                "Use ➕ Novo Cargo para criar."
            ),
            mods="—",
            leitura="—",
            criacao="—",
            edicao="—",
            exclusao="—",
            fin="—",
            saq="—",
            saldo="—",
            aprov="—",
            restr="—",
        )
    else:
        rows: List[Sequence[str]] = []
        for r in roles:
            perms = _perms_to_list(r.permissions)
            perms_txt = "*" if "*" in perms else f"{len(perms)}"
            admin_count = await _count_admins_in_role(int(r.id))
            sys_em = "🔒" if r.is_system else "🔓"
            rows.append(
                [
                    str(r.id),
                    sys_em,
                    truncate(r.name or "—", 22),
                    perms_txt,
                    str(admin_count),
                ]
            )

        lista = table(rows, ["ID", "", "Cargo", "Perms", "Admins"])
        rendered = await message_engine.render(
            MSG_KEY_PANEL,
            fallback=(
                f"🔐 <b>SISTEMA DE PERMISSÕES</b>\n\n"
                f"<code>{lista}</code>"
            ),
            total=len(roles),
            lista=lista,
        )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="➕ Novo Cargo", callback_data=CB_NEW),
            ],
        ]

        for r in roles:
            em = "🔒" if r.is_system else "🔓"
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"{em} #{r.id} {truncate(r.name or '—', 22)}",
                        callback_data=f"{CB_VIEW}:{r.id}",
                    )
                ]
            )

        rows_kb.append(
            [
                InlineKeyboardButton(text="🔄 Recarregar", callback_data=CB_RELOAD),
                InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data.in_({CB_ROOT, CB_RELOAD}))
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_panel(callback)
        if callback.data == CB_RELOAD:
            await toast_ok(callback, "🔄 Atualizado!")
        else:
            await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_root falhou: {exc}")


# ============================================================
# CRIAR CARGO
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        prompt = (
            "➕ <b>NOVO CARGO</b>\n\n"
            "Envie o <b>nome</b> do novo cargo.\n"
            "Ex.: <code>Moderador</code>, <code>Financeiro</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="new_role_name",
            screen="permissions:new",
            use_force_reply=True,
            reply_placeholder="Nome do cargo",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_new falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_permissions_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Novo cargo
    if awaiting == "new_role_name":
        value, _extra = await consume_input(message, admin_id, expected_field="new_role_name")
        if value is None:
            return
        await _create_role(message, admin_id, value)
        return

    # Editar campo
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        rid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field(message, admin_id, rid, field, value)
        return

    # Adicionar permissão customizada
    if awaiting.startswith("perm_custom:"):
        rid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _add_custom_permission(message, admin_id, rid, value)
        return

    # Aplicar role em massa (lista de IDs de admin)
    if awaiting.startswith("applyto:"):
        rid = int(awaiting.split(":", 1)[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_role_batch(message, admin_id, rid, value)
        return


async def _create_role(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 60:
        await message.answer("❌ Nome inválido.")
        return

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
                [InlineKeyboardButton(text="🔐 Editar Permissões", callback_data=f"{CB_VIEW}:{rid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE / EDITOR
# ============================================================
async def _render_editor(rid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    r = await _load_role(rid)
    if r is None:
        return None, None

    perms = _perms_to_list(r.permissions)
    admin_count = await _count_admins_in_role(rid)

    perms_txt = (
        "*" if "*" in perms else (", ".join(perms[:20]) or "—")
    )

    sys_em = "🔒 Sistema" if r.is_system else "🔓 Custom"

    rendered = await message_engine.render(
        MSG_KEY_EDITOR,
        fallback=(
            f"🔐 <b>CARGO #{r.id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Nome: <b>{r.name}</b>\n"
            f"├ Descrição: {truncate(r.description or '—', 60)}\n"
            f"├ Tipo: {sys_em}\n"
            f"├ Admins com este cargo: <b>{admin_count}</b>\n"
            f"└ Permissões ({len(perms)}):\n"
            f"    <code>{truncate(perms_txt, 300)}</code>"
        ),
        id=r.id,
        nome=r.name or "—",
        descricao=truncate(r.description or "—", 60),
        system=sys_em,
        admins=admin_count,
        total=len(perms),
        mods="—",
        leitura="✅" if "read" in perms or "*" in perms else "—",
        criacao="✅" if "create" in perms or "*" in perms else "—",
        edicao="✅" if "edit" in perms or "*" in perms else "—",
        exclusao="✅" if "delete" in perms or "*" in perms else "—",
        fin="✅" if "finance" in perms or "*" in perms else "—",
        saq="✅" if "withdraw" in perms or "*" in perms else "—",
        saldo="✅" if "balance" in perms or "*" in perms else "—",
        aprov="✅" if "approve" in perms or "*" in perms else "—",
        restr="—",
        perms=truncate(perms_txt, 300),
    )

    engine_kb = await button_engine.inline(SCREEN_EDITOR)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_editor_keyboard(r))

    return rendered.content, keyboard


def _default_editor_keyboard(r: Role) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    perms = _perms_to_list(r.permissions)
    has_wildcard = "*" in perms

    rows: List[List[InlineKeyboardButton]] = [
        [b("✏️ Nome", f"{CB_SET}:name:{r.id}")],
        [b("✏️ Descrição", f"{CB_SET}:description:{r.id}")],
    ]

    # Toggle wildcard (Acesso Total)
    wildcard_label = "🔴 Remover Acesso Total" if has_wildcard else "👑 Dar Acesso Total"
    wildcard_cb = (
        f"{CB_PERM_DEL}:{r.id}:*" if has_wildcard else f"{CB_PERM_ADD}:{r.id}:*"
    )
    rows.append([b(wildcard_label, wildcard_cb)])

    # Módulos
    rows.append([b("📦 Módulos", f"{CB_PERM_MODULES}:{r.id}:page:1")])

    # Permissões especiais
    rows.append([b("🔐 Permissões Especiais", f"admin:permissions:special:{r.id}")])

    # Aplicar cargo em massa
    rows.append([b("📥 Aplicar em Massa", f"{CB_APPLY}:{r.id}")])

    # Ver admins com este cargo
    rows.append([b("👤 Admins com Cargo", f"{CB_ADMINS}:{r.id}:page:1")])

    # Excluir (só se não for sistema)
    if not r.is_system:
        rows.append([b("🗑️ Excluir Cargo", f"{CB_DELETE}:{r.id}")])

    rows.append([b("🔙 Voltar", CB_ROOT)])

    return rows


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
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
        text, keyboard = await _render_editor(rid)
        if text is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_view falhou: {exc}")


# ============================================================
# EDITAR CAMPOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:permissions:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        rid = int(parts[4])

        meta = TEXT_FIELDS.get(field)
        if meta is None:
            await toast_err(callback, "❌ Campo inválido.")
            return

        label = meta["label"]
        prompt = (
            f"✏️ <b>EDITAR · {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{rid}",
            screen=f"permissions:edit:{rid}",
            extra={"role_id": rid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_set_field falhou: {exc}")


async def _apply_field(
    message: Message,
    admin_id: int,
    rid: int,
    field: str,
    value: str,
) -> None:
    meta = TEXT_FIELDS.get(field)
    if meta is None:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return
    if len(val) > meta.get("max", 300):
        await message.answer("❌ Texto muito longo.")
        return

    if field == "name":
        try:
            async with session_scope() as session:
                r = await session.execute(
                    select(func.count(Role.id)).where(
                        Role.name == val,
                        Role.id != rid,
                    )
                )
                if int(r.scalar() or 0) > 0:
                    await message.answer("❌ Já existe um cargo com este nome.")
                    return
        except Exception:
            pass

    try:
        async with session_scope() as session:
            r = await session.execute(select(Role).where(Role.id == rid))
            role: Optional[Role] = r.scalar_one_or_none()
            if role is None:
                await message.answer("❌ Cargo não encontrado.")
                return
            setattr(role, field, val)
            session.add(role)
    except Exception as exc:
        logger.exception(f"_apply_field (role) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"role.set.{field}",
        category="admin",
        details={"role_id": rid, "value": val},
    )

    text, keyboard = await _render_editor(rid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# PERMISSÕES: ADD / DEL
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PERM_ADD}:"))
async def on_perm_add(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:permissions:padd:ID:permission
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        rid = int(parts[4])
        perm = parts[5].lower()

        try:
            async with session_scope() as session:
                res = await session.execute(select(Role).where(Role.id == rid))
                role: Optional[Role] = res.scalar_one_or_none()
                if role is None:
                    await toast_err(callback, "❌ Cargo não encontrado.")
                    return
                perms = _perms_to_list(role.permissions)
                if perm == "*":
                    perms = ["*"]
                else:
                    if perm not in perms:
                        perms.append(perm)
                role.permissions = _validate_perms(perms)
                session.add(role)
        except Exception as exc:
            logger.exception(f"perm add falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"role.perm.add.{perm}",
            category="admin",
            details={"role_id": rid, "permission": perm},
        )

        text, keyboard = await _render_editor(rid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"✅ +{perm}")
    except Exception as exc:
        logger.exception(f"permissions on_perm_add falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PERM_DEL}:"))
async def on_perm_del(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        # admin:permissions:pdel:ID:permission
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        rid = int(parts[4])
        perm = parts[5].lower()

        try:
            async with session_scope() as session:
                res = await session.execute(select(Role).where(Role.id == rid))
                role: Optional[Role] = res.scalar_one_or_none()
                if role is None:
                    await toast_err(callback, "❌ Cargo não encontrado.")
                    return
                perms = [p for p in _perms_to_list(role.permissions) if p != perm]
                role.permissions = _validate_perms(perms)
                session.add(role)
        except Exception as exc:
            logger.exception(f"perm del falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"role.perm.del.{perm}",
            category="admin",
            details={"role_id": rid, "permission": perm},
        )

        text, keyboard = await _render_editor(rid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"➖ -{perm}")
    except Exception as exc:
        logger.exception(f"permissions on_perm_del falhou: {exc}")


# ============================================================
# LISTA DE MÓDULOS (TOGGLES)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PERM_MODULES}:"))
async def on_perm_modules(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        # admin:permissions:pmods:ID:page:N
        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        rid = int(parts[4])
        page = 1
        if len(parts) >= 7 and parts[5] == "page":
            try:
                page = int(parts[6])
            except Exception:
                page = 1

        role = await _load_role(rid)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        perms = _perms_to_list(role.permissions)
        has_wildcard = "*" in perms

        # Paginação de módulos (8 por página)
        per_page = 8
        total_p = max(1, (len(MODULES) + per_page - 1) // per_page)
        page = max(1, min(page, total_p))
        start = (page - 1) * per_page
        page_modules = MODULES[start:start + per_page]

        lines = [
            f"📦 <b>MÓDULOS · {truncate(role.name, 30)}</b>",
            f"Página {page}/{total_p}",
            "",
        ]

        if has_wildcard:
            lines.append("👑 <b>Este cargo tem Acesso Total (*)</b>")
            lines.append("Toque em um módulo para remover o wildcard e usar permissões explícitas.")

        rows_kb: List[List[InlineKeyboardButton]] = []
        buf: List[InlineKeyboardButton] = []
        for m in page_modules:
            has = has_wildcard or m in perms
            label = f"{'✅' if has else '☑️'} {m}"
            cb = (
                f"{CB_PERM_DEL}:{rid}:{m}"
                if has and not has_wildcard
                else f"{CB_PERM_ADD}:{rid}:{m}"
            )
            if has_wildcard and m not in perms:
                # Se wildcard ativo, permite "fixar" módulo específico
                cb = f"{CB_PERM_ADD}:{rid}:{m}"
            buf.append(InlineKeyboardButton(text=label, callback_data=cb))
            if len(buf) == 2:
                rows_kb.append(buf)
                buf = []
        if buf:
            rows_kb.append(buf)

        # Paginação
        if total_p > 1:
            nav: List[InlineKeyboardButton] = []
            if page > 1:
                nav.append(
                    InlineKeyboardButton(
                        text="⬅️ Anterior",
                        callback_data=f"{CB_PERM_MODULES}:{rid}:page:{page - 1}",
                    )
                )
            if page < total_p:
                nav.append(
                    InlineKeyboardButton(
                        text="Próxima ➡️",
                        callback_data=f"{CB_PERM_MODULES}:{rid}:page:{page + 1}",
                    )
                )
            if nav:
                rows_kb.append(nav)

        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="✏️ Adicionar Custom",
                    callback_data=f"admin:permissions:paddcustom:{rid}",
                )
            ]
        )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{rid}")]
        )

        text = "\n".join(lines)

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_perm_modules falhou: {exc}")


# ============================================================
# PERMISSÕES ESPECIAIS
# ============================================================
@router.callback_query(F.data.startswith("admin:permissions:special:"))
async def on_special(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
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
        has_wildcard = "*" in perms

        rows_kb: List[List[InlineKeyboardButton]] = []
        buf: List[InlineKeyboardButton] = []
        for key, label in SPECIAL_PERMS.items():
            if key == "*":
                continue
            has = has_wildcard or key in perms
            cb = (
                f"{CB_PERM_DEL}:{rid}:{key}"
                if has
                else f"{CB_PERM_ADD}:{rid}:{key}"
            )
            buf.append(
                InlineKeyboardButton(
                    text=f"{'✅' if has else '☑️'} {label}",
                    callback_data=cb,
                )
            )
            if len(buf) == 2:
                rows_kb.append(buf)
                buf = []
        if buf:
            rows_kb.append(buf)

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{rid}")]
        )

        text = (
            f"🔐 <b>PERMISSÕES ESPECIAIS · {truncate(role.name, 30)}</b>\n\n"
            "Toque para ativar/desativar permissões especiais:"
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_special falhou: {exc}")


# ============================================================
# ADICIONAR PERMISSÃO CUSTOMIZADA
# ============================================================
@router.callback_query(F.data.startswith("admin:permissions:paddcustom:"))
async def on_paddcustom(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            rid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        prompt = (
            "✏️ <b>PERMISSÃO CUSTOMIZADA</b>\n\n"
            "Envie o nome da permissão (ex.: <code>products:export</code>).\n"
            "Suporta wildcards: <code>products:*</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"perm_custom:{rid}",
            screen=f"permissions:custom:{rid}",
            extra={"role_id": rid},
            use_force_reply=True,
            reply_placeholder="ex: products:export",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_paddcustom falhou: {exc}")


async def _add_custom_permission(
    message: Message,
    admin_id: int,
    rid: int,
    raw_perm: str,
) -> None:
    perm = (raw_perm or "").strip().lower().replace(" ", "")
    if not perm or len(perm) > 60:
        await message.answer("❌ Permissão inválida.")
        return

    try:
        async with session_scope() as session:
            res = await session.execute(select(Role).where(Role.id == rid))
            role: Optional[Role] = res.scalar_one_or_none()
            if role is None:
                await message.answer("❌ Cargo não encontrado.")
                return
            perms = _perms_to_list(role.permissions)
            if perm not in perms:
                perms.append(perm)
            role.permissions = _validate_perms(perms)
            session.add(role)
    except Exception as exc:
        logger.exception(f"add custom perm falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action="role.perm.custom_add",
        category="admin",
        details={"role_id": rid, "permission": perm},
    )

    text, keyboard = await _render_editor(rid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# APLICAR CARGO EM MASSA
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_APPLY}:"))
async def on_apply(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
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

        prompt = (
            f"📥 <b>APLICAR CARGO EM MASSA</b>\n\n"
            f"Cargo: <b>{role.name}</b>\n\n"
            "Envie os <b>Telegram IDs</b> dos admins (separados por vírgula).\n"
            "Ex.: <code>123456789,987654321</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"applyto:{rid}",
            screen=f"permissions:apply:{rid}",
            extra={"role_id": rid},
            use_force_reply=True,
            reply_placeholder="123,456",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_apply falhou: {exc}")


async def _apply_role_batch(
    message: Message,
    admin_id: int,
    rid: int,
    raw_ids: str,
) -> None:
    # Reutiliza o parser do tools
    import re as _re

    tg_ids: List[int] = []
    for token in _re.split(r"[,\s\n]+", raw_ids or ""):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            try:
                tg_ids.append(int(token))
            except Exception:
                continue

    if not tg_ids:
        await message.answer("❌ Nenhum ID válido.")
        return

    role = await _load_role(rid)
    if role is None:
        await message.answer("❌ Cargo não encontrado.")
        return

    perms = _perms_to_list(role.permissions)

    success = 0
    failed = 0
    try:
        async with session_scope() as session:
            for tg_id in tg_ids:
                try:
                    res = await session.execute(
                        select(Admin).where(Admin.telegram_id == tg_id)
                    )
                    a: Optional[Admin] = res.scalar_one_or_none()
                    if a is None:
                        failed += 1
                        continue
                    a.role_id = rid
                    a.permissions = perms
                    session.add(a)
                    success += 1
                except Exception:
                    failed += 1
                    continue
    except Exception as exc:
        logger.exception(f"_apply_role_batch falhou: {exc}")
        await message.answer("❌ Erro ao aplicar.")
        return

    await log_action(
        admin_id=admin_id,
        action="role.applied_batch",
        category="admin",
        details={
            "role_id": rid,
            "role_name": role.name,
            "tg_ids": tg_ids,
            "success": success,
            "failed": failed,
        },
    )

    await message.answer(
        f"✅ <b>Cargo aplicado em massa</b>\n\n"
        f"🎭 {role.name}\n"
        f"✅ Sucesso: <b>{success}</b>\n"
        f"❌ Falhas: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{rid}")]]
        ),
    )


# ============================================================
# ADMINS COM ESTE CARGO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_ADMINS}:"))
async def on_admins_with_role(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # admin:permissions:admins:ID:page:N
        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        rid = int(parts[4])
        page = 1
        if len(parts) >= 7 and parts[5] == "page":
            try:
                page = int(parts[6])
            except Exception:
                page = 1

        role = await _load_role(rid)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        try:
            async with session_scope() as session:
                total = int(
                    (
                        await session.execute(
                            select(func.count(Admin.id)).where(Admin.role_id == rid)
                        )
                    ).scalar()
                    or 0
                )
                total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
                page = max(1, min(page, total_p))

                res = await session.execute(
                    select(Admin)
                    .where(Admin.role_id == rid)
                    .order_by(Admin.id.asc())
                    .offset(page_offset(page, ADMIN_PAGE_SIZE_DEFAULT))
                    .limit(ADMIN_PAGE_SIZE_DEFAULT)
                )
                admins = list(res.scalars().all())
        except Exception:
            total = 0
            total_p = 1
            admins = []

        if not admins:
            text = (
                f"👤 <b>ADMINS COM CARGO {truncate(role.name, 30)}</b>\n\n"
                "<i>Nenhum admin com este cargo.</i>"
            )
        else:
            rows: List[List[str]] = []
            for a in admins:
                em = "👑" if a.is_main_admin else "🛡️"
                rows.append(
                    [
                        str(a.id),
                        em,
                        str(a.telegram_id),
                        truncate(a.first_name or "—", 20),
                    ]
                )
            lista = table(rows, ["ID", "", "TG ID", "Nome"])
            text = (
                f"👤 <b>ADMINS COM CARGO {truncate(role.name, 30)} ({total})</b>\n\n"
                f"<code>{lista}</code>"
            )

        rows_kb: List[List[InlineKeyboardButton]] = []
        pag_kb = build_pagination_row(
            current_page=page,
            total_pages=total_p,
            base_callback=f"{CB_ADMINS}:{rid}:page",
            back_callback=f"{CB_VIEW}:{rid}",
        )
        if pag_kb:
            rows_kb.append(pag_kb)
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{rid}")]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_admins_with_role falhou: {exc}")


# ============================================================
# EXCLUIR CARGO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            rid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        role = await _load_role(rid)
        if role is None:
            await toast_err(callback, "❌ Cargo não encontrado.")
            return

        if role.is_system:
            await toast_err(callback, "❌ Cargos do sistema não podem ser excluídos.")
            return

        admin_count = await _count_admins_in_role(rid)

        text = (
            f"🗑️ <b>EXCLUIR CARGO</b>\n\n"
            f"🎭 <b>{role.name}</b> (#{role.id})\n"
            f"👤 Admins usando: <b>{admin_count}</b>\n\n"
        )
        if admin_count > 0:
            text += "⚠️ Os admins ficarão sem cargo (role_id=None).\n\n"
        text += "Tem certeza?"

        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{rid}",
            cancel_callback=f"{CB_VIEW}:{rid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"permissions on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
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
                    sa_update(Admin).where(Admin.role_id == rid).values(role_id=None)
                )
                # Remove role
                await session.execute(Role.__table__.delete().where(Role.id == rid))
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

        await render_panel(callback)
        await toast_ok(callback, "🗑️ Cargo excluído.")
    except Exception as exc:
        logger.exception(f"permissions on_delete_confirm falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_root",
    "on_new",
    "on_permissions_text_input",
    "on_view",
    "on_set_field",
    "on_perm_add",
    "on_perm_del",
    "on_perm_modules",
    "on_special",
    "on_paddcustom",
    "on_apply",
    "on_admins_with_role",
    "on_delete",
    "on_delete_confirm",
    "CB_ROOT",
    "CB_RELOAD",
    "CB_NEW",
    "CB_VIEW",
    "CB_SET",
    "CB_PERM_ADD",
    "CB_PERM_DEL",
    "CB_PERM_MODULES",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_APPLY",
    "CB_APPLY_TO",
    "CB_APPLY_EXEC",
    "CB_ADMINS",
    "CB_HISTORY",
    "MODULES",
    "SPECIAL_PERMS",
    "TEXT_FIELDS",
]
