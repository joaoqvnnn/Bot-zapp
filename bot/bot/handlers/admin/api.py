"""
Larizinha Store — Módulo Admin: API e Integrações
Implementa o painel de integrações externas conforme DIM.

Telas:
- Painel de integrações (Telegram, Gateway, API PIX, Webhooks)
- Status de cada serviço (🟢/🔴) em tempo real
- Editor de chaves de API (mascaradas)
- Editor de webhooks
- Editor de timeout / reconexão
- Ver últimas requisições (log histórico em memória do Redis)
- Ver erros recentes (security_events com categoria integration)
- Testar conexões (MP, SMTP, Telegram)
- Reconectar (reinicializar clientes HTTP)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel.
- Todos os dados vêm do banco em runtime (Setting + .env fallback).
- Toda ação crítica é auditada (log_action).
- Chaves sensíveis são SEMPRE mascaradas na exibição.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import SecurityEvent, Setting, session_scope
from bot.logger import logger
from bot.redis_client import Keys, get_redis
from bot.services.message_engine import message_engine
from bot.services.pix_service import pix_service
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    AdminFlow,
    SYM_ERR,
    SYM_OK,
    SYM_WARN,
    answer,
    ask_input,
    consume_input,
    edit_or_send,
    fmt_dt,
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
router = Router(name="admin_api")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_api_panel"
MSG_KEY_REQUESTS = "admin_api_requests"
MSG_KEY_ERRORS = "admin_api_errors"

SCREEN_PANEL = "admin_api_panel"

# Callbacks base
CB_ROOT = "admin:api"
CB_RELOAD = "admin:api:reload"
CB_SET = "admin:api:set"                    # :field
CB_REVEAL = "admin:api:reveal"              # :field
CB_TEST = "admin:api:test"                  # :service
CB_RECONNECT = "admin:api:reconnect"        # :service
CB_RECONNECT_YES = "admin:api:reconnect:yes"  # :service
CB_REQUESTS = "admin:api:requests"          # :page:N
CB_ERRORS = "admin:api:errors"
CB_LOGS_CLEAR = "admin:api:logs:clear"

# Serviços monitorados
SERVICES: Dict[str, str] = {
    "telegram": "🔗 Telegram Bot API",
    "mercadopago": "💳 Mercado Pago (PIX)",
    "smtp": "📧 SMTP (E-mail)",
    "redis": "⚡ Redis",
    "postgres": "🗄️ PostgreSQL",
}

# Campos configuráveis (Setting keys)
FIELDS: Dict[str, Dict[str, Any]] = {
    # --- Mercado Pago ---
    "mp_access_token": {
        "label": "MP Access Token",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "env_attr": "MP_ACCESS_TOKEN",
        "default": "",
    },
    "mp_public_key": {
        "label": "MP Public Key",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "env_attr": "MP_PUBLIC_KEY",
        "default": "",
    },
    "mp_webhook_secret": {
        "label": "MP Webhook Secret",
        "group": "Mercado Pago",
        "sensitive": True,
        "type": "text",
        "env_attr": "MP_WEBHOOK_SECRET",
        "default": "",
    },
    "mp_webhook_url": {
        "label": "MP Webhook URL",
        "group": "Mercado Pago",
        "sensitive": False,
        "type": "url",
        "env_attr": "MP_WEBHOOK_URL",
        "default": "",
    },
    # --- Webhooks ---
    "telegram_webhook_url": {
        "label": "Webhook Telegram",
        "group": "Webhooks",
        "sensitive": False,
        "type": "url",
        "env_attr": "TELEGRAM_WEBHOOK_URL",
        "default": "",
    },
    "webhook_secret_token": {
        "label": "Webhook Secret Token",
        "group": "Webhooks",
        "sensitive": True,
        "type": "text",
        "env_attr": "WEBHOOK_SECRET_TOKEN",
        "default": "",
    },
    # --- Rede ---
    "api_timeout": {
        "label": "Timeout (s)",
        "group": "Rede",
        "sensitive": False,
        "type": "int",
        "default": "20",
    },
    "api_max_retries": {
        "label": "Máx. tentativas",
        "group": "Rede",
        "sensitive": False,
        "type": "int",
        "default": "3",
    },
    # --- API Interna ---
    "api_base_url": {
        "label": "API Base URL",
        "group": "API Interna",
        "sensitive": False,
        "type": "url",
        "env_attr": "API_BASE_URL",
        "default": "",
    },
    "webapp_url": {
        "label": "WebApp URL",
        "group": "API Interna",
        "sensitive": False,
        "type": "url",
        "env_attr": "WEBAPP_URL",
        "default": "",
    },
}


# ============================================================
# 🧰 HELPERS
# ============================================================
def _mask(value: Optional[str], keep: int = 4) -> str:
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


async def _set_setting(key: str, value: str) -> bool:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Setting).where(Setting.key == key))
            s = res.scalar_one_or_none()
            if s is None:
                session.add(Setting(key=key, value=value, category="api"))
            else:
                s.value = value
                session.add(s)
        return True
    except Exception as exc:
        logger.debug(f"_set_setting (api) falhou: {exc}")
        return False


def _env_value(key: str) -> Optional[str]:
    meta = FIELDS.get(key, {})
    attr = meta.get("env_attr")
    if not attr:
        return None
    try:
        v = getattr(settings, attr, None)
    except Exception:
        return None
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


async def _resolve_value(key: str) -> str:
    meta = FIELDS.get(key, {})
    v = await _get_setting(key, None)
    if v is not None and str(v).strip() != "":
        return str(v)
    v_env = _env_value(key)
    if v_env is not None:
        return v_env
    return str(meta.get("default", ""))


async def _check_telegram() -> bool:
    """Verifica se o bot consegue acessar getMe."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe"
            )
            return r.status_code == 200
    except Exception:
        return False


async def _check_mp() -> bool:
    try:
        return await pix_service.healthcheck()
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        r = await get_redis()
        return bool(await r.ping())
    except Exception:
        return False


async def _check_postgres() -> bool:
    try:
        from bot.database import healthcheck as db_health

        return await db_health()
    except Exception:
        return False


async def _check_smtp() -> bool:
    try:
        host = await _resolve_value("smtp_host")
        port_raw = await _resolve_value("smtp_port")
        if not host:
            return False
        try:
            port = int(port_raw or 587)
        except Exception:
            port = 587
        # Só testa TCP (não envia e-mail)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=8,
        )
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _status_map() -> Dict[str, bool]:
    """Retorna o status de cada serviço (paralelo)."""
    results = await asyncio.gather(
        _check_telegram(),
        _check_mp(),
        _check_redis(),
        _check_postgres(),
        _check_smtp(),
        return_exceptions=True,
    )
    return {
        "telegram": bool(results[0]) if not isinstance(results[0], Exception) else False,
        "mercadopago": bool(results[1]) if not isinstance(results[1], Exception) else False,
        "redis": bool(results[2]) if not isinstance(results[2], Exception) else False,
        "postgres": bool(results[3]) if not isinstance(results[3], Exception) else False,
        "smtp": bool(results[4]) if not isinstance(results[4], Exception) else False,
    }


async def _log_api_request(
    service: str,
    endpoint: str,
    status: int,
    success: bool,
    elapsed_ms: int,
) -> None:
    """Registra requisição no Redis (lista rotativa)."""
    try:
        r = await get_redis()
        key = f"{settings.REDIS_PREFIX}:api:requests"
        entry = json.dumps(
            {
                "service": service,
                "endpoint": truncate(endpoint, 100),
                "status": status,
                "success": success,
                "ms": elapsed_ms,
                "ts": int(datetime.now(timezone.utc).timestamp()),
            },
            default=str,
        )
        pipe = r.pipeline()
        pipe.lpush(key, entry)
        pipe.ltrim(key, 0, 199)  # últimos 200
        pipe.expire(key, 86400 * 7)  # 7 dias
        await pipe.execute()
    except Exception:
        pass


async def _get_recent_requests(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        r = await get_redis()
        key = f"{settings.REDIS_PREFIX}:api:requests"
        raws = await r.lrange(key, 0, limit - 1)
        out = []
        for raw in raws:
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
        return out
    except Exception:
        return []


async def _get_recent_errors(limit: int = 20) -> List[SecurityEvent]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(SecurityEvent)
                .where(SecurityEvent.type.like("%integration%"))
                .order_by(SecurityEvent.created_at.desc())
                .limit(limit)
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    statuses = await _status_map()

    # Coleta valores
    lines = ["🔌 <b>API E INTEGRAÇÕES</b>", ""]
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🔗 <b>Status dos serviços</b>")
    for key, label in SERVICES.items():
        em = SYM_OK if statuses.get(key) else SYM_ERR
        lines.append(f"{em} {label}")

    lines.append("")
    lines.append("💳 <b>MERCADO PAGO</b>")
    for k in ("mp_access_token", "mp_public_key", "mp_webhook_url"):
        meta = FIELDS[k]
        v = await _resolve_value(k)
        disp = _mask(v) if meta["sensitive"] else (truncate(v, 50) or "—")
        lines.append(f"├ {meta['label']}: <code>{disp}</code>")

    lines.append("")
    lines.append("🪝 <b>WEBHOOKS</b>")
    tg_wh = await _resolve_value("telegram_webhook_url")
    lines.append(f"├ Telegram: <code>{truncate(tg_wh, 45) or '—'}</code>")

    lines.append("")
    lines.append("🌐 <b>API INTERNA</b>")
    api_url = await _resolve_value("api_base_url")
    webapp_url = await _resolve_value("webapp_url")
    lines.append(f"├ API: <code>{truncate(api_url, 45) or '—'}</code>")
    lines.append(f"└ WebApp: <code>{truncate(webapp_url, 45) or '—'}</code>")

    lines.append("")
    lines.append("⏱️ <b>REDE</b>")
    timeout_v = await _resolve_value("api_timeout")
    retries_v = await _resolve_value("api_max_retries")
    lines.append(f"├ Timeout: <b>{timeout_v}s</b>")
    lines.append(f"└ Máx. tentativas: <b>{retries_v}</b>")

    text = "\n".join(lines)

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=text,
        status_tg=SYM_OK if statuses.get("telegram") else SYM_ERR,
        status_mp=SYM_OK if statuses.get("mercadopago") else SYM_ERR,
        status_api=SYM_OK if statuses.get("redis") and statuses.get("postgres") else SYM_WARN,
        status_db=SYM_OK if statuses.get("postgres") else SYM_ERR,
        status_redis=SYM_OK if statuses.get("redis") else SYM_ERR,
        status_pgto=SYM_OK if statuses.get("mercadopago") else SYM_ERR,
        status_smtp=SYM_OK if statuses.get("smtp") else SYM_ERR,
        chaves="—",
        status="OK",
        req=fmt_num(0),
        erros=fmt_num(0),
        timeout=timeout_v,
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="🔗 Telegram",
                        callback_data=f"{CB_TEST}:telegram",
                    ),
                    InlineKeyboardButton(
                        text="💳 MP",
                        callback_data=f"{CB_TEST}:mercadopago",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📧 SMTP",
                        callback_data=f"{CB_TEST}:smtp",
                    ),
                    InlineKeyboardButton(
                        text="⚡ Redis",
                        callback_data=f"{CB_TEST}:redis",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🗄️ PostgreSQL",
                        callback_data=f"{CB_TEST}:postgres",
                    ),
                    InlineKeyboardButton(
                        text="🧪 Testar Todos",
                        callback_data=f"{CB_TEST}:all",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔑 MP Access",
                        callback_data=f"{CB_SET}:mp_access_token",
                    ),
                    InlineKeyboardButton(
                        text="🔑 MP Public",
                        callback_data=f"{CB_SET}:mp_public_key",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🪝 MP Webhook",
                        callback_data=f"{CB_SET}:mp_webhook_url",
                    ),
                    InlineKeyboardButton(
                        text="🔐 Secret",
                        callback_data=f"{CB_SET}:webhook_secret_token",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🪝 Webhook TG",
                        callback_data=f"{CB_SET}:telegram_webhook_url",
                    ),
                    InlineKeyboardButton(
                        text="🌐 API URL",
                        callback_data=f"{CB_SET}:api_base_url",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🖥️ WebApp URL",
                        callback_data=f"{CB_SET}:webapp_url",
                    ),
                    InlineKeyboardButton(
                        text="⏱️ Timeout",
                        callback_data=f"{CB_SET}:api_timeout",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🔢 Retries",
                        callback_data=f"{CB_SET}:api_max_retries",
                    ),
                    InlineKeyboardButton(
                        text="🔄 Reconectar",
                        callback_data=f"{CB_RECONNECT}:all",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📜 Requisições",
                        callback_data=f"{CB_REQUESTS}:1",
                    ),
                    InlineKeyboardButton(
                        text="⚠️ Erros",
                        callback_data=CB_ERRORS,
                    ),
                ],
                [
                    InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_RELOAD),
                    InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
                ],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


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
        logger.exception(f"api on_root falhou: {exc}")


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
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
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

        hint = ""
        if meta["type"] == "int":
            hint = "Ex.: <code>20</code>"
        elif meta["type"] == "url":
            hint = "Deve começar com <code>https://</code>"
        else:
            hint = "Valor em texto"

        warn = ""
        if meta["sensitive"]:
            warn = "\n⚠️ <b>Valor sensível</b> — será mascarado após salvar."

        prompt = (
            f"✏️ <b>EDITAR · {meta['label']}</b>\n\n"
            f"{hint}\n{warn}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"apiset:{field}",
            screen=f"api:set:{field}",
            extra={"field": field},
            use_force_reply=True,
            reply_placeholder=meta["label"],
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"api on_set_field falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_api_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting.startswith("apiset:"):
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

    if meta["type"] == "int":
        num = parse_int(val)
        if num is None or num < 0 or num > 300:
            await message.answer("❌ Número inválido (0-300).")
            return
        val = str(int(num))
    elif meta["type"] == "url":
        if not (val.startswith("http://") or val.startswith("https://")):
            await message.answer("❌ URL inválida (deve começar com https://).")
            return

    ok = await _set_setting(field, val)
    if not ok:
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"api.set.{field}",
        category="admin",
        details={"field": field, "sensitive": meta.get("sensitive", False)},
    )

    display = _mask(val) if meta.get("sensitive") else truncate(val, 60)

    await message.answer(
        f"✅ <b>{meta['label']}</b> atualizado.\n\n"
        f"Novo valor: <code>{display}</code>",
        parse_mode="HTML",
        reply_markup=kb(
            [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
        ),
    )


# ============================================================
# REVELAR (só Admin Principal)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REVEAL}:"))
async def on_reveal(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            field = callback.data.split(":", 2)[-1]
        except Exception:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = FIELDS.get(field)
        if meta is None or not meta.get("sensitive"):
            await toast_err(callback, "❌ Campo inválido.")
            return

        val = await _resolve_value(field)
        await callback.answer(
            text=f"🔓 {meta['label']}:\n{(val or '')[:180]}",
            show_alert=True,
        )

        await log_action(
            admin_id=admin_id,
            action=f"api.reveal.{field}",
            category="security",
            details={"field": field},
        )
    except Exception as exc:
        logger.exception(f"api on_reveal falhou: {exc}")


# ============================================================
# TESTAR CONEXÃO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TEST}:"))
async def on_test(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            service = callback.data.split(":")[-1]
        except Exception:
            await toast_err(callback, "❌ Serviço inválido.")
            return

        await toast_ok(callback, "🧪 Testando...")

        details: List[str] = []
        ok_count = 0

        if service == "telegram" or service == "all":
            ok = await _check_telegram()
            details.append(f"🔗 Telegram: {SYM_OK if ok else SYM_ERR}")
            if ok:
                ok_count += 1
        if service == "mercadopago" or service == "all":
            ok = await _check_mp()
            details.append(f"💳 Mercado Pago: {SYM_OK if ok else SYM_ERR}")
            if ok:
                ok_count += 1
        if service == "smtp" or service == "all":
            ok = await _check_smtp()
            details.append(f"📧 SMTP: {SYM_OK if ok else SYM_ERR}")
            if ok:
                ok_count += 1
        if service == "redis" or service == "all":
            ok = await _check_redis()
            details.append(f"⚡ Redis: {SYM_OK if ok else SYM_ERR}")
            if ok:
                ok_count += 1
        if service == "postgres" or service == "all":
            ok = await _check_postgres()
            details.append(f"🗄️ PostgreSQL: {SYM_OK if ok else SYM_ERR}")
            if ok:
                ok_count += 1

        if not details:
            details.append("❌ Serviço desconhecido")

        total_services = 5 if service == "all" else 1
        overall = ok_count == total_services

        await log_action(
            admin_id=admin_id,
            action=f"api.test.{service}",
            category="admin",
            details={"service": service, "ok_count": ok_count, "overall": overall},
        )

        text = (
            f"🧪 <b>RESULTADO DO TESTE · {service.upper()}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(details)
            + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + f"{'✅ Todos OK' if overall else '⚠️ Alguns falharam'}"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🔄 Testar Novamente",
                            callback_data=f"{CB_TEST}:{service}",
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
    except Exception as exc:
        logger.exception(f"api on_test falhou: {exc}")


# ============================================================
# RECONECTAR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RECONNECT}:"))
async def on_reconnect(callback: CallbackQuery) -> None:
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
            service = parts[-1]
        except Exception:
            service = "all"

        text = (
            f"🔄 <b>RECONECTAR</b>\n\n"
            f"Serviço: <b>{service.upper()}</b>\n\n"
            "Isso reinicia os clientes HTTP e limpa caches.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_RECONNECT_YES}:{service}",
            cancel_callback=CB_ROOT,
            confirm_text="🔄 Sim, reconectar",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"api on_reconnect falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_RECONNECT_YES}:"))
async def on_reconnect_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            service = callback.data.split(":")[-1]
        except Exception:
            service = "all"

        # Reconexões
        if service in ("mercadopago", "all"):
            try:
                await pix_service.close()
            except Exception as exc:
                logger.debug(f"close mp falhou: {exc}")

        if service in ("redis", "all"):
            try:
                from bot.redis_client import close_redis, get_redis as _get_redis

                await close_redis()
                _ = await _get_redis()
            except Exception as exc:
                logger.debug(f"reconnect redis falhou: {exc}")

        if service in ("postgres", "all"):
            try:
                from bot.database import get_engine

                engine = get_engine()
                try:
                    await engine.dispose()
                except Exception:
                    pass
            except Exception as exc:
                logger.debug(f"reconnect db falhou: {exc}")

        # Verifica
        statuses = await _status_map()
        ok_list = [k for k, v in statuses.items() if v]
        fail_list = [k for k, v in statuses.items() if not v]

        details = [
            f"✅ OK: {', '.join(ok_list) or '—'}",
            f"❌ Falhas: {', '.join(fail_list) or '—'}",
        ]

        await log_action(
            admin_id=admin_id,
            action=f"api.reconnect.{service}",
            category="admin",
            details={"service": service, "ok": ok_list, "fail": fail_list},
        )

        text = (
            f"🔄 <b>RECONEXÃO CONCLUÍDA</b>\n\n"
            + "\n".join(details)
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await toast_ok(callback, "🔄 Reconectado.")
    except Exception as exc:
        logger.exception(f"api on_reconnect_confirm falhou: {exc}")


# ============================================================
# REQUISIÇÕES RECENTES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REQUESTS}:"))
async def on_requests(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        items = await _get_recent_requests(limit=200)

        if not items:
            text = (
                "📜 <b>REQUISIÇÕES RECENTES</b>\n\n"
                "<i>Nenhuma requisição registrada.</i>"
            )
        else:
            # Paginação manual
            per_page = 15
            total_p = max(1, (len(items) + per_page - 1) // per_page)
            page = max(1, min(page, total_p))
            start = (page - 1) * per_page
            page_items = items[start:start + per_page]

            lines = [
                f"📜 <b>REQUISIÇÕES RECENTES</b> ({len(items)})",
                f"Página {page}/{total_p}",
                "",
            ]
            for it in page_items:
                em = SYM_OK if it.get("success") else SYM_ERR
                ts = it.get("ts", 0)
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                except Exception:
                    dt = None
                lines.append(
                    f"{em} <b>{it.get('service')}</b> · "
                    f"HTTP {it.get('status')} · {it.get('ms')}ms\n"
                    f"    <code>{truncate(it.get('endpoint'), 45)}</code>\n"
                    f"    {fmt_dt(dt)}"
                )
            text = "\n".join(lines)

            pag_kb = build_pagination_row(
                current_page=page,
                total_pages=total_p,
                base_callback=CB_REQUESTS,
                back_callback=CB_ROOT,
            )
            rows_kb = []
            if pag_kb:
                rows_kb.append(pag_kb)
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text="🧹 Limpar",
                        callback_data=CB_LOGS_CLEAR,
                    )
                ]
            )
            rows_kb.append(
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]
            )

            await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
            await answer(callback)
            return

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🧹 Limpar",
                            callback_data=CB_LOGS_CLEAR,
                        )
                    ],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
                ]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"api on_requests falhou: {exc}")


@router.callback_query(F.data == CB_LOGS_CLEAR)
async def on_logs_clear(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        if admin_id != settings.MAIN_ADMIN_ID:
            await toast_err(callback, "❌ Apenas o Admin Principal.")
            return

        try:
            r = await get_redis()
            key = f"{settings.REDIS_PREFIX}:api:requests"
            await r.delete(key)
            await toast_ok(callback, "🧹 Histórico limpo.")
        except Exception as exc:
            logger.debug(f"clear api logs falhou: {exc}")
            await toast_err(callback, "❌ Erro ao limpar.")

        await log_action(
            admin_id=admin_id,
            action="api.clear_requests_log",
            category="admin",
        )
    except Exception as exc:
        logger.exception(f"api on_logs_clear falhou: {exc}")


# ============================================================
# ERROS
# ============================================================
@router.callback_query(F.data == CB_ERRORS)
async def on_errors(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        items = await _get_recent_errors(limit=20)

        if not items:
            text = (
                "⚠️ <b>ERROS DE INTEGRAÇÃO</b>\n\n"
                "✅ <i>Nenhum erro recente.</i>"
            )
        else:
            lines = [f"⚠️ <b>ERROS DE INTEGRAÇÃO ({len(items)})</b>", ""]
            for e in items:
                lines.append(
                    f"🔴 <code>{truncate(e.type or '—', 30)}</code>\n"
                    f"    {truncate(e.details and str(e.details) or '—', 60)}\n"
                    f"    {fmt_dt(e.created_at)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"api on_errors falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_root",
    "on_set_field",
    "on_api_text_input",
    "on_reveal",
    "on_test",
    "on_reconnect",
    "on_reconnect_confirm",
    "on_requests",
    "on_logs_clear",
    "on_errors",
    "CB_ROOT",
    "CB_RELOAD",
    "CB_SET",
    "CB_REVEAL",
    "CB_TEST",
    "CB_RECONNECT",
    "CB_RECONNECT_YES",
    "CB_REQUESTS",
    "CB_ERRORS",
    "CB_LOGS_CLEAR",
    "SERVICES",
    "FIELDS",
    "_log_api_request",
]
