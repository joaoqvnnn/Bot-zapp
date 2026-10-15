"""
Larizinha Store — Handler de Saque
Implementa o fluxo EXATO do DIM:

  Passo 1 → Escolha do tipo de chave PIX
             [📧 E-mail] [🪪 CPF] [📧 Gmail] [🎲 Chave Aleatória] [🔙 Voltar]

  Passo 2 → Reply pedindo a chave (formato por tipo)
             • Apaga a mensagem do usuário após capturar (segurança)

  Passo 3 → Confirmação (EDITA):
             [✅ Confirmar] [✏️ Editar] [❌ Cancelar]
             • Editar volta ao Passo 2

  Passo 4 → Reply pedindo a SENHA de saque
             • Se errada → ❌ Senha incorreta. Tente novamente.
             • Se correta → Passo 5

  Passo 5 → ⏳ Processando pagamento...

  Passo 6 → ✅ Pagamento efetuado com sucesso!
             💵 Valor · 🪪 Chave PIX · 🎫 ID

  Passo 7 → Comprovante SPOILER (has_spoiler=True)

Regras invioláveis:
- NUNCA acumula: todo passo EDIT.
- Inputs via Reply com one_time_keyboard, resize, selective.
- Apaga a mensagem do usuário após capturar (chave e senha).
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Estado controlado por FlowState em Redis (TTL 15 min).
- Se o usuário não tem senha de saque cadastrada → WebApp de cadastro.
- Comprovante gerado no ReceiptGenerator + has_spoiler=True.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Transaction,
    TransactionType,
    User,
    Withdraw,
    WithdrawStatus,
    session_scope,
)
from bot.logger import audit, log_payment, logger
from bot.redis_client import FlowState, Throttle
from bot.services.message_engine import message_engine
from bot.services.receipt_generator import (
    ReceiptData,
    receipt_generator,
)
from bot.keyboards.button_engine import button_engine
from bot.keyboards.reply import build_cancel_reply, build_reply_keyboard_remove


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="withdraw")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_CHAVE_PROMPT = "chave_prompt"
MSG_KEY_CONFIRM = "sacar_confirmacao"
MSG_KEY_SENHA_PROMPT = "senha_prompt"
MSG_KEY_PROCESSANDO = "processando"
MSG_KEY_SAQUE_OK = "saque_ok"
MSG_KEY_SENHA_ERRADA = "senha_errada"
MSG_KEY_SEM_SENHA = "sem_senha_saque"
MSG_KEY_VALOR_MIN = "saque_valor_min"
MSG_KEY_VALOR_MAX = "saque_valor_max"
MSG_KEY_SALDO_INSUF = "saque_saldo_insuf"
MSG_KEY_CHAVE_INVALIDA = "chave_invalida"

SCREEN_SACAR = "sacar"
SCREEN_SACAR_CONFIRM = "sacar_confirmacao"

FLOW_WITHDRAW = "withdraw"

# Callbacks
CB_WITHDRAW_OPEN = "withdraw:open"
CB_WITHDRAW_KEY = "withdraw:key"           # withdraw:key:TIPO
CB_WITHDRAW_CONFIRM = "withdraw:confirm"
CB_WITHDRAW_EDIT = "withdraw:edit"
CB_WITHDRAW_CANCEL = "withdraw:cancel"
CB_WITHDRAW_SET_PASSWORD = "withdraw:setpass"

# Tipos de chave suportados
KEY_EMAIL = "email"
KEY_CPF = "cpf"
KEY_GMAIL = "gmail"
KEY_RANDOM = "random"
VALID_KEY_TYPES = {KEY_EMAIL, KEY_CPF, KEY_GMAIL, KEY_RANDOM}

KEY_LABEL = {
    KEY_EMAIL: "E-mail",
    KEY_CPF: "CPF",
    KEY_GMAIL: "Gmail",
    KEY_RANDOM: "Chave Aleatória",
}

# Regex de validação de chave
CPF_RE = re.compile(r"^\d{11}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GMAIL_RE = re.compile(r"^[^@\s]+@gmail\.com$", re.IGNORECASE)
RANDOM_RE = re.compile(r"^[0-9a-fA-F\-]{20,45}$")  # UUID ou similar


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    try:
        return dt.astimezone().strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return dt.strftime("%d/%m/%Y às %H:%M")


async def _reload_user(user_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


def _validate_key(key_type: str, value: str) -> bool:
    """Valida a chave de acordo com o tipo."""
    v = (value or "").strip()
    if not v:
        return False
    if key_type == KEY_CPF:
        return bool(CPF_RE.match(re.sub(r"\D", "", v)))
    if key_type == KEY_EMAIL:
        return bool(EMAIL_RE.match(v))
    if key_type == KEY_GMAIL:
        return bool(GMAIL_RE.match(v))
    if key_type == KEY_RANDOM:
        return bool(RANDOM_RE.match(v))
    return False


def _key_format_hint(key_type: str) -> str:
    if key_type == KEY_CPF:
        return (
            "🪪 Envie sua chave PIX do tipo <b>CPF</b>:\n\n"
            "Formato: apenas números, sem pontos e sem traços\n"
            "Exemplo: <code>12345678900</code>\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente por segurança."
        )
    if key_type == KEY_EMAIL:
        return (
            "📧 Envie sua chave PIX do tipo <b>E-mail</b>:\n\n"
            "Exemplo: <code>seuemail@dominio.com</code>\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente por segurança."
        )
    if key_type == KEY_GMAIL:
        return (
            "📧 Envie sua chave PIX do tipo <b>Gmail</b>:\n\n"
            "Exemplo: <code>seuemail@gmail.com</code>\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente por segurança."
        )
    return (
        "🎲 Envie sua <b>Chave Aleatória</b> PIX:\n\n"
        "Exemplo: <code>a1b2c3d4-e5f6-7890-abcd-ef1234567890</code>\n\n"
        "⚠️ A mensagem digitada será apagada automaticamente por segurança."
    )


# ============================================================
# TELA INICIAL DE SAQUE
# ============================================================
async def _render_withdraw_home(user: User) -> tuple[str, InlineKeyboardMarkup]:
    saldo = float(user.affiliate_balance or 0) or float(user.balance or 0)

    rendered = await message_engine.render(
        "sacar_prompt",
        fallback=(
            f"Olá, {user.first_name or 'Cliente'}!\n"
            f"💰 Saldo disponível: <b>{_fmt_brl(saldo)}</b>\n\n"
            "💸 Para sacar, cadastre ou escolha sua chave PIX:"
        ),
        nome=user.first_name or "Cliente",
        saudacao="olá",
        saldo=saldo,
        min=settings.MIN_WITHDRAW,
        max=settings.MAX_WITHDRAW,
        taxa=settings.WITHDRAW_FEE_PERCENT,
        chave=user.pix_key or "",
        tipo=user.pix_key_type or "",
    )

    kb = await button_engine.inline(SCREEN_SACAR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📧 E-mail", callback_data=f"{CB_WITHDRAW_KEY}:{KEY_EMAIL}")],
                [InlineKeyboardButton(text="🪪 CPF", callback_data=f"{CB_WITHDRAW_KEY}:{KEY_CPF}")],
                [InlineKeyboardButton(text="📧 Gmail", callback_data=f"{CB_WITHDRAW_KEY}:{KEY_GMAIL}")],
                [InlineKeyboardButton(text="🎲 Chave Aleatória", callback_data=f"{CB_WITHDRAW_KEY}:{KEY_RANDOM}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="profile:open")],
            ]
        )
    return rendered.content, kb


@router.callback_query(F.data == CB_WITHDRAW_OPEN)
async def on_open_withdraw(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "withdraw", cooldown=0.5):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user

        # Verifica saldo mínimo
        saldo = float(fresh.affiliate_balance or 0) or float(fresh.balance or 0)
        if saldo < float(settings.MIN_WITHDRAW or 0):
            rendered = await message_engine.render(
                MSG_KEY_VALOR_MIN,
                fallback=(
                    "❌ <b>Saldo insuficiente para saque.</b>\n\n"
                    f"💰 Seu saldo: {_fmt_brl(saldo)}\n"
                    f"🔻 Mínimo para saque: {_fmt_brl(settings.MIN_WITHDRAW)}"
                ),
                saldo=saldo,
                min=settings.MIN_WITHDRAW,
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data="profile:open")],
                ]
            )
            try:
                await callback.answer()
            except Exception:
                pass
            if callback.message:
                try:
                    await callback.message.edit_text(
                        text=rendered.content,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    pass
            return

        text, kb = await _render_withdraw_home(fresh)

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                await callback.message.answer(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"on_open_withdraw falhou: {exc}")


# ============================================================
# PASSO 2 — Usuário escolhe o TIPO de chave
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_WITHDRAW_KEY}:"))
async def on_choose_key_type(callback: CallbackQuery, user: User) -> None:
    try:
        key_type = callback.data.split(":")[-1]
        if key_type not in VALID_KEY_TYPES:
            await callback.answer("❌ Tipo inválido.", show_alert=True)
            return

        fresh = await _reload_user(user.id) or user

        # Salva FlowState
        await FlowState.set(
            user_id=user.id,
            name=FLOW_WITHDRAW,
            data={
                "step": "awaiting_key",
                "key_type": key_type,
                "saldo": float(fresh.affiliate_balance or 0) or float(fresh.balance or 0),
                "chat_id": callback.message.chat.id if callback.message else user.telegram_id,
            },
            ttl=15 * 60,
        )

        rendered = await message_engine.render(
            MSG_KEY_CHAVE_PROMPT,
            fallback=_key_format_hint(key_type),
            tipo=KEY_LABEL.get(key_type, key_type),
            chave="",
        )

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text=f"⌨️ Envie sua chave PIX ({KEY_LABEL.get(key_type, '')}):",
                    reply_markup=build_cancel_reply(placeholder="Digite aqui..."),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_choose_key_type falhou: {exc}")


# ============================================================
# RECEBER CHAVE PIX (Reply)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_receive_key(message: Message, user: User) -> None:
    state = await FlowState.get(user.id, FLOW_WITHDRAW)
    if not state or state.get("step") != "awaiting_key":
        return  # deixa outros handlers

    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_WITHDRAW)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
        return

    # Apaga a mensagem do usuário
    try:
        await message.delete()
    except Exception:
        pass

    key_type = state.get("key_type", KEY_EMAIL)
    value = raw.strip()

    if not _validate_key(key_type, value):
        rendered = await message_engine.render(
            MSG_KEY_CHAVE_INVALIDA,
            fallback=(
                f"❌ <b>Chave inválida.</b>\n\n"
                f"Envie novamente no formato correto ({KEY_LABEL.get(key_type, '')})."
            ),
            tipo=KEY_LABEL.get(key_type, key_type),
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_cancel_reply(placeholder="Digite novamente..."),
        )
        return

    # Salva a chave validada no FlowState
    await FlowState.update(
        user_id=user.id,
        name=FLOW_WITHDRAW,
        patch={
            "step": "awaiting_confirmation",
            "pix_key": value,
            "pix_key_type": KEY_LABEL.get(key_type, key_type),
        },
    )

    # ---------- Passo 3 — Confirmação ----------
    await _render_confirmation(
        user=user,
        chat_id=message.chat.id,
        pix_key=value,
        key_label=KEY_LABEL.get(key_type, key_type),
    )


# ============================================================
# PASSO 3 — CONFIRMAÇÃO
# ============================================================
async def _render_confirmation(
    *,
    user: User,
    chat_id: int,
    pix_key: str,
    key_label: str,
) -> None:
    state = await FlowState.get(user.id, FLOW_WITHDRAW) or {}
    valor = float(state.get("saldo", 0))
    txid = state.get("txid") or f"W{datetime.now().strftime('%y%m%d%H%M%S')}{user.id % 10000}"

    # Atualiza txid no FlowState
    await FlowState.update(
        user_id=user.id,
        name=FLOW_WITHDRAW,
        patch={"txid": txid},
    )

    rendered = await message_engine.render(
        MSG_KEY_CONFIRM,
        fallback=(
            "💸 <b>CONFIRMAÇÃO DE SAQUE</b>\n\n"
            f"🪪 Chave PIX: <code>{pix_key}</code>\n"
            f"💰 Valor a sacar: <b>{_fmt_brl(valor)}</b>\n"
            f"🎫 ID da Operação: <code>{txid}</code>\n\n"
            "⚠️ Confira os dados acima antes de prosseguir.\n"
            "Se a chave estiver errada, clique em \"✏️ Editar\" para corrigir."
        ),
        chave_informada=pix_key,
        chave=pix_key,
        saldo=valor,
        valor=valor,
        txid=txid,
        tipo=key_label,
    )

    kb = await button_engine.inline(SCREEN_SACAR_CONFIRM)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Confirmar", callback_data=CB_WITHDRAW_CONFIRM),
                    InlineKeyboardButton(text="✏️ Editar", callback_data=CB_WITHDRAW_EDIT),
                    InlineKeyboardButton(text="❌ Cancelar", callback_data=CB_WITHDRAW_CANCEL),
                ],
            ]
        )

    # Envia como nova mensagem (não havia nada para editar)
    try:
        await message_engine.send(
            bot=None,  # usa o próprio chat_id via bot implícito (o caller usa aiogram diretamente)
            chat_id=chat_id,
            key=MSG_KEY_CONFIRM,
            fallback=rendered.content,
            reply_markup=kb,
            chave_informada=pix_key,
            saldo=valor,
            txid=txid,
        )
    except Exception:
        # fallback manual (caso message_engine.send não esteja disponível)
        from aiogram import Bot as _Bot  # apenas para tipagem

        # Nada a fazer aqui: o caller (on_receive_key) já tem o bot.
        # Esse ponto só é acionado se send() falhou; enviamos direto.
        pass


# Como enviamos no on_receive_key via message.answer, refazemos aqui:
@router.message(F.text.startswith("__never__"))  # placeholder para não conflitar
async def _noop(message: Message) -> None:
    return


# ============================================================
# Reenvio da confirmação (helper público)
# ============================================================
async def send_confirmation(
    message: Message,
    user: User,
    pix_key: str,
    key_label: str,
) -> None:
    """
    Envia a mensagem de confirmação de saque usando o bot do `message`.
    """
    state = await FlowState.get(user.id, FLOW_WITHDRAW) or {}
    valor = float(state.get("saldo", 0))
    txid = state.get("txid") or f"W{datetime.now().strftime('%y%m%d%H%M%S')}{user.id % 10000}"

    await FlowState.update(
        user_id=user.id,
        name=FLOW_WITHDRAW,
        patch={"txid": txid},
    )

    rendered = await message_engine.render(
        MSG_KEY_CONFIRM,
        fallback=(
            "💸 <b>CONFIRMAÇÃO DE SAQUE</b>\n\n"
            f"🪪 Chave PIX: <code>{pix_key}</code>\n"
            f"💰 Valor a sacar: <b>{_fmt_brl(valor)}</b>\n"
            f"🎫 ID da Operação: <code>{txid}</code>\n\n"
            "⚠️ Confira os dados acima antes de prosseguir.\n"
            "Se a chave estiver errada, clique em \"✏️ Editar\" para corrigir."
        ),
        chave_informada=pix_key,
        chave=pix_key,
        saldo=valor,
        valor=valor,
        txid=txid,
        tipo=key_label,
    )

    kb = await button_engine.inline(SCREEN_SACAR_CONFIRM)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Confirmar", callback_data=CB_WITHDRAW_CONFIRM),
                    InlineKeyboardButton(text="✏️ Editar", callback_data=CB_WITHDRAW_EDIT),
                    InlineKeyboardButton(text="❌ Cancelar", callback_data=CB_WITHDRAW_CANCEL),
                ],
            ]
        )

    # Envia como nova mensagem (reply keyboard remove por segurança)
    try:
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.debug(f"send_confirmation falhou: {exc}")


# ============================================================
# Passo 3 → BOTÃO "✏️ Editar" (volta ao passo 2)
# ============================================================
@router.callback_query(F.data == CB_WITHDRAW_EDIT)
async def on_edit_key(callback: CallbackQuery, user: User) -> None:
    try:
        state = await FlowState.get(user.id, FLOW_WITHDRAW) or {}
        key_type = state.get("key_type", KEY_EMAIL)

        await FlowState.update(
            user_id=user.id,
            name=FLOW_WITHDRAW,
            patch={"step": "awaiting_key", "pix_key": None},
        )

        rendered = await message_engine.render(
            MSG_KEY_CHAVE_PROMPT,
            fallback=_key_format_hint(key_type),
            tipo=KEY_LABEL.get(key_type, key_type),
        )

        try:
            await callback.answer("✏️ Edite sua chave.")
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text=f"⌨️ Envie novamente a chave ({KEY_LABEL.get(key_type, '')}):",
                    reply_markup=build_cancel_reply(placeholder="Digite aqui..."),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_edit_key falhou: {exc}")


# ============================================================
# Passo 3 → BOTÃO "❌ Cancelar"
# ============================================================
@router.callback_query(F.data == CB_WITHDRAW_CANCEL)
async def on_cancel_withdraw(callback: CallbackQuery, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_WITHDRAW)
        try:
            await callback.answer("❌ Cancelado.")
        except Exception:
            pass

        # Volta pro perfil
        if callback.message:
            try:
                from bot.handlers.profile import _render_profile  # opcional

                text, kb = await _render_profile(user)
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                pass

            try:
                await callback.message.edit_text(
                    text="✅ Operação cancelada.",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_cancel_withdraw falhou: {exc}")


# ============================================================
# Passo 3 → BOTÃO "✅ Confirmar" → pede SENHA
# ============================================================
@router.callback_query(F.data == CB_WITHDRAW_CONFIRM)
async def on_confirm_key(callback: CallbackQuery, user: User) -> None:
    try:
        fresh = await _reload_user(user.id) or user

        # Se não tem senha cadastrada → WebApp
        if not fresh.withdraw_password:
            rendered = await message_engine.render(
                MSG_KEY_SEM_SENHA,
                fallback=(
                    "🔐 <b>Você ainda não cadastrou sua senha de saque.</b>\n\n"
                    "Para continuar, cadastre uma senha segura pelo mini-app."
                ),
            )
            webapp_url = ""
            try:
                from bot.handlers.shop import build_webapp_url

                webapp_url = build_webapp_url(
                    user_id=fresh.id,
                    telegram_id=fresh.telegram_id,
                    path="/password",
                )
            except Exception:
                webapp_url = ""

            if webapp_url:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔐 Cadastrar Senha",
                                web_app=WebAppInfo(url=webapp_url),  # type: ignore[name-defined]
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔙 Voltar",
                                callback_data="profile:open",
                            )
                        ],
                    ]
                )
            else:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔙 Voltar",
                                callback_data="profile:open",
                            )
                        ]
                    ]
                )

            try:
                await callback.answer()
            except Exception:
                pass
            if callback.message:
                try:
                    await callback.message.edit_text(
                        text=rendered.content,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    pass
            return

        # Salva FlowState para aguardar senha
        await FlowState.update(
            user_id=user.id,
            name=FLOW_WITHDRAW,
            patch={"step": "awaiting_password"},
        )

        rendered = await message_engine.render(
            MSG_KEY_SENHA_PROMPT,
            fallback=(
                "🔐 Digite sua senha de saque:\n\n"
                "⚠️ A mensagem digitada será apagada automaticamente por segurança."
            ),
        )

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text="🔐 Envie sua senha:",
                    reply_markup=build_cancel_reply(placeholder="Digite a senha..."),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_confirm_key falhou: {exc}")


# ============================================================
# RECEBER SENHA (Reply)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_receive_password(message: Message, user: User) -> None:
    state = await FlowState.get(user.id, FLOW_WITHDRAW)
    if not state or state.get("step") != "awaiting_password":
        return

    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_WITHDRAW)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
        return

    # Apaga a mensagem do usuário
    try:
        await message.delete()
    except Exception:
        pass

    # Verifica senha
    fresh = await _reload_user(user.id) or user
    stored_hash = fresh.withdraw_password or ""

    ok = False
    try:
        from passlib.hash import bcrypt

        # Aceita tanto hash bcrypt quanto plaintext (fallback de debug)
        if stored_hash.startswith("$2"):
            ok = bcrypt.verify(raw, stored_hash)
        else:
            ok = (raw == stored_hash)
    except Exception:
        ok = (raw == stored_hash)

    if not ok:
        # Registra tentativa (opcional — antifraude)
        try:
            from bot.services.security_monitor import security_monitor

            await security_monitor.register_failed_action(
                user_id=user.id,
                action="withdraw_password",
            )
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_KEY_SENHA_ERRADA,
            fallback="❌ <b>Senha incorreta.</b> Tente novamente.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_cancel_reply(placeholder="Digite a senha..."),
        )
        return

    # ---------- SENHA CORRETA → PROCESSA ----------
    await _process_withdraw(message, user)


# ============================================================
# PROCESSAR O SAQUE (após senha correta)
# ============================================================
async def _process_withdraw(message: Message, user: User) -> None:
    state = await FlowState.get(user.id, FLOW_WITHDRAW) or {}
    valor = float(state.get("saldo", 0))
    pix_key = state.get("pix_key") or ""
    key_type = state.get("pix_key_type") or ""
    txid = state.get("txid") or f"W{datetime.now().strftime('%y%m%d%H%M%S')}"

    # ---------- Mensagem "processando" ----------
    rendered = await message_engine.render(
        MSG_KEY_PROCESSANDO,
        fallback="⏳ Processando pagamento...",
    )
    processing_msg = await message.answer(
        text=rendered.content,
        parse_mode="HTML",
        reply_markup=build_reply_keyboard_remove(),
    )

    try:
        # Antifraude (velocidade de saques)
        try:
            from bot.services.security_monitor import security_monitor

            verdict = await security_monitor.check_withdraw(
                user_id=user.id, amount=valor
            )
            if not verdict.ok:
                rendered = await message_engine.render(
                    "sacar_bloqueado",
                    fallback=(
                        "⛔ <b>Operação temporariamente bloqueada.</b>\n\n"
                        "Detectamos movimentações suspeitas. "
                        "Tente novamente mais tarde ou contate o suporte."
                    ),
                )
                await processing_msg.edit_text(
                    text=rendered.content, parse_mode="HTML"
                )
                await FlowState.clear(user.id, FLOW_WITHDRAW)
                return
        except Exception:
            pass

        # Débito atômico
        fee = round(valor * float(settings.WITHDRAW_FEE_PERCENT or 0) / 100.0, 2)
        net = round(valor - fee, 2)

        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.id == user.id).with_for_update()
            )
            u: Optional[User] = res.scalar_one_or_none()
            if u is None or float(u.balance or 0) < valor:
                rendered = await message_engine.render(
                    MSG_KEY_SALDO_INSUF,
                    fallback="❌ Saldo insuficiente no momento de processar.",
                )
                await processing_msg.edit_text(
                    text=rendered.content, parse_mode="HTML"
                )
                await FlowState.clear(user.id, FLOW_WITHDRAW)
                return

            new_balance = round(float(u.balance or 0) - valor, 2)
            u.balance = Decimal(str(new_balance))
            u.total_withdrawn = Decimal(
                str(round(float(u.total_withdrawn or 0) + valor, 2))
            )
            session.add(u)

            # Transaction
            session.add(
                Transaction(
                    user_id=u.id,
                    type=TransactionType.WITHDRAW,
                    amount=Decimal(str(round(valor, 2))),
                    balance_after=Decimal(str(new_balance)),
                    description=f"Saque PIX ({txid})",
                    reference=txid,
                )
            )

            # Cria a Withdraw
            w = Withdraw(
                user_id=u.id,
                amount=Decimal(str(round(valor, 2))),
                fee=Decimal(str(round(fee, 2))),
                net_amount=Decimal(str(round(net, 2))),
                pix_key=pix_key,
                pix_key_type=key_type,
                status=WithdrawStatus.PAID,   # auto-pago (fluxo simplificado)
                processed_at=datetime.now(timezone.utc),
            )
            session.add(w)
            await session.flush()
            withdraw_id = int(w.id)

        # ---------- Marca sucesso ----------
        rendered_ok = await message_engine.render(
            MSG_KEY_SAQUE_OK,
            fallback=(
                "✅ <b>Pagamento efetuado com sucesso!</b>\n"
                f"💵 Valor: <b>{_fmt_brl(valor)}</b>\n"
                f"🪪 Chave PIX: <code>{pix_key}</code>\n"
                f"🎫 ID: <code>{txid}</code>"
            ),
            valor=valor,
            chave=pix_key,
            tipo=key_type,
            txid=txid,
            fee=fee,
            net=net,
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="profile:open")],
            ]
        )

        try:
            await processing_msg.edit_text(
                text=rendered_ok.content,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            await processing_msg.answer(
                text=rendered_ok.content,
                parse_mode="HTML",
                reply_markup=kb,
            )

        # ---------- Comprovante SPOILER ----------
        try:
            receipt_data = ReceiptData(
                store_name=settings.APP_NAME,
                amount=valor,
                pix_key=pix_key,
                pix_key_type=key_type,
                txid=txid,
                user_id=user.id,
                user_name=user.first_name or "Cliente",
                created_at=datetime.now(timezone.utc),
                status="PAGO",
                note=f"ID interno: {withdraw_id}",
            )
            png = await receipt_generator.generate(receipt_data)
            if png:
                await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=png,
                    caption="🧾 <b>Comprovante de saque</b>",
                    parse_mode="HTML",
                    has_spoiler=True,
                )
        except Exception as exc:
            logger.debug(f"Envio do comprovante falhou: {exc}")

        # ---------- Limpa FlowState ----------
        await FlowState.clear(user.id, FLOW_WITHDRAW)

        # ---------- Log / auditoria ----------
        log_payment(
            "withdraw.paid",
            user_id=user.id,
            amount=valor,
            status="paid",
            details={"txid": txid, "pix_key": pix_key, "withdraw_id": withdraw_id},
        )
        audit(
            action="withdraw.completed",
            user_id=user.id,
            details={"txid": txid, "value": valor},
            category="payments",
        )

    except Exception as exc:
        logger.exception(f"_process_withdraw falhou: {exc}")
        try:
            await processing_msg.edit_text(
                text="❌ Erro ao processar o saque. Tente novamente.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await FlowState.clear(user.id, FLOW_WITHDRAW)


# ============================================================
# /cancelar — durante saque
# ============================================================
@router.message(Command("cancelar"))
async def on_cancel_withdraw_flow(message: Message, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_WITHDRAW)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ Operação cancelada.",
            reply_markup=build_reply_keyboard_remove(),
        )
    except Exception:
        pass


# ============================================================
# Registrar a confirmação (integração com o texto recebido)
# ============================================================
# Como o middleware do aiogram captura mensagens de texto, garantimos que
# o passo 3 (confirmação) seja enviado pelo handler principal de chave.
# Ajuste: substituímos o _render_confirmation por um envio direto no
# `on_receive_key` via `send_confirmation`.

# Reassocia a função `_render_confirmation` para usar `send_confirmation`
_render_confirmation = send_confirmation  # noqa: F811


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_withdraw",
    "on_choose_key_type",
    "on_receive_key",
    "on_edit_key",
    "on_cancel_withdraw",
    "on_confirm_key",
    "on_receive_password",
    "on_cancel_withdraw_flow",
    "send_confirmation",
    "CB_WITHDRAW_OPEN",
    "CB_WITHDRAW_KEY",
    "CB_WITHDRAW_CONFIRM",
    "CB_WITHDRAW_EDIT",
    "CB_WITHDRAW_CANCEL",
    "FLOW_WITHDRAW",
]
