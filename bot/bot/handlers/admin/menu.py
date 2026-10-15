"""
Larizinha Store — Menu Principal do Painel Admin
Implementa o Menu Principal EXATAMENTE como o DIM define:

    ╔══════════════════════════════════╗
    ║    🛡️ PAINEL ADMINISTRATIVO       ║
    ║    {{nome_loja}} · v{{versao}}    ║
    ╚══════════════════════════════════╝

    👤 Admin: {{nome_admin}}
    🆔 ID: {{id_admin}}
    🕐 Último acesso: {{ultimo_acesso}}

Rodapé: [🔄 Atualizar] [👤 Admin] [🔙 Voltar]

30 Módulos (callback = admin:<modulo>):
    1  📊 Dashboard           admin:dashboard
    2  📦 Produtos            admin:products
    3  🗂️ Categorias          admin:categories
    4  💰 Vendas              admin:sales
    5  👥 Usuários            admin:users
    6  💳 Pagamentos          admin:payments
    7  🎁 Gift Cards          admin:gifts
    8  🎟️ Cupons              admin:coupons
    9  📢 Promoções           admin:promos
    10 📡 Canal               admin:channel
    11 👥 Afiliados           admin:affiliates
    12 💸 Saques              admin:withdraws
    13 ✏️ Mensagens           admin:messages
    14 🔘 Botões              admin:buttons
    15 🔐 Credenciais         admin:credentials
    16 📈 Estatísticas        admin:stats
    17 ⚙️ Configurações       admin:settings
    18 🚫 Blacklist           admin:blacklist
    19 📤 Backup              admin:backup
    20 📨 Disparos            admin:broadcast
    21 🧠 Automações          admin:automations
    22 🧪 Testes              admin:tests
    23 🧾 Logs                admin:logs
    24 🔒 Travas              admin:locks
    25 👑 Admins              admin:admins
    26 🎫 Tickets             admin:tickets
    27 🎮 Jogos               admin:games
    28 🛡️ Segurança          admin:security
    29 ⚠️ Alertas             admin:alerts
    30 🔙 Sair                admin:exit

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Todos os botões são renderizados pelo button_engine (screen="admin_menu").
- Fallback: se o painel não tiver botões customizados, usa a matriz padrão DIM.
- Comando /admin abre o painel (bloqueado para não-admins).
- Botão admin:exit volta ao bot (com boas-vindas).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple, Union

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.database import Admin, User, session_scope
from bot.logger import logger
from bot.redis_client import Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from sqlalchemy import select

from bot.handlers.admin.helpers import (
    AdminFlow,
    AdminCtx,
    answer,
    edit_or_send,
    fmt_dt,
    kb,
    load_admin,
    log_action,
    toast_err,
    toast_ok,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_menu")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_MENU = "admin_menu"
SCREEN_MENU = "admin_menu"

CB_ADMIN_ROOT = "admin:root"
CB_ADMIN_MENU = "admin:menu"
CB_ADMIN_REFRESH = "admin:refresh"
CB_ADMIN_PROFILE = "admin:profile"
CB_ADMIN_EXIT = "admin:exit"
CB_ADMIN_BACK = "admin:back_menu"

# Prefixo de todos os módulos do painel
CB_MODULE_PREFIX = "admin:"

# Módulos do DIM (ordem exata do documento)
MODULES: List[Tuple[str, str, str]] = [
    ("📊 Dashboard",           "admin:dashboard",   "dashboard"),
    ("📦 Produtos",            "admin:products",    "products"),
    ("🗂️ Categorias",          "admin:categories",  "categories"),
    ("💰 Vendas",              "admin:sales",       "sales"),
    ("👥 Usuários",            "admin:users",       "users"),
    ("💳 Pagamentos",          "admin:payments",    "payments"),
    ("🎁 Gift Cards",          "admin:gifts",       "gifts"),
    ("🎟️ Cupons",              "admin:coupons",     "coupons"),
    ("📢 Promoções",           "admin:promos",      "promos"),
    ("📡 Canal",               "admin:channel",     "channel"),
    ("👥 Afiliados",           "admin:affiliates",  "affiliates"),
    ("💸 Saques",              "admin:withdraws",   "withdraws"),
    ("✏️ Mensagens",           "admin:messages",    "messages"),
    ("🔘 Botões",              "admin:buttons",     "buttons"),
    ("🔐 Credenciais",         "admin:credentials", "credentials"),
    ("📈 Estatísticas",        "admin:stats",       "stats"),
    ("⚙️ Configurações",       "admin:settings",    "settings"),
    ("🚫 Blacklist",           "admin:blacklist",   "blacklist"),
    ("📤 Backup",              "admin:backup",      "backup"),
    ("📨 Disparos",            "admin:broadcast",   "broadcast"),
    ("🧠 Automações",          "admin:automations", "automations"),
    ("🧪 Testes",              "admin:tests",       "tests"),
    ("🧾 Logs",                "admin:logs",        "logs"),
    ("🔒 Travas",              "admin:locks",       "locks"),
    ("👑 Admins",              "admin:admins",      "admins"),
    ("🎫 Tickets",             "admin:tickets",     "tickets"),
    ("🎮 Jogos",               "admin:games",       "games"),
    ("🛡️ Segurança",           "admin:security",    "security"),
    ("⚠️ Alertas",             "admin:alerts",      "alerts"),
    ("🔙 Sair",                "admin:exit",        "exit"),
]


# ============================================================
# 🧰 HELPERS LOCAIS
# ============================================================
async def _load_admin_row(tg_id: int) -> Optional[Admin]:
    """Carrega o registro Admin (tabela admins) pelo telegram_id."""
    return await load_admin(tg_id)


async def _ensure_admin_row(tg_id: int) -> Optional[Admin]:
    """
    Garante que o admin exista na tabela `admins`.
    Se estiver apenas no .env, cria na primeira vez.
    """
    row = await _load_admin_row(tg_id)
    if row is not None:
        # Atualiza último acesso
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Admin).where(Admin.telegram_id == tg_id)
                )
                adm: Optional[Admin] = res.scalar_one_or_none()
                if adm is not None:
                    adm.last_login_at = datetime.now(timezone.utc)
                    session.add(adm)
        except Exception:
            pass
        return row

    # Não existe no banco — cria se estiver na whitelist do .env
    if tg_id not in settings.admin_ids_list:
        return None

    try:
        async with session_scope() as session:
            adm = Admin(
                telegram_id=tg_id,
                is_main_admin=(tg_id == settings.MAIN_ADMIN_ID),
                permissions=["*"] if tg_id == settings.MAIN_ADMIN_ID else [],
                last_login_at=datetime.now(timezone.utc),
            )
            session.add(adm)
            await session.flush()
            await session.refresh(adm)
            return adm
    except Exception as exc:
        logger.debug(f"_ensure_admin_row falhou: {exc}")
        return None


def _build_default_keyboard() -> InlineKeyboardMarkup:
    """
    Monta a matriz de botões padrão do DIM (fallback).
    Coloca 2 por linha, com os últimos 5 agrupados.
    """
    rows: List[List[InlineKeyboardButton]] = []

    # Botões principais (1 a 29) — 2 por linha
    main_modules = MODULES[:-1]  # todos menos o "Sair"
    buf: List[InlineKeyboardButton] = []
    for label, cb, _key in main_modules:
        buf.append(InlineKeyboardButton(text=label, callback_data=cb))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)

    # Botão Sair separado
    rows.append(
        [
            InlineKeyboardButton(text="🔙 Sair", callback_data=CB_ADMIN_EXIT),
        ]
    )

    # Rodapé
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_ADMIN_REFRESH),
            InlineKeyboardButton(text="👤 Admin", callback_data=CB_ADMIN_PROFILE),
            InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ADMIN_BACK),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_admin_keyboard() -> InlineKeyboardMarkup:
    """
    Monta o teclado do menu admin:
    - Tenta o button_engine (editável no painel) primeiro.
    - Se ausente ou vazio, usa o fallback padrão DIM.
    """
    try:
        engine_kb = await button_engine.inline(SCREEN_MENU)
        if engine_kb is not None and engine_kb.inline_keyboard:
            return engine_kb
    except Exception as exc:
        logger.debug(f"button_engine admin_menu falhou: {exc}")

    return _build_default_keyboard()


# ============================================================
# RENDER DO MENU
# ============================================================
async def render_menu(
    target: Union[CallbackQuery, Message],
    admin_id: int,
) -> Optional[Message]:
    """
    Renderiza o menu principal do painel admin.
    - Sempre EDITA (se callback) ou envia (se message).
    - Puxa nome do admin, último acesso, versão, nome da loja.
    """
    # ---------- Carrega dados ----------
    admin_row = await _ensure_admin_row(admin_id)

    nome_admin = "Admin"
    try:
        if isinstance(target, CallbackQuery) and target.from_user:
            nome_admin = (
                target.from_user.full_name
                or target.from_user.first_name
                or "Admin"
            )
        elif isinstance(target, Message) and target.from_user:
            nome_admin = (
                target.from_user.full_name
                or target.from_user.first_name
                or "Admin"
            )
    except Exception:
        pass

    ultimo_acesso = "—"
    if admin_row is not None and admin_row.last_login_at:
        ultimo_acesso = fmt_dt(admin_row.last_login_at)

    # ---------- Texto editável ----------
    rendered = await message_engine.render(
        MSG_KEY_MENU,
        fallback=(
            "╔══════════════════════════════════╗\n"
            "║    🛡️ PAINEL ADMINISTRATIVO       ║\n"
            f"║    {settings.APP_NAME} · v{settings.APP_VERSION}    ║\n"
            "╚══════════════════════════════════╝\n\n"
            f"👤 Admin: <b>{nome_admin}</b>\n"
            f"🆔 ID: <code>{admin_id}</code>\n"
            f"🕐 Último acesso: {ultimo_acesso}"
        ),
        nome_loja=settings.APP_NAME,
        versao=settings.APP_VERSION,
        nome_admin=nome_admin,
        id_admin=admin_id,
        ultimo_acesso=ultimo_acesso,
    )

    # ---------- Teclado ----------
    keyboard = await _build_admin_keyboard()

    # ---------- Envia/edita ----------
    return await edit_or_send(
        target,
        text=rendered.content,
        reply_markup=keyboard,
    )


# ============================================================
# HANDLERS — ABRIR MENU
# ============================================================
@router.message(Command("admin"))
async def on_cmd_admin(message: Message, admin: Optional[Admin] = None) -> None:
    """
    /admin no privado abre o painel.
    Se não for admin, resposta silenciosa (não acumula).
    """
    try:
        # Apaga a mensagem do admin para não acumular
        try:
            await message.delete()
        except Exception:
            pass

        admin_id = message.from_user.id if message.from_user else 0
        if admin_id not in settings.admin_ids_list:
            try:
                rendered = await message_engine.render(
                    "admin_negado",
                    fallback=(
                        "⛔ <b>Acesso negado.</b>\n\n"
                        "Você não tem permissão para acessar o painel."
                    ),
                )
                await message.answer(
                    text=rendered.content,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        await AdminFlow.clear(admin_id)
        await render_menu(message, admin_id)

        # Auditoria
        try:
            await log_action(
                admin_id=admin_id,
                action="admin.panel_opened",
                category="admin",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"on_cmd_admin falhou: {exc}")


@router.callback_query(F.data.in_({CB_ADMIN_ROOT, CB_ADMIN_MENU}))
async def on_open_menu(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await toast_err(callback, "⛔ Acesso negado.")
            return

        if not await Throttle.allow(admin_id, "admin_menu", cooldown=0.3):
            await answer(callback)
            return

        await AdminFlow.clear(admin_id)
        await render_menu(callback, admin_id)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_open_menu falhou: {exc}")


# ============================================================
# HANDLERS — ATUALIZAR / PERFIL / VOLTAR / SAIR
# ============================================================
@router.callback_query(F.data == CB_ADMIN_REFRESH)
async def on_refresh(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await toast_err(callback, "⛔ Acesso negado.")
            return

        await render_menu(callback, admin_id)
        await toast_ok(callback, "🔄 Atualizado!")
    except Exception as exc:
        logger.exception(f"on_refresh falhou: {exc}")


@router.callback_query(F.data == CB_ADMIN_PROFILE)
async def on_admin_profile(callback: CallbackQuery) -> None:
    """
    Mostra o perfil do admin logado com informações úteis.
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await toast_err(callback, "⛔ Acesso negado.")
            return

        admin_row = await _ensure_admin_row(admin_id)

        nome = callback.from_user.full_name or "Admin"
        username = f"@{callback.from_user.username}" if callback.from_user.username else "—"
        is_main = admin_id == settings.MAIN_ADMIN_ID
        cargo = "👑 Admin Principal" if is_main else "🛡️ Admin"

        perms: List[str] = []
        try:
            if admin_row is not None and admin_row.permissions:
                if isinstance(admin_row.permissions, list):
                    perms = [str(p) for p in admin_row.permissions]
        except Exception:
            perms = []

        if is_main:
            perms = ["*"]

        perms_text = ", ".join(perms[:12]) if perms else "—"
        ultimo_login = fmt_dt(admin_row.last_login_at) if admin_row else "—"

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        text = (
            "👤 <b>PERFIL DO ADMIN</b>\n\n"
            f"🧑 Nome: <b>{nome}</b>\n"
            f"🔗 Username: {username}\n"
            f"🆔 ID: <code>{admin_id}</code>\n"
            f"👑 Cargo: {cargo}\n"
            f"🕐 Último acesso: {ultimo_login}\n\n"
            f"🔐 Permissões ({len(perms)}):\n"
            f"<code>{perms_text}</code>"
        )

        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_ADMIN_MENU,
                    )
                ]
            ]
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=back_kb,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_admin_profile falhou: {exc}")


@router.callback_query(F.data == CB_ADMIN_BACK)
async def on_back(callback: CallbackQuery) -> None:
    """
    Volta para as boas-vindas do bot (mesma função do botão 🔙 Voltar).
    """
    try:
        admin_id = callback.from_user.id

        await AdminFlow.clear(admin_id)

        # Renderiza boas-vindas no chat atual
        try:
            from bot.handlers.start import _render_welcome

            fresh_user: Optional[User] = None
            try:
                async with session_scope() as session:
                    res = await session.execute(
                        select(User).where(User.telegram_id == admin_id)
                    )
                    fresh_user = res.scalar_one_or_none()
            except Exception:
                fresh_user = None

            if fresh_user is not None:
                text, keyboard = await _render_welcome(fresh_user)
                await edit_or_send(
                    callback,
                    text=text,
                    reply_markup=keyboard,
                )
            else:
                await edit_or_send(
                    callback,
                    text="🏠 Voltando ao início...",
                )
        except Exception as exc:
            logger.debug(f"voltar para boas-vindas falhou: {exc}")
            await edit_or_send(
                callback,
                text="🏠 Voltando ao início...",
            )

        await answer(callback, "🏠 Voltando...")
    except Exception as exc:
        logger.exception(f"on_back falhou: {exc}")


@router.callback_query(F.data == CB_ADMIN_EXIT)
async def on_exit(callback: CallbackQuery) -> None:
    """
    Sai do painel administrativo (botão 🔙 Sair).
    """
    try:
        admin_id = callback.from_user.id

        await AdminFlow.clear(admin_id)

        rendered = await message_engine.render(
            "admin_sair",
            fallback=(
                "👋 <b>Você saiu do Painel Administrativo.</b>\n\n"
                "Para voltar, mande <code>/admin</code> no chat privado."
            ),
        )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        kb_exit = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤖 Voltar ao Bot",
                        callback_data="catalog:open",
                    )
                ]
            ]
        )

        await edit_or_send(
            callback,
            text=rendered.content,
            reply_markup=kb_exit,
        )
        await answer(callback, "👋 Até logo!")

        try:
            await log_action(
                admin_id=admin_id,
                action="admin.panel_closed",
                category="admin",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"on_exit falhou: {exc}")


# ============================================================
# HANDLERS — DESPACHO DE MÓDULOS
# ============================================================
@router.callback_query(F.data.startswith(CB_MODULE_PREFIX))
async def on_module_dispatch(callback: CallbackQuery) -> None:
    """
    Despacho central para os módulos do painel.
    Cada módulo tem seu próprio handler que responde ao prefixo
    `admin:<modulo>` (ex.: admin:dashboard).

    Este handler é o fallback (último a ser avaliado): se nenhum
    módulo específico tiver tratado, ele avisa que o módulo ainda
    não está implementado — sem acumular, sem travar o bot.
    """
    try:
        cb_data = callback.data or ""
        # Ignora os callbacks que JÁ são tratados acima
        if cb_data in {
            CB_ADMIN_ROOT,
            CB_ADMIN_MENU,
            CB_ADMIN_REFRESH,
            CB_ADMIN_PROFILE,
            CB_ADMIN_EXIT,
            CB_ADMIN_BACK,
        }:
            return

        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await toast_err(callback, "⛔ Acesso negado.")
            return

        # Extrai o nome do módulo
        module = cb_data.split(":", 1)[1] if ":" in cb_data else "unknown"

        logger.debug(
            f"admin module dispatch: module={module} admin={admin_id}"
        )

        # Aviso amigável para módulo não implementado
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        text = (
            "🚧 <b>Módulo em construção</b>\n\n"
            f"O módulo <code>{module}</code> ainda não foi implementado.\n"
            "Ele será ativado em breve."
        )

        kb_back = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_ADMIN_MENU,
                    )
                ]
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=kb_back)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_module_dispatch falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_menu",
    "on_cmd_admin",
    "on_open_menu",
    "on_refresh",
    "on_admin_profile",
    "on_back",
    "on_exit",
    "on_module_dispatch",
    "MODULES",
    "CB_ADMIN_ROOT",
    "CB_ADMIN_MENU",
    "CB_ADMIN_REFRESH",
    "CB_ADMIN_PROFILE",
    "CB_ADMIN_EXIT",
    "CB_ADMIN_BACK",
    "CB_MODULE_PREFIX",
    "MSG_KEY_MENU",
    "SCREEN_MENU",
]
