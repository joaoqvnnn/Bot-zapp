"""
Larizinha Store — Módulo Admin: Configurações Gerais
Implementa o editor de configurações globais da loja conforme DIM.

Campos editáveis (Setting):
- Nome da loja
- Moeda (BRL, USD, EUR...)
- Fuso horário
- Idioma padrão
- Formato de valores
- Saldo inicial de novos usuários
- Limites (depósito, saque, compra)
- Taxas globais
- Links (termos, privacidade, avisos)
- Contato do suporte (WhatsApp, e-mail)
- Termos de uso (texto)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime (Setting).
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Union

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
    consume_input,
    edit_or_send,
    fmt_num,
    kb,
    log_action,
    parse_int,
    parse_number,
    toast_err,
    toast_ok,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_settings")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_settings_panel"

SCREEN_PANEL = "admin_settings_panel"

# Callbacks base
CB_ROOT = "admin:settings"
CB_SET = "admin:settings:set"           # :field
CB_TOGGLE = "admin:settings:toggle"     # :field

# Campos editáveis
FIELDS: Dict[str, Dict[str, Any]] = {
    # ---- Identidade ----
    "store_name": {
        "label": "Nome da loja",
        "group": "Identidade",
        "type": "text",
        "default": "Larizinha Store",
    },
    "store_description": {
        "label": "Descrição curta",
        "group": "Identidade",
        "type": "text",
        "default": "",
    },
    # ---- Localização ----
    "currency": {
        "label": "Moeda",
        "group": "Localização",
        "type": "choice",
        "choices": ["BRL", "USD", "EUR"],
        "default": "BRL",
    },
    "timezone": {
        "label": "Fuso horário",
        "group": "Localização",
        "type": "text",
        "default": "America/Sao_Paulo",
    },
    "language": {
        "label": "Idioma",
        "group": "Localização",
        "type": "choice",
        "choices": ["pt-BR", "en-US", "es-ES"],
        "default": "pt-BR",
    },
    "value_format": {
        "label": "Formato de valores",
        "group": "Localização",
        "type": "text",
        "default": "R$ {valor}",
    },
    # ---- Financeiro ----
    "initial_balance": {
        "label": "Saldo inicial",
        "group": "Financeiro",
        "type": "money",
        "default": "0.00",
    },
    "min_purchase": {
        "label": "Compra mínima",
        "group": "Financeiro",
        "type": "money",
        "default": "0.00",
    },
    "global_fee_percent": {
        "label": "Taxa global (%)",
        "group": "Financeiro",
        "type": "number",
        "default": "0",
    },
    # ---- Links ----
    "terms_url": {
        "label": "Link dos Termos",
        "group": "Links",
        "type": "url",
        "default": "",
    },
    "privacy_url": {
        "label": "Link da Privacidade",
        "group": "Links",
        "type": "url",
        "default": "",
    },
    "legal_notice": {
        "label": "Aviso legal",
        "group": "Links",
        "type": "text",
        "default": "",
    },
    # ---- Contato ----
    "support_whatsapp": {
        "label": "WhatsApp do suporte",
        "group": "Contato",
        "type": "text",
        "default": "",
    },
    "support_email": {
        "label": "E-mail do suporte",
        "group": "Contato",
        "type": "text",
        "default": "",
    },
    "support_username": {
        "label": "Username do suporte",
        "group": "Contato",
        "type": "text",
        "default": "",
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


async def _set_setting(key: str, value: str, category: str = "settings") -> bool:
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
        logger.debug(f"_set_setting (settings) falhou: {exc}")
        return False


def _env_default(key: str) -> Optional[str]:
    mapping = {
        "store_name": "APP_NAME",
        "currency": "DEFAULT_CURRENCY",
        "timezone": "TIMEZONE",
        "language": "DEFAULT_LANGUAGE",
        "initial_balance": "INITIAL_BALANCE",
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
    return str(v)


async def _resolve_value(key: str) -> str:
    meta = FIELDS.get(key, {})
    v = await _get_setting(key, None)
    if v is not None and str(v).strip() != "":
        return str(v)
    v_env = _env_default(key)
    if v_env is not None:
        return v_env
    return str(meta.get("default", ""))


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Coleta valores por grupo
    groups: Dict[str, Dict[str, str]] = {}
    for key, meta in FIELDS.items():
        val = await _resolve_value(key)
        if meta["type"] == "money":
            try:
                display = f"R$ {float(val or 0):.2f}"
            except Exception:
                display = val
        elif meta["type"] == "number":
            display = f"{val}%"
        else:
            display = val or "—"
        groups.setdefault(meta["group"], {})[key] = display

    ident = groups.get("Identidade", {})
    local = groups.get("Localização", {})
    fin = groups.get("Financeiro", {})
    links = groups.get("Links", {})
    contato = groups.get("Contato", {})

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "⚙️ <b>CONFIGURAÇÕES GERAIS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🏪 <b>IDENTIDADE</b>\n"
            f"├ Nome: {ident.get('store_name', '—')}\n"
            f"└ Descrição: {truncate(ident.get('store_description', '—'), 40)}\n\n"
            "🌎 <b>LOCALIZAÇÃO</b>\n"
            f"├ Moeda: {local.get('currency', '—')}\n"
            f"├ Fuso: {local.get('timezone', '—')}\n"
            f"├ Idioma: {local.get('language', '—')}\n"
            f"└ Formato: {local.get('value_format', '—')}\n\n"
            "💰 <b>FINANCEIRO</b>\n"
            f"├ Saldo inicial: {fin.get('initial_balance', '—')}\n"
            f"├ Compra mínima: {fin.get('min_purchase', '—')}\n"
            f"└ Taxa global: {fin.get('global_fee_percent', '—')}\n\n"
            "🔗 <b>LINKS</b>\n"
            f"├ Termos: {truncate(links.get('terms_url', '—'), 40)}\n"
            f"├ Privacidade: {truncate(links.get('privacy_url', '—'), 40)}\n"
            f"└ Aviso legal: {truncate(links.get('legal_notice', '—'), 40)}\n\n"
            "📞 <b>CONTATO</b>\n"
            f"├ WhatsApp: {contato.get('support_whatsapp', '—')}\n"
            f"├ E-mail: {contato.get('support_email', '—')}\n"
            f"└ Username: {contato.get('support_username', '—')}"
        ),
        nome=ident.get("store_name", "—"),
        moeda=local.get("currency", "—"),
        tz=local.get("timezone", "—"),
        lang=local.get("language", "—"),
        formato=local.get("value_format", "—"),
        saldo_ini=fin.get("initial_balance", "—"),
        limites=fin.get("min_purchase", "—"),
        taxas=fin.get("global_fee_percent", "—"),
        link_termos=links.get("terms_url", "—"),
        link_priv=links.get("privacy_url", "—"),
        avisos=truncate(links.get("legal_notice", "—"), 40),
        contato=contato.get("support_whatsapp", "—"),
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="✏️ Nome", callback_data=f"{CB_SET}:store_name"),
                InlineKeyboardButton(text="✏️ Descrição", callback_data=f"{CB_SET}:store_description"),
            ],
            [
                InlineKeyboardButton(text="✏️ Moeda", callback_data=f"{CB_SET}:currency"),
                InlineKeyboardButton(text="✏️ Fuso", callback_data=f"{CB_SET}:timezone"),
            ],
            [
                InlineKeyboardButton(text="✏️ Idioma", callback_data=f"{CB_SET}:language"),
                InlineKeyboardButton(text="✏️ Formato", callback_data=f"{CB_SET}:value_format"),
            ],
            [
                InlineKeyboardButton(text="✏️ Saldo Inicial", callback_data=f"{CB_SET}:initial_balance"),
                InlineKeyboardButton(text="✏️ Compra Mín", callback_data=f"{CB_SET}:min_purchase"),
            ],
            [
                InlineKeyboardButton(text="✏️ Taxa Global", callback_data=f"{CB_SET}:global_fee_percent"),
            ],
            [
                InlineKeyboardButton(text="✏️ Termos", callback_data=f"{CB_SET}:terms_url"),
                InlineKeyboardButton(text="✏️ Privacidade", callback_data=f"{CB_SET}:privacy_url"),
            ],
            [
                InlineKeyboardButton(text="✏️ Aviso Legal", callback_data=f"{CB_SET}:legal_notice"),
            ],
            [
                InlineKeyboardButton(text="✏️ WhatsApp", callback_data=f"{CB_SET}:support_whatsapp"),
                InlineKeyboardButton(text="✏️ E-mail", callback_data=f"{CB_SET}:support_email"),
            ],
            [
                InlineKeyboardButton(text="✏️ Username", callback_data=f"{CB_SET}:support_username"),
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
        logger.exception(f"settings on_panel falhou: {exc}")


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

        # ---- choice → mostra opções ----
        if meta["type"] == "choice":
            choices = meta.get("choices", [])
            if not choices:
                await toast_err(callback, "❌ Sem opções.")
                return
            rows_kb = []
            for c in choices:
                rows_kb.append(
                    [
                        InlineKeyboardButton(
                            text=f"• {c}",
                            callback_data=f"{CB_SET}:{field}:{c}",
                        )
                    ]
                )
            rows_kb.append(
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
            )
            text = f"✏️ <b>{meta['label']}</b>\n\nEscolha uma opção:"
            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        # ---- texto/money/number/url → pedir input ----
        hint = ""
        if meta["type"] == "money":
            hint = "Ex.: <code>10.00</code>"
        elif meta["type"] == "number":
            hint = "Ex.: <code>5</code>"
        elif meta["type"] == "url":
            hint = "Deve começar com <code>https://</code>"
        else:
            hint = "Texto livre"

        prompt = (
            f"✏️ <b>EDITAR · {meta['label']}</b>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"setcfg:{field}",
            screen=f"settings:set:{field}",
            extra={"field": field},
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"settings on_set_field falhou: {exc}")


# Callback com choice escolhido: admin:settings:set:field:value
@router.callback_query(
    F.data.startswith(f"{CB_SET}:") & F.data.regexp(r"^admin:settings:set:[a-z_]+:.+$")
)
async def on_choice_chosen(callback: CallbackQuery) -> None:
    """
    Este handler pega os cliques em opções de choice
    (formato: admin:settings:set:field:value).
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:settings:set:field:value
        if len(parts) < 6:
            return  # não é escolha

        field = parts[3]
        value = parts[4]

        meta = FIELDS.get(field)
        if meta is None or meta.get("type") != "choice":
            return

        ok = await _set_setting(field, value)
        if not ok:
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"settings.set.{field}",
            category="admin",
            details={"field": field, "value": value},
        )

        await render_panel(callback)
        await toast_ok(callback, f"✅ {meta['label']} = {value}")
    except Exception as exc:
        logger.exception(f"settings on_choice_chosen falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_settings_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting.startswith("setcfg:"):
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

    if len(val) > 500:
        await message.answer("❌ Texto muito longo.")
        return

    # Validações por tipo
    if meta["type"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        val = f"{num:.2f}"
    elif meta["type"] == "number":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        val = f"{num:g}"
    elif meta["type"] == "url":
        if not (val.startswith("http://") or val.startswith("https://")):
            await message.answer("❌ URL inválida.")
            return
    elif field == "timezone":
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(val)
        except Exception:
            await message.answer("❌ Fuso horário inválido. Ex.: America/Sao_Paulo")
            return

    ok = await _set_setting(field, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"settings.set.{field}",
        category="admin",
        details={"field": field, "value": val},
    )

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
        ),
    )


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_panel",
    "on_set_field",
    "on_choice_chosen",
    "on_settings_text_input",
    "CB_ROOT",
    "CB_SET",
    "CB_TOGGLE",
    "FIELDS",
]
