"""
Larizinha Store — Handler de Recarga (PIX Rápido)
Implementa TODO o fluxo PIX do DIM:

    1) Tela inicial de recarga (key="recarregar")
       • ID da carteira + saldo
       • Botão [💠 PIX Rápido] + [🔙 Voltar]

    2) PIX Rápido (Reply): pede valor
       • Validações reais: inválido / mín / máx / bônus
       • Apaga a mensagem do usuário (segurança)

    3) Geração:
       • key="pix_gerando"    → ⏳ Gerando pagamento...
       • Chama pix_service.create_charge (trava real de estoque se for produto)
       • key="pix_qr"         → QR + Copia e Cola + botões:
           [🔄 Aguardando Pagamento] [📋 Copiar PIX]

    4) Aguardando pagamento:
       • Consulta real na API do MP (fetch_status)
       • Se pago → credita saldo + edita para key="pix_ok"
       • Se expirado → key="pix_expirado" com [🔄 Gerar Novo PIX] [🔙 Voltar]

    5) Automação PIX Não Pago:
       • key="pix_naopago" — disparada pelo worker

Regras invioláveis:
- NUNCA acumula: sempre edit_message_text.
- Reply keyboard: one_time, resize, selective.
- Apaga a mensagem do usuário após capturar o valor.
- Toda mensagem/botão 100% editável pelo painel admin.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from bot.config import settings
from bot.database import (
    PixCharge,
    PixStatus,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import log_payment, logger
from bot.redis_client import FlowState, Throttle
from bot.services.message_engine import message_engine
from bot.services.pix_service import (
    FLOW_NAME_PIX,
    PixResult,
    decode_qr_base64,
    pix_service,
)
from bot.services.receipt_generator import (
    ReceiptData,
    receipt_generator,
)
from bot.keyboards.button_engine import button_engine
from bot.keyboards.reply import build_cancel_reply, build_reply_keyboard_remove


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="recharge")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_RECARREGAR = "recarregar"
MSG_KEY_VALOR_PROMPT = "valor_prompt"
MSG_KEY_VALOR_INVALIDO = "valor_invalido"
MSG_KEY_VALOR_MIN = "valor_min"
MSG_KEY_VALOR_MAX = "valor_max"
MSG_KEY_PIX_GERANDO = "pix_gerando"
MSG_KEY_PIX_QR = "pix_qr"
MSG_KEY_PIX_OK = "pix_ok"
MSG_KEY_PIX_EXPIRADO = "pix_expirado"
MSG_KEY_PIX_NAOPAGO = "pix_naopago"
MSG_KEY_CANCELAR = "cmd_cancelar"

SCREEN_RECARREGAR = "recarregar"
SCREEN_PIX_QR = "pix_qr"
SCREEN_PIX_EXPIRADO = "pix_expirado"

FLOW_NAME_VALUE = "recharge_value"

# Callbacks
CB_RECHARGE_OPEN = "recharge:open"
CB_RECHARGE_PIX_FAST = "recharge:fast"
CB_RECHARGE_PIX_AMOUNT = "recharge:pix"        # recharge:pix:VALOR
CB_RECHARGE_CHECK = "recharge:check"           # recharge:check:TXID
CB_RECHARGE_COPY = "recharge:copy"             # recharge:copy:TXID
CB_RECHARGE_NEW = "recharge:new"
CB_RECHARGE_CANCEL = "recharge:cancel"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


async def _reload_user(user_id: int) -> Optional[User]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_charge(txid: str) -> Optional[PixCharge]:
    return await pix_service.get_by_txid(txid)


# ============================================================
# 🖼️ TELA INICIAL DE RECARGA
# ============================================================
async def _render_recharge_home(user: User) -> tuple[str, InlineKeyboardMarkup]:
    rendered = await message_engine.render(
        MSG_KEY_RECARREGAR,
        fallback=(
            f"🆔| ID da Carteira: <code>{user.id}</code>\n"
            f"💰| Saldo Disponível: <b>{_fmt_brl(float(user.balance or 0))}</b>\n\n"
            "📍 Opte por 💠 PIX Rápido para que seu saldo seja creditado imediatamente.\n\n"
            "💡 Selecione uma opção para recarregar:"
        ),
        id=user.id,
        saldo=float(user.balance or 0),
        min=settings.MIN_DEPOSIT,
        max=settings.MAX_DEPOSIT,
        bonus=settings.DEPOSIT_BONUS_PERCENT,
        min_bonus=settings.DEPOSIT_BONUS_MIN,
    )

    kb = await button_engine.inline(SCREEN_RECARREGAR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💠 PIX Rápido", callback_data=CB_RECHARGE_PIX_FAST)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")],
            ]
        )
    return rendered.content, kb


@router.callback_query(F.data == CB_RECHARGE_OPEN)
async def on_recharge_open(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "recharge", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_recharge_home(fresh)

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
        logger.exception(f"on_recharge_open falhou: {exc}")


# ============================================================
# 💠 PIX RÁPIDO — pedir valor (Reply)
# ============================================================
async def _render_value_prompt(user: User) -> str:
    rendered = await message_engine.render(
        MSG_KEY_VALOR_PROMPT,
        fallback=(
            "ℹ️ <b>Informe o valor que deseja recarregar:</b>\n\n"
            f"🔻 Recarga mínima: <b>{_fmt_brl(settings.MIN_DEPOSIT)}</b>\n\n"
            "⚠️ Por favor, envie o valor que deseja recarregar agora.\n"
            "Ao realizar um depósito você declara ter lido e estar de acordo com nossos /termos\n\n"
            f"🎁 Bônus de recarga: <b>{settings.DEPOSIT_BONUS_PERCENT}%</b>\n"
            f"❗️ Recarga mínima para ganhar o bônus: <b>{_fmt_brl(settings.DEPOSIT_BONUS_MIN)}</b>"
        ),
        min=settings.MIN_DEPOSIT,
        max=settings.MAX_DEPOSIT,
        bonus=settings.DEPOSIT_BONUS_PERCENT,
        min_bonus=settings.DEPOSIT_BONUS_MIN,
        nome=user.first_name or "Cliente",
        saldo=float(user.balance or 0),
    )
    return rendered.content


@router.callback_query(F.data == CB_RECHARGE_PIX_FAST)
async def on_recharge_pix_fast(callback: CallbackQuery, user: User) -> None:
    try:
        text = await _render_value_prompt(user)

        # Guarda estado do fluxo
        await FlowState.set(
            user_id=user.id,
            name=FLOW_NAME_VALUE,
            data={
                "chat_id": callback.message.chat.id if callback.message else user.telegram_id,
            },
            ttl=15 * 60,
        )

        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text="⌨️ Envie o valor desejado (ex: 20 ou 25.50):",
                    reply_markup=build_cancel_reply(placeholder="Ex: 20"),
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_recharge_pix_fast falhou: {exc}")


# ============================================================
# RECEBER VALOR
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_receive_recharge_value(message: Message, user: User) -> None:
    """
    Trata a mensagem apenas se o usuário estiver no fluxo de recarga.
    Caso contrário, ignora (deixa outros handlers).
    """
    state = await FlowState.get(user.id, FLOW_NAME_VALUE)
    if not state:
        return

    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_NAME_VALUE)
        try:
            await message.delete()
        except Exception:
            pass
        try:
            fresh = await _reload_user(user.id) or user
            text, kb = await _render_recharge_home(fresh)
            await message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
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

    # ---------- Validação de valor ----------
    raw_num = raw.replace(",", ".")
    try:
        valor = float(raw_num)
        if valor <= 0:
            raise ValueError
    except Exception:
        rendered = await message_engine.render(
            MSG_KEY_VALOR_INVALIDO,
            fallback="❌ <b>Valor inválido.</b>\n\nEnvie um número maior que zero.",
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_cancel_reply(placeholder="Ex: 20"),
        )
        return

    # Mínimo
    if valor < settings.MIN_DEPOSIT:
        rendered = await message_engine.render(
            MSG_KEY_VALOR_MIN,
            fallback=(
                f"❌ <b>Valor abaixo do mínimo.</b>\n\n"
                f"🔻 Recarga mínima: <b>{_fmt_brl(settings.MIN_DEPOSIT)}</b>"
            ),
            min=settings.MIN_DEPOSIT,
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_cancel_reply(placeholder="Ex: 20"),
        )
        return

    # Máximo
    if valor > settings.MAX_DEPOSIT:
        rendered = await message_engine.render(
            MSG_KEY_VALOR_MAX,
            fallback=(
                f"❌ <b>Valor acima do máximo.</b>\n\n"
                f"🔺 Recarga máxima: <b>{_fmt_brl(settings.MAX_DEPOSIT)}</b>"
            ),
            max=settings.MAX_DEPOSIT,
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=build_cancel_reply(placeholder="Ex: 20"),
        )
        return

    # ---------- Bônus ----------
    bonus = 0.0
    if (
        float(settings.DEPOSIT_BONUS_PERCENT or 0) > 0
        and valor >= float(settings.DEPOSIT_BONUS_MIN or 0)
    ):
        bonus = round(valor * float(settings.DEPOSIT_BONUS_PERCENT) / 100.0, 2)

    # ---------- Gera PIX ----------
    await _start_pix_flow(
        message=message,
        user=user,
        valor=valor,
        bonus=bonus,
    )


# ============================================================
# 🚀 INICIAR FLUXO PIX (comum a /pix, recharge, etc.)
# ============================================================
async def _start_pix_flow(
    *,
    message: Message,
    user: User,
    valor: float,
    bonus: float = 0.0,
    product_id: Optional[int] = None,
    quantity: int = 1,
) -> None:
    """
    Executa o fluxo PIX completo:
      1) Mensagem "gerando" (editável).
      2) Chamada ao pix_service.create_charge.
      3) Envia QR como foto com legenda editável.
      4) Salva estado em FlowState com txid.
    """
    await FlowState.clear(user.id, FLOW_NAME_VALUE)

    # ---------- Mensagem "gerando" ----------
    rendered_gerando = await message_engine.render(
        MSG_KEY_PIX_GERANDO,
        fallback="⏳ Gerando pagamento...",
    )

    generating_msg = await message.answer(
        text=rendered_gerando.content,
        parse_mode="HTML",
        reply_markup=None,
    )

    # ---------- Chama o serviço ----------
    result: PixResult = await pix_service.create_charge(
        user_id=user.id,
        amount=valor,
        bonus=bonus,
        reserve_product_id=product_id,
        reserve_quantity=quantity,
        metadata={
            "chat_id": generating_msg.chat.id,
            "generating_message_id": generating_msg.message_id,
        },
    )

    if not result.ok or not result.txid:
        try:
            await generating_msg.edit_text(
                text=(
                    f"❌ <b>Não foi possível gerar o PIX.</b>\n\n"
                    f"{result.error or 'Tente novamente em instantes.'}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ---------- Salva FlowState ----------
    await FlowState.set(
        user_id=user.id,
        name=FLOW_NAME_PIX,
        data={
            "txid": result.txid,
            "charge_id": result.charge_id,
            "amount": result.amount,
            "bonus": result.bonus,
            "chat_id": generating_msg.chat.id,
            "message_id": generating_msg.message_id,
        },
        ttl=settings.MP_PIX_EXPIRATION_MINUTES * 60 + 300,
    )

    # ---------- Tela do QR ----------
    await _render_and_send_qr(
        bot=message.bot,
        user=user,
        result=result,
        edit_message_id=generating_msg.message_id,
        chat_id=generating_msg.chat.id,
    )


# ============================================================
# 🖼️ RENDERIZAR QR
# ============================================================
async def _render_and_send_qr(
    *,
    bot,
    user: User,
    result: PixResult,
    chat_id: int,
    edit_message_id: Optional[int] = None,
) -> None:
    """
    Renderiza a tela de QR:
    - Se tiver imagem base64 → envia como FOTO com legenda.
    - Se não → envia como TEXTO.
    - Botões: [🔄 Aguardando Pagamento] [📋 Copiar PIX]
    """
    # Variáveis do template
    minutes = settings.MP_PIX_EXPIRATION_MINUTES
    saldo_atual = float(user.balance or 0)
    saldo_final = round(saldo_atual + float(result.amount or 0) + float(result.bonus or 0), 2)

    rendered = await message_engine.render(
        MSG_KEY_PIX_QR,
        fallback=(
            "💰 <b>Comprar Saldo com PIX Automático</b>:\n\n"
            f"⏱️ Expira em: <b>{minutes} Minutos</b>\n"
            f"💵 Valor: <b>{_fmt_brl(float(result.amount or 0))}</b>\n"
            f"✨ ID da Recarga: <code>{result.txid}</code>\n\n"
            "📃 Atenção: Este código é válido para apenas um único pagamento.\n\n"
            "💎 <b>PIX Copia e Cola:</b>\n"
            f"<code>{result.copy_paste or ''}</code>\n\n"
            "💡 Dica: Clique no código acima para copiar.\n\n"
            "📊 <b>Dados:</b>\n"
            f"— 💰 Saldo Atual: {_fmt_brl(saldo_atual)}\n"
            f"— 🎁 Bônus a receber: {_fmt_brl(float(result.bonus or 0))}\n"
            f"— 💸 Saldo após o pagamento: {_fmt_brl(saldo_final)}\n\n"
            "🇧🇷 Após o pagamento, seu saldo será liberado instantaneamente."
        ),
        min=minutes,
        valor=float(result.amount or 0),
        txid=result.txid,
        pix_copia_cola=result.copy_paste or "",
        saldo=saldo_atual,
        bonus=float(result.bonus or 0),
        saldo_final=saldo_final,
        nome=user.first_name or "Cliente",
    )

    # ---------- Teclado ----------
    kb = await button_engine.inline(SCREEN_PIX_QR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Aguardando Pagamento",
                        callback_data=f"{CB_RECHARGE_CHECK}:{result.txid}",
                    ),
                    InlineKeyboardButton(
                        text="📋 Copiar PIX",
                        callback_data=f"{CB_RECHARGE_COPY}:{result.txid}",
                    ),
                ],
            ]
        )

    # ---------- Tenta enviar como FOTO (QR base64) ----------
    qr_bytes = decode_qr_base64(result.qr_code_base64)
    if qr_bytes:
        try:
            if edit_message_id:
                # tenta deletar a antiga e enviar nova (não é possível "editar" texto → foto)
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=edit_message_id)
                except Exception:
                    pass
            await bot.send_photo(
                chat_id=chat_id,
                photo=qr_bytes,
                caption=rendered.content,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception as exc:
            logger.debug(f"send_photo QR falhou: {exc}")

    # ---------- Fallback: texto ----------
    if edit_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=edit_message_id,
                text=rendered.content,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    await bot.send_message(
        chat_id=chat_id,
        text=rendered.content,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


# ============================================================
# 📋 COPIAR PIX (silencioso)
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RECHARGE_COPY}:"))
async def on_copy_pix(callback: CallbackQuery, user: User) -> None:
    try:
        txid = callback.data.split(":", 2)[-1]
        charge = await _load_charge(txid)

        try:
            await callback.answer(
                "📋 Código copiado! Cole no seu banco." if charge else "❌ PIX não encontrado.",
                show_alert=False,
            )
        except Exception:
            pass
    except Exception as exc:
        logger.debug(f"on_copy_pix falhou: {exc}")


# ============================================================
# 🔄 AGUARDANDO PAGAMENTO — consulta real ao MP
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RECHARGE_CHECK}:"))
async def on_check_pix(callback: CallbackQuery, user: User) -> None:
    """
    Consulta o status atual do PIX no Mercado Pago.
    - Pago → credita + edita para key="pix_ok"
    - Pendente → toast "aguardando"
    - Expirado → edita para key="pix_expirado"
    """
    try:
        txid = callback.data.split(":", 2)[-1]
        charge = await _load_charge(txid)

        if charge is None:
            await callback.answer("❌ PIX não encontrado.", show_alert=True)
            return

        # Já pago?
        if charge.status == PixStatus.PAID:
            await _show_pix_ok(callback, user, charge)
            return

        # Já expirado?
        if charge.status in (PixStatus.EXPIRED, PixStatus.CANCELED):
            await _show_pix_expired(callback, charge)
            return

        # Consulta o MP
        mp_status: Optional[str] = None
        if charge.mp_payment_id:
            info = await pix_service.fetch_status(charge.mp_payment_id)
            if info:
                mp_status = str(info.get("status", "")).lower()

        if mp_status in {"approved", "authorized"}:
            # ---------- MARCA COMO PAGO ----------
            updated = await pix_service.mark_paid(
                txid,
                mp_payment_id=charge.mp_payment_id,
                paid_amount=float(charge.amount or 0),
            )
            if updated is not None:
                await _show_pix_ok(callback, user, updated)
            else:
                await callback.answer("⏳ Confirmando pagamento...", show_alert=False)
            return

        # Ainda pendente → verifica expiração local
        now = datetime.now(timezone.utc)
        if charge.expires_at and charge.expires_at <= now:
            await pix_service.expire_charge(txid)
            await _show_pix_expired(callback, charge)
            return

        await callback.answer(
            "⏳ Ainda não identificamos o pagamento. Aguarde alguns segundos.",
            show_alert=True,
        )
    except Exception as exc:
        logger.exception(f"on_check_pix falhou: {exc}")
        try:
            await callback.answer("❌ Erro ao verificar.", show_alert=True)
        except Exception:
            pass


async def _show_pix_ok(callback: CallbackQuery, user: User, charge: PixCharge) -> None:
    """Edita a mensagem atual para o estado de sucesso (key="pix_ok")."""
    fresh = await _reload_user(user.id) or user

    rendered = await message_engine.render(
        MSG_KEY_PIX_OK,
        fallback=(
            "✅ <b>Pagamento confirmado!</b>\n"
            f"💵 Valor creditado: {_fmt_brl(float(charge.amount or 0))}\n"
            f"🎁 Bônus: {_fmt_brl(float(charge.bonus or 0))}\n"
            f"💰 Novo saldo: {_fmt_brl(float(fresh.balance or 0))}"
        ),
        valor=float(charge.amount or 0),
        bonus=float(charge.bonus or 0),
        saldo=float(fresh.balance or 0),
        txid=charge.txid,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍️ Ir às compras", callback_data="catalog:open")],
        ]
    )

    try:
        await callback.answer("✅ Pagamento confirmado!", show_alert=False)
    except Exception:
        pass

    if callback.message:
        # se a mensagem é foto, edita a caption
        try:
            if callback.message.photo:
                await callback.message.edit_caption(
                    caption=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
        except Exception:
            try:
                await callback.message.answer(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

    # Limpa FlowState
    try:
        await FlowState.clear(user.id, FLOW_NAME_PIX)
    except Exception:
        pass


async def _show_pix_expired(callback: CallbackQuery, charge: PixCharge) -> None:
    """Edita a mensagem atual para o estado de expirado."""
    rendered = await message_engine.render(
        MSG_KEY_PIX_EXPIRADO,
        fallback=(
            "⌛️ <b>PAGAMENTO PIX EXPIRADO</b>\n\n"
            "⚠️ O tempo limite para realizar este pagamento foi excedido.\n\n"
            f"🆔 Referência: <code>{charge.txid}</code>\n"
            f"💸 Valor Solicitado: {_fmt_brl(float(charge.amount or 0))}"
        ),
        txid=charge.txid,
        valor=float(charge.amount or 0),
    )

    kb = await button_engine.inline(SCREEN_PIX_EXPIRADO)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Gerar Novo PIX", callback_data=CB_RECHARGE_PIX_FAST)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")],
            ]
        )

    try:
        await callback.answer("⌛️ PIX expirado.", show_alert=False)
    except Exception:
        pass

    if callback.message:
        try:
            if callback.message.photo:
                await callback.message.edit_caption(
                    caption=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                await callback.message.edit_text(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
        except Exception:
            pass


# ============================================================
# 🔄 GERAR NOVO PIX
# ============================================================
@router.callback_query(F.data == CB_RECHARGE_NEW)
async def on_new_pix(callback: CallbackQuery, user: User) -> None:
    try:
        text = await _render_value_prompt(user)
        try:
            await callback.answer()
        except Exception:
            pass

        if callback.message:
            try:
                await callback.message.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

            try:
                await callback.message.answer(
                    text="⌨️ Envie o novo valor:",
                    reply_markup=build_cancel_reply(placeholder="Ex: 20"),
                )
            except Exception:
                pass

        await FlowState.set(
            user_id=user.id,
            name=FLOW_NAME_VALUE,
            data={"chat_id": callback.message.chat.id if callback.message else user.telegram_id},
            ttl=15 * 60,
        )
    except Exception as exc:
        logger.exception(f"on_new_pix falhou: {exc}")


# ============================================================
# ❌ CANCELAR PIX
# ============================================================
@router.callback_query(F.data == CB_RECHARGE_CANCEL)
async def on_cancel_pix(callback: CallbackQuery, user: User) -> None:
    try:
        state = await FlowState.get(user.id, FLOW_NAME_PIX)
        if state and state.get("txid"):
            await pix_service.cancel_charge(state["txid"])
        await FlowState.clear(user.id, FLOW_NAME_PIX)

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_recharge_home(fresh)

        try:
            await callback.answer("❌ PIX cancelado.")
        except Exception:
            pass

        if callback.message:
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                else:
                    await callback.message.edit_text(
                        text=text,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_cancel_pix falhou: {exc}")


# ============================================================
# 💠 CALLBACK DE PIX VIA URL "recharge:pix:VALOR[:product:ID[:qty:N]]"
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_RECHARGE_PIX_AMOUNT}:"))
async def on_pix_from_other_flows(callback: CallbackQuery, user: User) -> None:
    """
    Chamado por outros fluxos (ex.: compra com saldo insuficiente).
    Formato:
        recharge:pix:VALOR
        recharge:pix:VALOR:product:ID
        recharge:pix:VALOR:product:ID:qty:N
    """
    try:
        parts = callback.data.split(":")
        # parts = [recharge, pix, VALOR, ...]
        valor = 0.0
        product_id: Optional[int] = None
        quantity = 1

        if len(parts) >= 3:
            try:
                valor = float(parts[2].replace(",", "."))
            except Exception:
                valor = 0.0

        # product:ID
        try:
            if "product" in parts:
                idx = parts.index("product")
                product_id = int(parts[idx + 1])
        except Exception:
            product_id = None

        # qty:N
        try:
            if "qty" in parts:
                idx = parts.index("qty")
                quantity = int(parts[idx + 1])
        except Exception:
            quantity = 1

        if valor <= 0:
            await callback.answer("❌ Valor inválido.", show_alert=True)
            return

        try:
            await callback.answer()
        except Exception:
            pass

        # Usa a mensagem atual como base para "editar"
        # Após apagar a mensagem, criamos a nova via send_message
        msg = callback.message
        if msg is None:
            return

        # Apaga a mensagem atual (não acumular)
        try:
            await msg.delete()
        except Exception:
            pass

        # Cria mensagem "gerando" como base
        rendered_gerando = await message_engine.render(
            MSG_KEY_PIX_GERANDO,
            fallback="⏳ Gerando pagamento...",
        )

        placeholder = await callback.message.answer(
            text=rendered_gerando.content,
            parse_mode="HTML",
        )

        # Chama o serviço e em seguida renderiza o QR editando o placeholder
        result = await pix_service.create_charge(
            user_id=user.id,
            amount=valor,
            bonus=0.0,
            reserve_product_id=product_id,
            reserve_quantity=quantity,
            metadata={"source": "callback_recharge_pix"},
        )

        if not result.ok or not result.txid:
            try:
                await placeholder.edit_text(
                    text=f"❌ {result.error or 'Erro ao gerar PIX.'}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        # Salva FlowState
        await FlowState.set(
            user_id=user.id,
            name=FLOW_NAME_PIX,
            data={
                "txid": result.txid,
                "charge_id": result.charge_id,
                "amount": result.amount,
                "bonus": result.bonus,
                "chat_id": placeholder.chat.id,
                "message_id": placeholder.message_id,
            },
            ttl=settings.MP_PIX_EXPIRATION_MINUTES * 60 + 300,
        )

        # Renderiza o QR (edita o placeholder se possível)
        await _render_and_send_qr(
            bot=callback.bot,
            user=user,
            result=result,
            chat_id=placeholder.chat.id,
            edit_message_id=placeholder.message_id,
        )
    except Exception as exc:
        logger.exception(f"on_pix_from_other_flows falhou: {exc}")


# ============================================================
# /cancelar — durante recarga
# ============================================================
@router.message(Command("cancelar"))
async def on_cancel_recharge(message: Message, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_NAME_VALUE)
        state = await FlowState.get(user.id, FLOW_NAME_PIX)
        if state and state.get("txid"):
            await pix_service.cancel_charge(state["txid"])
            await FlowState.clear(user.id, FLOW_NAME_PIX)

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
# 💠 HELPER PÚBLICO (usado por /pix em commands.py)
# ============================================================
async def start_recharge_with_amount(
    message: Message,
    user: User,
    valor: float,
) -> None:
    """
    Inicia o fluxo PIX diretamente com um valor (usado por /pix <valor>).
    Não pede o valor de novo — vai direto ao QR.
    """
    if valor < settings.MIN_DEPOSIT or valor > settings.MAX_DEPOSIT:
        await message.answer(
            f"❌ Valor fora do intervalo permitido "
            f"({_fmt_brl(settings.MIN_DEPOSIT)} — {_fmt_brl(settings.MAX_DEPOSIT)}).",
            parse_mode="HTML",
        )
        return

    bonus = 0.0
    if (
        float(settings.DEPOSIT_BONUS_PERCENT or 0) > 0
        and valor >= float(settings.DEPOSIT_BONUS_MIN or 0)
    ):
        bonus = round(valor * float(settings.DEPOSIT_BONUS_PERCENT) / 100.0, 2)

    await _start_pix_flow(message=message, user=user, valor=valor, bonus=bonus)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_recharge_open",
    "on_recharge_pix_fast",
    "on_receive_recharge_value",
    "on_check_pix",
    "on_copy_pix",
    "on_new_pix",
    "on_cancel_pix",
    "on_pix_from_other_flows",
    "on_cancel_recharge",
    "start_recharge_with_amount",
    "CB_RECHARGE_OPEN",
    "CB_RECHARGE_PIX_FAST",
    "CB_RECHARGE_PIX_AMOUNT",
    "CB_RECHARGE_CHECK",
    "CB_RECHARGE_COPY",
    "CB_RECHARGE_NEW",
    "CB_RECHARGE_CANCEL",
    "FLOW_NAME_VALUE",
]
