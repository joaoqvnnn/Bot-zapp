"""
Larizinha Store — Módulo Admin: Credenciais
Implementa o editor de credenciais de APIs conforme DIM.

Campos (Settings key):
- Mercado Pago: mp_access_token, mp_public_key, mp_webhook_secret,
  mp_webhook_url, mp_expiration_minutes
- SMTP: smtp_host, smtp_port, smtp_email, smtp_password, smtp_use_tls
- Webhooks: telegram_webhook_url, mp_webhook_url

Ações:
- Editar qualquer campo (mascarado quando sensível)
- Testar MP (consulta /users/me)
- Testar SMTP (envia e-mail de teste)
- Mostrar/ocultar valor (mascarar/desmascarar)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime (Setting).
- Toda ação crítica é auditada (log_action).
- Valores sensíveis exibidos mascarados por padrão.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import aiosmtplib
import httpx
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from email.message import EmailMessage
from sqlalchemy import select

from bot.config import settings
from bot.database import Setting, session_scope
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    AdminFlow,
    SYM_OFF,
    SYM_OK,
    answer,
    ask_input,
    build_back_row,
    consume_input,
    edit_or_send,
    is_int,
    kb,
    log_action,
    parse_int,
    toast_err,
    toast_ok,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_credentials")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_credentials_panel"
MSG_KEY_TEST = "admin_credentials_test"

SCREEN_PANEL = "admin_credentials_panel"

# Callbacks base
CB_ROOT = "admin:credentials"
CB_SET = "admin:credentials:set"           # :field
CB_REVEAL = "admin:credentials:reveal"     # :field
CB_TEST_MP = "admin:credentials:testmp"
CB_TEST_SMTP = "admin:credentials:testsmtp"

# Campos configuráveis
FIELDS: Dict[str, Dict[str, Any]] = {
    # ---- Mercado Pago ----
    "mp_access_token": {
        "label": "MP Access Token",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "default": "",
    },
    "mp_public_key": {
        "label": "MP Public Key",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "default": "",
    },
    "mp_webhook_secret": {
        "label": "MP Webhook Secret",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "default": "",
    },
    "mp_webhook_url": {
        "label": "MP Webhook URL",
        "group": "Mercado Pago",
        "sensitive": False,
        "type": "url",
        "default": "",
    },
    "mp_expiration_minutes": {
        "label": "MP Expiração (min)",
        "group": "Mercado Pago",
        "sensitive": False,
        "type": "int",
        "default": str(settings.MP_PIX_EXPIRATION_MINUTES),
    },
    # ---- SMTP ----
    "smtp_host": {
        "label": "SMTP Host",
        "group": "SMTP",
        "sensitive": False,
        "type": "text",
        "default": "",
    },
    "smtp_port": {
        "label": "SMTP Porta",
        "group": "SMTP",
        "sensitive": False,
        "type": "int",
        "default": "587",
    },
    "smtp_email": {
        "label": "SMTP E-mail",
        "group": "SMTP",
        "sensitive": False,
        "type": "text",
        "default": "",
    },
    "smtp_password": {
        "label": "SMTP Senha",
        "group": "SMTP",
        "sensitive": True,
        "type": "text",
        "default": "",
    },
    "smtp_use_tls": {
        "label": "SMTP TLS",
        "group": "SMTP",
        "sensitive": False,
        "type": "bool",
        "default": "true",
    },
    # ---- Webhooks ----
    "telegram_webhook_url": {
        "label": "Webhook Telegram",
        "group": "Webhooks",
        "sensitive": False,
        "type": "url",
        "default": "",
    },
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _mask_value(value: Optional[str], keep: int = 4) -> str:
    if not value:
        return "—"
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


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


async def _set_setting(key: str, value: str, category: str = "credentials") -> bool:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is None:
                session.add(Setting(key=key, value=value, category=category))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting (credentials) falhou: {exc}")
        return False


def _validate_url(v: str) -> bool:
    return v.startswith("http://") or v.startswith("https://")


async def _resolve_effective_value(key: str) -> Optional[str]:
    """
    Retorna o valor efetivo (DB se existir, senão do .env).
    """
    meta = FIELDS.get(key, {})
    env_fallback = meta.get("env_fallback")
    val = await _get_setting(key, None)
    if val is not None and str(val).strip() != "":
        return str(val)
    # fallback para .env
    attr = meta.get("env_attr")
    if attr:
        return getattr(settings, attr, None)
    return env_fallback


def _resolve_env_default(key: str) -> Optional[str]:
    """Faz o match com os campos do Settings do .env."""
    mapping = {
        "mp_access_token": "MP_ACCESS_TOKEN",
        "mp_public_key": "MP_PUBLIC_KEY",
        "mp_webhook_secret": "MP_WEBHOOK_SECRET",
        "mp_webhook_url": "MP_WEBHOOK_URL",
        "mp_expiration_minutes": "MP_PIX_EXPIRATION_MINUTES",
        "smtp_host": "SMTP_HOST",
        "smtp_port": "SMTP_PORT",
        "smtp_email": "SMTP_EMAIL",
        "smtp_password": "SMTP_PASSWORD",
        "smtp_use_tls": "SMTP_USE_TLS",
        "telegram_webhook_url": "TELEGRAM_WEBHOOK_URL",
    }
    attr = mapping.get(key)
    if not attr:
        return None
    try:
        v = getattr(settings, attr, None)
    except Exception:
        return None
    if v is None:
        return None
    return str(v).lower() if isinstance(v, bool) else str(v)


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Coleta valores
    mp: Dict[str, str] = {}
    smtp: Dict[str, str] = {}
    webhooks: Dict[str, str] = {}

    for key, meta in FIELDS.items():
        val = await _resolve_effective_value(key)
        if val is None:
            val = _resolve_env_default(key) or meta.get("default", "")
        val = str(val)

        if meta["sensitive"]:
            display = _mask_value(val)
        elif meta["type"] == "bool":
            display = "🟢" if str(val).lower() == "true" else "🔴"
        else:
            display = val or "—"

        if meta["group"] == "Mercado Pago":
            mp[key] = display
        elif meta["group"] == "SMTP":
            smtp[key] = display
        else:
            webhooks[key] = display

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "🔐 <b>CREDENCIAIS & APIs</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💳 <b>MERCADO PAGO</b>\n"
            f"├ Access: <code>{mp.get('mp_access_token', '—')}</code>\n"
            f"├ Public: <code>{mp.get('mp_public_key', '—')}</code>\n"
            f"├ Webhook Secret: <code>{mp.get('mp_webhook_secret', '—')}</code>\n"
            f"├ Webhook URL: <code>{mp.get('mp_webhook_url', '—')}</code>\n"
            f"└ Expiração: <b>{mp.get('mp_expiration_minutes', '—')}</b> min\n\n"
            "📧 <b>SMTP</b>\n"
            f"├ Host: <code>{smtp.get('smtp_host', '—')}</code>\n"
            f"├ Porta: <code>{smtp.get('smtp_port', '—')}</code>\n"
            f"├ E-mail: <code>{smtp.get('smtp_email', '—')}</code>\n"
            f"├ Senha: <code>{smtp.get('smtp_password', '—')}</code>\n"
            f"└ TLS: {smtp.get('smtp_use_tls', '—')}\n\n"
            "🪝 <b>WEBHOOKS</b>\n"
            f"└ Telegram: <code>{webhooks.get('telegram_webhook_url', '—')}</code>"
        ),
        mp_access=mp.get("mp_access_token", "—"),
        mp_public=mp.get("mp_public_key", "—"),
        mp_webhook=mp.get("mp_webhook_secret", "—"),
        mp_url=mp.get("mp_webhook_url", "—"),
        mp_exp=mp.get("mp_expiration_minutes", "—"),
        smtp_host=smtp.get("smtp_host", "—"),
        smtp_port=smtp.get("smtp_port", "—"),
        smtp_email=smtp.get("smtp_email", "—"),
        smtp_pass=smtp.get("smtp_password", "—"),
        smtp_tls=smtp.get("smtp_use_tls", "—"),
        wh_tg=webhooks.get("telegram_webhook_url", "—"),
        wh_mp=mp.get("mp_webhook_url", "—"),
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="✏️ MP Access", callback_data=f"{CB_SET}:mp_access_token"),
                InlineKeyboardButton(text="✏️ MP Public", callback_data=f"{CB_SET}:mp_public_key"),
            ],
            [
                InlineKeyboardButton(text="✏️ MP Webhook", callback_data=f"{CB_SET}:mp_webhook_secret"),
                InlineKeyboardButton(text="✏️ MP URL", callback_data=f"{CB_SET}:mp_webhook_url"),
            ],
            [
                InlineKeyboardButton(text="✏️ MP Expiração", callback_data=f"{CB_SET}:mp_expiration_minutes"),
                InlineKeyboardButton(text="🧪 Testar MP", callback_data=CB_TEST_MP),
            ],
            [
                InlineKeyboardButton(text="✏️ SMTP Host", callback_data=f"{CB_SET}:smtp_host"),
                InlineKeyboardButton(text="✏️ SMTP Porta", callback_data=f"{CB_SET}:smtp_port"),
            ],
            [
                InlineKeyboardButton(text="✏️ SMTP E-mail", callback_data=f"{CB_SET}:smtp_email"),
                InlineKeyboardButton(text="✏️ SMTP Senha", callback_data=f"{CB_SET}:smtp_password"),
            ],
            [
                InlineKeyboardButton(text="🔄 SMTP TLS", callback_data=f"{CB_SET}:smtp_use_tls"),
                InlineKeyboardButton(text="🧪 Testar SMTP", callback_data=CB_TEST_SMTP),
            ],
            [
                InlineKeyboardButton(text="✏️ Webhook TG", callback_data=f"{CB_SET}:telegram_webhook_url"),
            ],
            [
                InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_ROOT),
                InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
            ],
        ]

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


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
        logger.exception(f"credentials on_panel falhou: {exc}")


# ============================================================
# EDITAR CAMPO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            field = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = FIELDS.get(field)
        if meta is None:
            await toast_err(callback, "❌ Campo inválido.")
            return

        # Booleans → toggle direto
        if meta["type"] == "bool":
            current = await _resolve_effective_value(field) or meta.get("default", "false")
            new_val = "false" if str(current).lower() == "true" else "true"
            ok = await _set_setting(field, new_val)
            if not ok:
                await toast_err(callback, "❌ Erro ao salvar.")
                return
            await log_action(
                admin_id=admin_id,
                action=f"credentials.set.{field}",
                category="admin",
                details={"field": field, "value": new_val},
            )
            await render_panel(callback)
            await toast_ok(callback, f"🔄 {meta['label']} → {new_val}")
            return

        # Instruções específicas
        hint = ""
        if meta["type"] == "int":
            hint = "Ex.: <code>30</code>"
        elif meta["type"] == "url":
            hint = "Deve começar com <code>https://</code>"
        else:
            hint = "Valor em texto (não será exibido depois)"

        extra_warn = ""
        if meta.get("sensitive"):
            extra_warn = "\n⚠️ <b>Valor sensível</b> — será mascarado após salvar."

        prompt = (
            f"✏️ <b>EDITAR · {meta['label']}</b>\n\n"
            f"{hint}\n{extra_warn}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"cred:{field}",
            screen=f"credentials:set:{field}",
            extra={"field": field},
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"credentials on_set_field falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_credentials_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting.startswith("cred:"):
        field = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field(message, admin_id, field, value)
        return


async def _apply_field(
    message: Message,
    admin_id: int,
    field: str,
    value: str,
) -> None:
    meta = FIELDS.get(field)
    if meta is None:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    # Validação por tipo
    if meta["type"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        val = str(int(num))

    if meta["type"] == "url":
        if not _validate_url(val):
            await message.answer("❌ URL inválida (deve começar com https://).")
            return

    ok = await _set_setting(field, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"credentials.set.{field}",
        category="admin",
        details={"field": field, "sensitive": meta.get("sensitive", False)},
    )

    display = _mask_value(val) if meta.get("sensitive") else truncate(val, 60)

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado.\n\n"
        f"Novo valor: <code>{display}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
        ),
    )


# ============================================================
# TESTAR MP
# ============================================================
@router.callback_query(F.data == CB_TEST_MP)
async def on_test_mp(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Testando MP...")

        token = await _resolve_effective_value("mp_access_token")
        if token is None or not token:
            token = _resolve_env_default("mp_access_token") or ""

        if not token:
            await edit_or_send(
                callback,
                text=(
                    "🧪 <b>TESTE MERCADO PAGO</b>\n\n"
                    "❌ <b>Falhou:</b> Access Token vazio.\n\n"
                    "Configure o <code>mp_access_token</code> antes de testar."
                ),
                reply_markup=kb(
                    [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
                ),
            )
            return

        result_msg = ""
        success = False

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(
                    "https://api.mercadopago.com/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    user_id = data.get("id", "—")
                    nickname = data.get("nickname") or data.get("first_name") or "—"
                    email = data.get("email") or "—"
                    result_msg = (
                        f"✅ <b>Sucesso!</b>\n\n"
                        f"👤 Nickname: <b>{nickname}</b>\n"
                        f"🆔 User ID: <code>{user_id}</code>\n"
                        f"📧 E-mail: <code>{email}</code>"
                    )
                    success = True
                elif r.status_code == 401:
                    result_msg = "❌ <b>Falhou:</b> Access Token inválido (401)."
                else:
                    result_msg = (
                        f"❌ <b>Falhou:</b> HTTP {r.status_code}\n"
                        f"<code>{truncate(r.text, 200)}</code>"
                    )
        except httpx.TimeoutException:
            result_msg = "❌ <b>Timeout:</b> Mercado Pago não respondeu em 15s."
        except Exception as exc:
            logger.debug(f"test MP exc: {exc}")
            result_msg = f"❌ <b>Erro:</b> {truncate(str(exc), 180)}"

        await log_action(
            admin_id=admin_id,
            action="credentials.test_mp",
            category="admin",
            details={"success": success},
        )

        text = f"🧪 <b>TESTE MERCADO PAGO</b>\n\n{result_msg}"
        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
    except Exception as exc:
        logger.exception(f"credentials on_test_mp falhou: {exc}")


# ============================================================
# TESTAR SMTP
# ============================================================
@router.callback_query(F.data == CB_TEST_SMTP)
async def on_test_smtp(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Testando SMTP...")

        host = await _resolve_effective_value("smtp_host") or _resolve_env_default("smtp_host") or ""
        port_raw = await _resolve_effective_value("smtp_port") or _resolve_env_default("smtp_port") or "587"
        user = await _resolve_effective_value("smtp_email") or _resolve_env_default("smtp_email") or ""
        password = await _resolve_effective_value("smtp_password") or _resolve_env_default("smtp_password") or ""
        tls_raw = await _resolve_effective_value("smtp_use_tls") or "true"

        try:
            port = int(port_raw)
        except Exception:
            port = 587

        use_tls = str(tls_raw).lower() == "true"

        if not host or not user or not password:
            await edit_or_send(
                callback,
                text=(
                    "🧪 <b>TESTE SMTP</b>\n\n"
                    "❌ <b>Falhou:</b> credenciais incompletas.\n\n"
                    "Preencha: <code>smtp_host</code>, "
                    "<code>smtp_email</code>, <code>smtp_password</code>."
                ),
                reply_markup=kb(
                    [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
                ),
            )
            return

        result_msg = ""
        success = False

        try:
            # Monta e-mail de teste
            msg = EmailMessage()
            msg["From"] = f"{settings.APP_NAME} <{user}>"
            msg["To"] = user
            msg["Subject"] = f"[{settings.APP_NAME}] Teste SMTP"
            msg.set_content(
                "Este é um e-mail de teste enviado pelo painel administrativo.\n"
                "Se você recebeu, o SMTP está configurado corretamente."
            )

            # Envia
            await asyncio.wait_for(
                aiosmtplib.send(
                    msg,
                    hostname=host,
                    port=port,
                    username=user,
                    password=password,
                    start_tls=use_tls,
                    timeout=20,
                ),
                timeout=25,
            )
            result_msg = (
                "✅ <b>Sucesso!</b>\n\n"
                f"📧 E-mail de teste enviado para <code>{user}</code>\n"
                f"🖥️ Host: <code>{host}:{port}</code>\n"
                f"🔐 TLS: {'🟢' if use_tls else '🔴'}"
            )
            success = True
        except asyncio.TimeoutError:
            result_msg = "❌ <b>Timeout:</b> SMTP não respondeu em 25s."
        except Exception as exc:
            logger.debug(f"test SMTP exc: {exc}")
            result_msg = f"❌ <b>Erro:</b> {truncate(str(exc), 180)}"

        await log_action(
            admin_id=admin_id,
            action="credentials.test_smtp",
            category="admin",
            details={"success": success, "host": host, "port": port},
        )

        text = f"🧪 <b>TESTE SMTP</b>\n\n{result_msg}"
        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
    except Exception as exc:
        logger.exception(f"credentials on_test_smtp falhou: {exc}")


# ============================================================
# REVELAR CAMPO (mostrar valor completo sensível)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REVEAL}:"))
async def on_reveal(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal pode ver valores sensíveis.")
            return

        try:
            field = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = FIELDS.get(field)
        if meta is None or not meta.get("sensitive"):
            await toast_err(callback, "❌ Campo inválido ou não sensível.")
            return

        val = await _resolve_effective_value(field) or _resolve_env_default(field) or ""

        await callback.answer(
            text=f"🔓 {meta['label']}:\n{val[:200]}",
            show_alert=True,
        )

        await log_action(
            admin_id=admin_id,
            action=f"credentials.reveal.{field}",
            category="security",
            details={"field": field},
        )
    except Exception as exc:
        logger.exception(f"credentials on_reveal falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_panel",
    "on_set_field",
    "on_credentials_text_input",
    "on_test_mp",
    "on_test_smtp",
    "on_reveal",
    "CB_ROOT",
    "CB_SET",
    "CB_REVEAL",
    "CB_TEST_MP",
    "CB_TEST_SMTP",
    "FIELDS",
]
