"""
Larizinha Store — Módulo Admin: Automações
Implementa o painel de controle das automações conforme DIM.

Toggles e configs:
- Boas-vindas, Programadas, Lembretes, Expiração cupons
- Atualizar estoque, Processar pagamentos, Atualizar pedidos
- Liberar comissões, Notificações saque, Recorrências
- PIX Expirado (toggle + delay)
- PIX Não Pago (toggle + delay)
- Carrinho Abandonado (toggle + delay)
- Promoção Nova (toggle + delay)
- Estoque Baixo (toggle + threshold)
- Heartbeat (toggle + timeout)
- Trava Reservada (toggle + TTL)
- Repetição de falhas (toggle)
- Editor de mensagem por automação

Integração:
- Todos os toggles e configs são lidos em runtime pelo `bot/worker.py`.

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime (Setting).
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

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
    fmt_dt,
    humanize_bool,
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
router = Router(name="admin_automations")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_automations_panel"

SCREEN_PANEL = "admin_automations_panel"

# Callbacks base
CB_ROOT = "admin:automations"
CB_TOGGLE = "admin:automations:toggle"     # :key
CB_SET = "admin:automations:set"           # :key
CB_MSG = "admin:automations:msg"           # :key
CB_RELOAD = "admin:automations:reload"

# Chaves de Setting usadas (todas as automações)
AUTOMATIONS: Dict[str, Dict[str, Any]] = {
    # --- Toggles gerais ---
    "auto_welcome": {
        "label": "Boas-vindas",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_scheduled": {
        "label": "Programadas",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_reminders": {
        "label": "Lembretes",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_coupon_expire": {
        "label": "Expiração cupons",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_update_stock": {
        "label": "Atualizar estoque",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_process_payments": {
        "label": "Processar pagamentos",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_update_orders": {
        "label": "Atualizar pedidos",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_release_commissions": {
        "label": "Liberar comissões",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_withdraw_notifications": {
        "label": "Notificações saque",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },
    "auto_recurrences": {
        "label": "Recorrências",
        "group": "Geral",
        "type": "bool",
        "default": "true",
        "worker": False,
    },

    # --- PIX Expirado ---
    "auto_pix_expired": {
        "label": "PIX Expirado",
        "group": "PIX",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_pix_expired_delay": {
        "label": "Delay (min)",
        "group": "PIX",
        "type": "int",
        "default": "30",
        "worker": True,
        "depends": "auto_pix_expired",
    },

    # --- PIX Não Pago ---
    "auto_pix_unpaid": {
        "label": "PIX Não Pago",
        "group": "PIX",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_pix_unpaid_delay": {
        "label": "Delay (min)",
        "group": "PIX",
        "type": "int",
        "default": "60",
        "worker": True,
        "depends": "auto_pix_unpaid",
    },

    # --- Carrinho Abandonado ---
    "auto_cart_abandoned": {
        "label": "Carrinho Abandonado",
        "group": "Reengajamento",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_cart_abandoned_delay": {
        "label": "Delay (min)",
        "group": "Reengajamento",
        "type": "int",
        "default": "15",
        "worker": True,
        "depends": "auto_cart_abandoned",
    },

    # --- Promoção Nova ---
    "auto_new_promo": {
        "label": "Promoção Nova",
        "group": "Reengajamento",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_new_promo_delay": {
        "label": "Delay (min)",
        "group": "Reengajamento",
        "type": "int",
        "default": "5",
        "worker": True,
        "depends": "auto_new_promo",
    },

    # --- Estoque Baixo ---
    "auto_low_stock": {
        "label": "Estoque Baixo",
        "group": "Estoque",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_low_stock_threshold": {
        "label": "Threshold (unid.)",
        "group": "Estoque",
        "type": "int",
        "default": "5",
        "worker": True,
        "depends": "auto_low_stock",
    },

    # --- Heartbeat ---
    "auto_heartbeat": {
        "label": "Heartbeat",
        "group": "Sistema",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "auto_heartbeat_timeout": {
        "label": "Timeout (s)",
        "group": "Sistema",
        "type": "int",
        "default": "120",
        "worker": True,
        "depends": "auto_heartbeat",
    },

    # --- Trava Reservada ---
    "enable_concurrency_lock": {
        "label": "Trava Reservada",
        "group": "Sistema",
        "type": "bool",
        "default": "true",
        "worker": True,
    },
    "product_lock_ttl": {
        "label": "Lock TTL (s)",
        "group": "Sistema",
        "type": "int",
        "default": "300",
        "worker": True,
        "depends": "enable_concurrency_lock",
    },

    # --- Repetição de falhas ---
    "auto_retry_failures": {
        "label": "Repetição de Falhas",
        "group": "Sistema",
        "type": "bool",
        "default": "true",
        "worker": False,
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
                session.add(Setting(key=key, value=value, category="automations"))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting (automations) falhou: {exc}")
        return False


async def _resolve_value(key: str) -> str:
    meta = AUTOMATIONS.get(key, {})
    v = await _get_setting(key, None)
    if v is not None and str(v).strip() != "":
        return str(v)
    return str(meta.get("default", ""))


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Agrupa por grupo
    groups: Dict[str, Dict[str, str]] = {}
    for key, meta in AUTOMATIONS.items():
        val = await _resolve_value(key)
        if meta["type"] == "bool":
            display = SYM_OK if str(val).lower() == "true" else SYM_OFF
        else:
            display = val
        groups.setdefault(meta["group"], {})[key] = display

    ger = groups.get("Geral", {})
    pix = groups.get("PIX", {})
    reeng = groups.get("Reengajamento", {})
    est = groups.get("Estoque", {})
    sis = groups.get("Sistema", {})

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "🧠 <b>AUTOMAÇÕES</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚙️ <b>GERAL</b>\n"
            f"├ Boas-vindas: {ger.get('auto_welcome', '—')}\n"
            f"├ Programadas: {ger.get('auto_scheduled', '—')}\n"
            f"├ Lembretes: {ger.get('auto_reminders', '—')}\n"
            f"├ Expiração cupons: {ger.get('auto_coupon_expire', '—')}\n"
            f"├ Atualizar estoque: {ger.get('auto_update_stock', '—')}\n"
            f"├ Processar pagamentos: {ger.get('auto_process_payments', '—')}\n"
            f"├ Atualizar pedidos: {ger.get('auto_update_orders', '—')}\n"
            f"├ Liberar comissões: {ger.get('auto_release_commissions', '—')}\n"
            f"├ Notificações saque: {ger.get('auto_withdraw_notifications', '—')}\n"
            f"└ Recorrências: {ger.get('auto_recurrences', '—')}\n\n"
            "💠 <b>PIX</b>\n"
            f"├ PIX Expirado: {pix.get('auto_pix_expired', '—')} "
            f"— delay {pix.get('auto_pix_expired_delay', '—')}min\n"
            f"└ PIX Não Pago: {pix.get('auto_pix_unpaid', '—')} "
            f"— delay {pix.get('auto_pix_unpaid_delay', '—')}min\n\n"
            "🔄 <b>REENGAJAMENTO</b>\n"
            f"├ Carrinho Abandonado: {reeng.get('auto_cart_abandoned', '—')} "
            f"— delay {reeng.get('auto_cart_abandoned_delay', '—')}min\n"
            f"└ Promoção Nova: {reeng.get('auto_new_promo', '—')} "
            f"— delay {reeng.get('auto_new_promo_delay', '—')}min\n\n"
            "📦 <b>ESTOQUE</b>\n"
            f"└ Estoque Baixo: {est.get('auto_low_stock', '—')} "
            f"— threshold {est.get('auto_low_stock_threshold', '—')}\n\n"
            "🧩 <b>SISTEMA</b>\n"
            f"├ Heartbeat: {sis.get('auto_heartbeat', '—')} "
            f"— timeout {sis.get('auto_heartbeat_timeout', '—')}s\n"
            f"├ Trava Reservada: {sis.get('enable_concurrency_lock', '—')} "
            f"— TTL {sis.get('product_lock_ttl', '—')}s\n"
            f"└ Repetição Falhas: {sis.get('auto_retry_failures', '—')}"
        ),
        welcome=ger.get("auto_welcome", "—"),
        scheduled=ger.get("auto_scheduled", "—"),
        reminders=ger.get("auto_reminders", "—"),
        coupon_expire=ger.get("auto_coupon_expire", "—"),
        update_stock=ger.get("auto_update_stock", "—"),
        process_payments=ger.get("auto_process_payments", "—"),
        update_orders=ger.get("auto_update_orders", "—"),
        release_comm=ger.get("auto_release_commissions", "—"),
        withdraw_not=ger.get("auto_withdraw_notifications", "—"),
        recurrences=ger.get("auto_recurrences", "—"),
        pix_exp=pix.get("auto_pix_expired", "—"),
        pix_exp_delay=pix.get("auto_pix_expired_delay", "—"),
        pix_unpaid=pix.get("auto_pix_unpaid", "—"),
        pix_unpaid_delay=pix.get("auto_pix_unpaid_delay", "—"),
        cart=reeng.get("auto_cart_abandoned", "—"),
        cart_delay=reeng.get("auto_cart_abandoned_delay", "—"),
        promo=reeng.get("auto_new_promo", "—"),
        promo_delay=reeng.get("auto_new_promo_delay", "—"),
        low_stock=est.get("auto_low_stock", "—"),
        threshold=est.get("auto_low_stock_threshold", "—"),
        heartbeat=sis.get("auto_heartbeat", "—"),
        timeout=sis.get("auto_heartbeat_timeout", "—"),
        lock=sis.get("enable_concurrency_lock", "—"),
        lock_ttl=sis.get("product_lock_ttl", "—"),
        retry=sis.get("auto_retry_failures", "—"),
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="🔄 Boas-vindas", callback_data=f"{CB_TOGGLE}:auto_welcome"),
                    InlineKeyboardButton(text="🔄 Programadas", callback_data=f"{CB_TOGGLE}:auto_scheduled"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Lembretes", callback_data=f"{CB_TOGGLE}:auto_reminders"),
                    InlineKeyboardButton(text="🔄 Cupons", callback_data=f"{CB_TOGGLE}:auto_coupon_expire"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Estoque", callback_data=f"{CB_TOGGLE}:auto_update_stock"),
                    InlineKeyboardButton(text="🔄 Pagamentos", callback_data=f"{CB_TOGGLE}:auto_process_payments"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Pedidos", callback_data=f"{CB_TOGGLE}:auto_update_orders"),
                    InlineKeyboardButton(text="🔄 Comissões", callback_data=f"{CB_TOGGLE}:auto_release_commissions"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Notif. Saque", callback_data=f"{CB_TOGGLE}:auto_withdraw_notifications"),
                    InlineKeyboardButton(text="🔄 Recorrências", callback_data=f"{CB_TOGGLE}:auto_recurrences"),
                ],
                [
                    InlineKeyboardButton(text="🔄 PIX Exp", callback_data=f"{CB_TOGGLE}:auto_pix_expired"),
                    InlineKeyboardButton(text="✏️ Delay", callback_data=f"{CB_SET}:auto_pix_expired_delay"),
                ],
                [
                    InlineKeyboardButton(text="🔄 PIX Não Pago", callback_data=f"{CB_TOGGLE}:auto_pix_unpaid"),
                    InlineKeyboardButton(text="✏️ Delay", callback_data=f"{CB_SET}:auto_pix_unpaid_delay"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Carrinho", callback_data=f"{CB_TOGGLE}:auto_cart_abandoned"),
                    InlineKeyboardButton(text="✏️ Delay", callback_data=f"{CB_SET}:auto_cart_abandoned_delay"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Promo", callback_data=f"{CB_TOGGLE}:auto_new_promo"),
                    InlineKeyboardButton(text="✏️ Delay", callback_data=f"{CB_SET}:auto_new_promo_delay"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Estoque Baixo", callback_data=f"{CB_TOGGLE}:auto_low_stock"),
                    InlineKeyboardButton(text="✏️ Threshold", callback_data=f"{CB_SET}:auto_low_stock_threshold"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Heartbeat", callback_data=f"{CB_TOGGLE}:auto_heartbeat"),
                    InlineKeyboardButton(text="✏️ Timeout", callback_data=f"{CB_SET}:auto_heartbeat_timeout"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Trava", callback_data=f"{CB_TOGGLE}:enable_concurrency_lock"),
                    InlineKeyboardButton(text="✏️ Lock TTL", callback_data=f"{CB_SET}:product_lock_ttl"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Retry Falhas", callback_data=f"{CB_TOGGLE}:auto_retry_failures"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Recarregar", callback_data=CB_RELOAD),
                    InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
                ],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data.in_({CB_ROOT, CB_RELOAD}))
async def on_panel(callback: CallbackQuery) -> None:
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
        logger.exception(f"automations on_panel falhou: {exc}")


# ============================================================
# TOGGLE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Chave inválida.")
            return

        meta = AUTOMATIONS.get(key)
        if meta is None or meta["type"] != "bool":
            await toast_err(callback, "❌ Chave inválida.")
            return

        current = await _resolve_value(key)
        new_val = "false" if str(current).lower() == "true" else "true"
        ok = await _set_setting(key, new_val)
        if not ok:
            await toast_err(callback, "❌ Erro ao salvar.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"automation.toggle.{key}",
            category="admin",
            details={"key": key, "value": new_val, "worker": meta.get("worker", False)},
        )

        await render_panel(callback)
        status_emoji = SYM_OK if new_val == "true" else SYM_OFF
        await toast_ok(callback, f"🔄 {meta['label']}: {status_emoji}")
    except Exception as exc:
        logger.exception(f"automations on_toggle falhou: {exc}")


# ============================================================
# SET (delay / threshold / timeout / ttl)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Chave inválida.")
            return

        meta = AUTOMATIONS.get(key)
        if meta is None or meta["type"] != "int":
            await toast_err(callback, "❌ Chave inválida.")
            return

        current = await _resolve_value(key)
        label = meta["label"]

        # Dicas por tipo de config
        hint = "Ex.: <code>30</code>"
        if key.endswith("_delay"):
            hint = "Tempo em minutos. Ex.: <code>15</code>"
        elif key == "auto_low_stock_threshold":
            hint = "Quantidade de unidades. Ex.: <code>5</code>"
        elif key == "auto_heartbeat_timeout":
            hint = "Tempo em segundos. Ex.: <code>120</code>"
        elif key == "product_lock_ttl":
            hint = "TTL em segundos. Ex.: <code>300</code>"

        prompt = (
            f"✏️ <b>EDITAR · {label}</b>\n\n"
            f"Valor atual: <b>{current}</b>\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"automation_set:{key}",
            screen=f"automations:set:{key}",
            extra={"key": key},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"automations on_set falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_automations_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting.startswith("automation_set:"):
        key = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_config(message, admin_id, key, value)
        return

    # Editor de mensagem da automação
    if awaiting.startswith("automation_msg:"):
        key = awaiting.split(":", 1)[1]
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _save_message_key(message, admin_id, key, value)
        return


async def _apply_config(
    message: Message,
    admin_id: int,
    key: str,
    value: str,
) -> None:
    meta = AUTOMATIONS.get(key)
    if meta is None:
        await message.answer("❌ Chave inválida.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    # Validação
    if meta["type"] == "int":
        num = parse_int(val)
        if num is None or num < 0:
            await message.answer("❌ Número inválido.")
            return
        # Limites
        if num > 100000:
            await message.answer("❌ Valor muito alto.")
            return
        val = str(int(num))

    ok = await _set_setting(key, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"automation.set.{key}",
        category="admin",
        details={"key": key, "value": val, "worker": meta.get("worker", False)},
    )

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado para <code>{val}</code>.",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
        ),
    )


# ============================================================
# EDITAR MENSAGEM DA AUTOMAÇÃO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_MSG}:"))
async def on_msg(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            key = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Chave inválida.")
            return

        meta = AUTOMATIONS.get(key)
        if meta is None:
            await toast_err(callback, "❌ Chave inválida.")
            return

        prompt = (
            f"✏️ <b>KEY DA MENSAGEM</b>\n\n"
            f"Automação: <b>{meta['label']}</b>\n\n"
            "Envie a <b>key</b> da mensagem usada por esta automação\n"
            "(ex.: <code>carrinho</code>, <code>pix_naopago</code>).\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"automation_msg:{key}",
            screen=f"automations:msg:{key}",
            extra={"key": key},
            use_force_reply=True,
            reply_placeholder="chave_da_msg",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"automations on_msg falhou: {exc}")


async def _save_message_key(
    message: Message,
    admin_id: int,
    key: str,
    value: str,
) -> None:
    val = (value or "").strip().lower().replace(" ", "_")
    if not val:
        await message.answer("❌ Key vazia.")
        return

    setting_key = f"{key}_message_key"
    ok = await _set_setting(setting_key, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"automation.msg.{key}",
        category="admin",
        details={"key": key, "message_key": val},
    )

    await message.answer(
        f"✅ Key da mensagem atualizada para <code>{val}</code>.",
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
    "on_toggle",
    "on_set",
    "on_automations_text_input",
    "on_msg",
    "CB_ROOT",
    "CB_TOGGLE",
    "CB_SET",
    "CB_MSG",
    "CB_RELOAD",
    "AUTOMATIONS",
]
