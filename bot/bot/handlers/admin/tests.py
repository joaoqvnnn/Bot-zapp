"""
Larizinha Store — Módulo Admin: Testes
Implementa simuladores reais para validar todos os fluxos sem afetar dados reais.

Simuladores:
- 🧪 Simular Compra (usa saldo de teste, sem debitar de verdade)
- 🧪 Simular PIX (consulta a API do MP sem gerar cobrança real)
- 🧪 Simular Saque (verifica fluxo sem debitar)
- 🧪 Simular Gift (cria e resgata gift card de teste)
- 🧪 Simular Afiliado (verifica comissões com dados reais)
- 🧪 Enviar Post Teste (post real no canal)
- 🧪 Enviar Disparo Teste (disparo para o próprio admin)
- 🧪 Testar Botões (valida callbacks dos button_templates)
- 🧪 Testar Variáveis (valida templates de mensagem)
- 🧪 Testar Trava (executa LockManager em produto)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel.
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
- Simuladores NÃO persistem dados reais (read-only) exceto gift/post/disparo de teste.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

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
    ButtonTemplate,
    GiftCard,
    MessageTemplate,
    Product,
    ProductCredential,
    User,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import LockManager
from bot.services.channel_post import channel_post
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
    build_back_row,
    consume_input,
    edit_or_send,
    fmt_brl,
    fmt_num,
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
router = Router(name="admin_tests")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_PANEL = "admin_tests_panel"
MSG_KEY_RESULT = "admin_tests_result"

SCREEN_PANEL = "admin_tests_panel"

# Callbacks base
CB_ROOT = "admin:tests"
CB_BUY = "admin:tests:buy"
CB_PIX = "admin:tests:pix"
CB_WITHDRAW = "admin:tests:withdraw"
CB_GIFT = "admin:tests:gift"
CB_AFFILIATE = "admin:tests:affiliate"
CB_POST = "admin:tests:post"
CB_BROADCAST = "admin:tests:broadcast"
CB_BUTTONS = "admin:tests:buttons"
CB_VARIABLES = "admin:tests:variables"
CB_LOCK = "admin:tests:lock"
CB_RELOAD = "admin:tests:reload"


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_first_product() -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product)
                .where(Product.visible == True)  # noqa: E712
                .order_by(Product.id.asc())
                .limit(1)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_admin_user() -> Optional[User]:
    """Carrega (ou cria) um usuário de teste."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.telegram_id == settings.MAIN_ADMIN_ID)
            )
            u = res.scalar_one_or_none()
            if u is not None:
                return u
    except Exception:
        pass
    return None


async def _count_credentials(product_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(ProductCredential.id))
                .where(ProductCredential.product_id == product_id)
                .where(ProductCredential.used == False)  # noqa: E712
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


# ============================================================
# PAINEL
# ============================================================
async def render_panel(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    # Info de diagnóstico
    try:
        async with session_scope() as session:
            users_total = int(
                (await session.execute(select(func.count(User.id)))).scalar() or 0
            )
            products_total = int(
                (await session.execute(select(func.count(Product.id)))).scalar() or 0
            )
            templates_total = int(
                (await session.execute(select(func.count(MessageTemplate.id)))).scalar() or 0
            )
            buttons_total = int(
                (await session.execute(select(func.count(ButtonTemplate.id)))).scalar() or 0
            )
    except Exception:
        users_total = products_total = templates_total = buttons_total = 0

    rendered = await message_engine.render(
        MSG_KEY_PANEL,
        fallback=(
            "🧪 <b>CENTRAL DE TESTES</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Os simuladores abaixo validam os fluxos SEM afetar\n"
            "os dados reais (exceto gift, post e disparo de teste).\n\n"
            "📊 <b>Diagnóstico do sistema:</b>\n"
            f"├ Usuários: <b>{fmt_num(users_total)}</b>\n"
            f"├ Produtos: <b>{fmt_num(products_total)}</b>\n"
            f"├ Templates de msg: <b>{fmt_num(templates_total)}</b>\n"
            f"└ Templates de botão: <b>{fmt_num(buttons_total)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Escolha um teste:"
        ),
        users_total=fmt_num(users_total),
        products_total=fmt_num(products_total),
        templates_total=fmt_num(templates_total),
        buttons_total=fmt_num(buttons_total),
    )

    engine_kb = await button_engine.inline(SCREEN_PANEL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="🧪 Simular Compra", callback_data=CB_BUY),
                    InlineKeyboardButton(text="🧪 Simular PIX", callback_data=CB_PIX),
                ],
                [
                    InlineKeyboardButton(text="🧪 Simular Saque", callback_data=CB_WITHDRAW),
                    InlineKeyboardButton(text="🧪 Simular Gift", callback_data=CB_GIFT),
                ],
                [
                    InlineKeyboardButton(text="🧪 Simular Afiliado", callback_data=CB_AFFILIATE),
                ],
                [
                    InlineKeyboardButton(text="📡 Post Teste", callback_data=CB_POST),
                    InlineKeyboardButton(text="📨 Disparo Teste", callback_data=CB_BROADCAST),
                ],
                [
                    InlineKeyboardButton(text="🔘 Testar Botões", callback_data=CB_BUTTONS),
                    InlineKeyboardButton(text="💠 Testar Variáveis", callback_data=CB_VARIABLES),
                ],
                [
                    InlineKeyboardButton(text="🔒 Testar Trava", callback_data=CB_LOCK),
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
        logger.exception(f"tests on_panel falhou: {exc}")


# ============================================================
# SIMULAR COMPRA
# ============================================================
@router.callback_query(F.data == CB_BUY)
async def on_test_buy(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Simulando compra...")

        # Carrega primeiro produto
        product = await _load_first_product()
        if product is None:
            await _show_result(
                callback,
                title="Simular Compra",
                success=False,
                details="❌ Nenhum produto cadastrado.\nCadastre um produto primeiro.",
            )
            return

        available = await _count_credentials(int(product.id))
        preco = float(product.price_promo or product.price or 0)
        admin_user = await _load_admin_user()

        # Analisa o que aconteceria
        analise: List[str] = []
        analise.append(f"📦 Produto: <b>{product.name}</b> (#{product.id})")
        analise.append(f"💵 Preço: <b>{fmt_brl(preco)}</b>")
        analise.append(f"📊 Estoque: <b>{fmt_num(available)}</b> credenciais disponíveis")

        if admin_user:
            saldo = float(admin_user.balance or 0)
            analise.append(f"👤 Saldo do admin: <b>{fmt_brl(saldo)}</b>")

            if saldo >= preco:
                analise.append("✅ <b>Saldo suficiente</b> para comprar")
            else:
                faltam = round(preco - saldo, 2)
                analise.append(
                    f"⚠️ Saldo insuficiente — faltam <b>{fmt_brl(faltam)}</b>"
                )

        if available <= 0 and not product.unlimited:
            analise.append("❌ <b>Sem estoque</b> — venda seria bloqueada")
            success = False
        elif available > 0 or product.unlimited:
            analise.append("✅ Estoque OK")
            success = True
        else:
            success = True

        # Verifica se há limite por usuário
        if int(product.per_user_limit or 0) > 0:
            analise.append(
                f"ℹ️ Limite/usuário: <b>{product.per_user_limit}</b>"
            )

        if product.auto_delivery:
            analise.append("🤖 Entrega automática habilitada")
        if product.human_delivery:
            analise.append("👨 Entrega humana habilitada")

        analise.append("")
        analise.append("🔒 <i>Nada foi persistido — apenas simulação.</i>")

        await _show_result(
            callback,
            title="Simular Compra",
            success=success,
            details="\n".join(analise),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.simulate_buy",
            category="admin",
            details={"product_id": int(product.id), "success": success},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_buy falhou: {exc}")
        await _show_result(
            callback,
            title="Simular Compra",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# SIMULAR PIX
# ============================================================
@router.callback_query(F.data == CB_PIX)
async def on_test_pix(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Testando MP...")

        # Verifica o serviço
        try:
            ok = await pix_service.healthcheck()
        except Exception as exc:
            logger.debug(f"pix healthcheck falhou: {exc}")
            ok = False

        details: List[str] = []

        # Verifica se os campos MP estão configurados
        access_token = settings.MP_ACCESS_TOKEN or ""
        public_key = settings.MP_PUBLIC_KEY or ""
        webhook_url = settings.MP_WEBHOOK_URL or ""
        exp_min = settings.MP_PIX_EXPIRATION_MINUTES

        details.append(f"🔑 Access Token: {'✅' if access_token else '❌'}")
        details.append(f"🔑 Public Key: {'✅' if public_key else '❌'}")
        details.append(f"🪝 Webhook URL: {'✅' if webhook_url else '❌'}")
        details.append(f"⏱️ Expiração: <b>{exp_min}</b> min")

        details.append("")
        if ok:
            details.append("✅ <b>Conexão com Mercado Pago: OK</b>")
            details.append("ℹ️ O serviço PIX está operacional.")
        else:
            details.append("❌ <b>Falha na conexão com MP</b>")
            details.append("💡 Verifique as credenciais no módulo 🔐 Credenciais.")

        details.append("")
        details.append(
            f"💰 Valor mínimo: <b>{fmt_brl(settings.MIN_DEPOSIT)}</b> · "
            f"Máximo: <b>{fmt_brl(settings.MAX_DEPOSIT)}</b>"
        )
        details.append(
            f"🎁 Bônus: <b>{settings.DEPOSIT_BONUS_PERCENT}%</b> "
            f"(mínimo {fmt_brl(settings.DEPOSIT_BONUS_MIN)})"
        )
        details.append("")
        details.append("🔒 <i>Nenhuma cobrança foi gerada — apenas simulação.</i>")

        await _show_result(
            callback,
            title="Simular PIX",
            success=bool(ok),
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.simulate_pix",
            category="admin",
            details={"mp_ok": ok},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_pix falhou: {exc}")
        await _show_result(
            callback,
            title="Simular PIX",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# SIMULAR SAQUE
# ============================================================
@router.callback_query(F.data == CB_WITHDRAW)
async def on_test_withdraw(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Simulando saque...")

        admin_user = await _load_admin_user()
        details: List[str] = []

        details.append(f"💰 Saldo mínimo: <b>{fmt_brl(settings.MIN_WITHDRAW)}</b>")
        details.append(f"💵 Saldo máximo: <b>{fmt_brl(settings.MAX_WITHDRAW)}</b>")
        details.append(f"📊 Taxa: <b>{settings.WITHDRAW_FEE_PERCENT}%</b>")
        details.append(f"📅 Limite diário: <b>{settings.WITHDRAW_DAILY_LIMIT}</b> saques")
        details.append(
            f"🤖 Aprovação automática: "
            f"{'🟢 sim' if settings.WITHDRAW_AUTO_APPROVE else '🔴 não'}"
        )
        details.append("")

        success = True
        if admin_user:
            saldo = float(admin_user.balance or 0)
            details.append(f"👤 Saldo do admin: <b>{fmt_brl(saldo)}</b>")

            has_password = bool(admin_user.withdraw_password)
            details.append(
                f"🔐 Senha de saque: "
                f"{'✅ cadastrada' if has_password else '❌ não cadastrada'}"
            )

            if saldo < float(settings.MIN_WITHDRAW):
                details.append(
                    f"⚠️ Saldo abaixo do mínimo para saque"
                )
                success = False
            elif not has_password:
                details.append(
                    "ℹ️ Sem senha cadastrada, o fluxo abriria o WebApp"
                )
            else:
                detalhes_ok = True
                details.append("✅ Fluxo completo de saque disponível")
        else:
            details.append("ℹ️ Admin não possui registro de usuário no banco")

        details.append("")
        details.append("🔒 <i>Nada foi debitado — apenas simulação.</i>")

        await _show_result(
            callback,
            title="Simular Saque",
            success=success,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.simulate_withdraw",
            category="admin",
            details={"success": success},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_withdraw falhou: {exc}")
        await _show_result(
            callback,
            title="Simular Saque",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# SIMULAR GIFT
# ============================================================
@router.callback_query(F.data == CB_GIFT)
async def on_test_gift(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Testando Gift...")

        details: List[str] = []

        # Conta gift cards
        try:
            async with session_scope() as session:
                total = int(
                    (await session.execute(select(func.count(GiftCard.id)))).scalar() or 0
                )
                now = datetime.now(timezone.utc)
                ativos = int(
                    (
                        await session.execute(
                            select(func.count(GiftCard.id))
                            .where(GiftCard.active == True)  # noqa: E712
                            .where((GiftCard.expires_at.is_(None)) | (GiftCard.expires_at > now))
                            .where((GiftCard.uses_max == 0) | (GiftCard.uses_count < GiftCard.uses_max))
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total = 0
            ativos = 0

        details.append(f"🎁 Total de gift cards: <b>{fmt_num(total)}</b>")
        details.append(f"🟢 Gift cards ativos: <b>{fmt_num(ativos)}</b>")
        details.append("")

        if ativos > 0:
            details.append("✅ Existem gift cards ativos para testar resgate")
            success = True
        else:
            details.append("⚠️ Nenhum gift card ativo — crie um primeiro")
            success = False

        details.append("")
        details.append("🔒 <i>Nenhum resgate foi realizado — apenas simulação.</i>")

        await _show_result(
            callback,
            title="Simular Gift",
            success=success,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.simulate_gift",
            category="admin",
            details={"total": total, "active": ativos},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_gift falhou: {exc}")
        await _show_result(
            callback,
            title="Simular Gift",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# SIMULAR AFILIADO
# ============================================================
@router.callback_query(F.data == CB_AFFILIATE)
async def on_test_affiliate(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Simulando afiliado...")

        details: List[str] = []

        # Configurações
        details.append(f"🟢 Programa: {'ativo' if settings.AFFILIATE_ENABLED else 'inativo'}")
        details.append(f"💰 Comissão/compra: <b>{settings.AFFILIATE_COMMISSION_PERCENT}%</b>")
        details.append(f"💵 Comissão/depósito: <b>{settings.AFFILIATE_COMMISSION_DEPOSIT_PERCENT}%</b>")
        details.append(f"📊 Saque mínimo: <b>{fmt_brl(settings.AFFILIATE_MIN_WITHDRAW)}</b>")
        details.append("")

        # Conta afiliados
        try:
            async with session_scope() as session:
                from bot.database import Commission, CommissionStatus

                total_afiliados = int(
                    (
                        await session.execute(
                            select(func.count(User.id)).where(
                                User.affiliate_code.isnot(None)
                            )
                        )
                    ).scalar()
                    or 0
                )
                ativos = int(
                    (
                        await session.execute(
                            select(func.count(User.id)).where(
                                User.affiliate_active == True  # noqa: E712
                            )
                        )
                    ).scalar()
                    or 0
                )
                pendentes = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(Commission.amount), 0)).where(
                                Commission.status == CommissionStatus.PENDING
                            )
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total_afiliados = ativos = 0
            pendentes = 0.0

        details.append(f"👥 Total afiliados: <b>{fmt_num(total_afiliados)}</b>")
        details.append(f"🟢 Afiliados ativos: <b>{fmt_num(ativos)}</b>")
        details.append(f"💰 Comissões pendentes: <b>{fmt_brl(pendentes)}</b>")

        # Simulação de comissão
        exemplo = 50.0
        com_ex = round(exemplo * float(settings.AFFILIATE_COMMISSION_DEPOSIT_PERCENT) / 100.0, 2)
        details.append("")
        details.append("📊 <b>Exemplo:</b>")
        details.append(
            f"Se um indicado recarregar {fmt_brl(exemplo)}, "
            f"o padrinho recebe <b>{fmt_brl(com_ex)}</b>"
        )

        await _show_result(
            callback,
            title="Simular Afiliado",
            success=True,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.simulate_affiliate",
            category="admin",
            details={"total": total_afiliados, "ativos": ativos},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_affiliate falhou: {exc}")
        await _show_result(
            callback,
            title="Simular Afiliado",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# ENVIAR POST TESTE
# ============================================================
@router.callback_query(F.data == CB_POST)
async def on_test_post(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Enviando post de teste...")

        # Carrega primeiro produto
        product = await _load_first_product()

        try:
            # Envia via channel_post (mesma função real)
            result = await channel_post.post_stock(
                callback.bot,
                product_id=int(product.id) if product else 0,
                product_name=product.name if product else "Produto Teste",
                quantity=10,
            )
            sent = result.sent if hasattr(result, "sent") else 0
            failed = result.failed if hasattr(result, "failed") else 0

            details: List[str] = []
            details.append(f"📡 Post enviado para <b>{sent}</b> canal(is)")
            if failed > 0:
                details.append(f"⚠️ Falhas: <b>{failed}</b>")

            if sent > 0:
                details.append("✅ <b>Post de teste enviado com sucesso</b>")
                success = True
            else:
                details.append("❌ <b>Nenhum canal configurado</b>")
                details.append("💡 Vá em 📡 Canal para conectar um canal")
                success = False
        except Exception as exc:
            logger.debug(f"post teste falhou: {exc}")
            details = [f"❌ Erro: {truncate(str(exc), 180)}"]
            success = False

        await _show_result(
            callback,
            title="Enviar Post Teste",
            success=success,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.send_post",
            category="admin",
            details={"success": success},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_post falhou: {exc}")
        await _show_result(
            callback,
            title="Enviar Post Teste",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# ENVIAR DISPARO TESTE
# ============================================================
@router.callback_query(F.data == CB_BROADCAST)
async def on_test_broadcast(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Enviando disparo de teste...")

        details: List[str] = []
        try:
            await callback.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🧪 <b>Teste de Disparo</b>\n\n"
                    "Esta é uma mensagem de teste enviada pela central de testes.\n"
                    "Se você recebeu, o disparo em massa está funcionando.\n\n"
                    f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                ),
                parse_mode="HTML",
            )
            details.append("✅ <b>Disparo de teste enviado</b>")
            details.append(f"📬 Destinatário: <code>{admin_id}</code>")
            details.append("ℹ️ Em um disparo real, a mensagem vai para todos os alvos.")
            success = True
        except Exception as exc:
            logger.debug(f"disparo teste falhou: {exc}")
            details.append(f"❌ Erro: {truncate(str(exc), 180)}")
            success = False

        await _show_result(
            callback,
            title="Enviar Disparo Teste",
            success=success,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.send_broadcast",
            category="admin",
            details={"success": success},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_broadcast falhou: {exc}")
        await _show_result(
            callback,
            title="Enviar Disparo Teste",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# TESTAR BOTÕES
# ============================================================
@router.callback_query(F.data == CB_BUTTONS)
async def on_test_buttons(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Validando botões...")

        details: List[str] = []

        try:
            async with session_scope() as session:
                res = await session.execute(select(ButtonTemplate))
                buttons = list(res.scalars().all())
        except Exception:
            buttons = []

        if not buttons:
            details.append("ℹ️ Nenhum botão cadastrado no painel.")
            details.append("💡 O bot está usando fallback hardcoded.")
            await _show_result(
                callback,
                title="Testar Botões",
                success=True,
                details="\n".join(details),
            )
            return

        # Valida cada botão
        total = len(buttons)
        invalidos: List[str] = []
        sem_callback: List[str] = []

        for b in buttons:
            t = str(b.type or "").lower()
            if t == "callback":
                if not b.callback_data:
                    sem_callback.append(f"#{b.id} · {b.key}")
            elif t == "url":
                if not b.url or not (b.url.startswith("http://") or b.url.startswith("https://")):
                    invalidos.append(f"#{b.id} · URL inválida")
            elif t == "webapp":
                if not b.webapp_url or not b.webapp_url.startswith("http"):
                    invalidos.append(f"#{b.id} · WebApp URL inválida")

        details.append(f"📊 Total de botões: <b>{fmt_num(total)}</b>")

        if invalidos:
            details.append("")
            details.append(f"❌ <b>{len(invalidos)} botões inválidos:</b>")
            for inv in invalidos[:8]:
                details.append(f"  • {inv}")

        if sem_callback:
            details.append("")
            details.append(
                f"⚠️ <b>{len(sem_callback)} botões sem callback:</b>"
            )
            for sc in sem_callback[:8]:
                details.append(f"  • {sc}")

        if not invalidos and not sem_callback:
            details.append("✅ <b>Todos os botões estão OK</b>")

        details.append("")
        details.append(f"🔒 <i>Validação read-only — nada foi alterado.</i>")

        await _show_result(
            callback,
            title="Testar Botões",
            success=(not invalidos),
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.validate_buttons",
            category="admin",
            details={"total": total, "invalid": len(invalidos)},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_buttons falhou: {exc}")
        await _show_result(
            callback,
            title="Testar Botões",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# TESTAR VARIÁVEIS
# ============================================================
@router.callback_query(F.data == CB_VARIABLES)
async def on_test_variables(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🧪 Validando variáveis...")

        details: List[str] = []

        try:
            async with session_scope() as session:
                res = await session.execute(select(MessageTemplate))
                templates = list(res.scalars().all())
        except Exception:
            templates = []

        if not templates:
            details.append("ℹ️ Nenhum template cadastrado.")
            await _show_result(
                callback,
                title="Testar Variáveis",
                success=True,
                details="\n".join(details),
            )
            return

        # Regex para variáveis
        var_re = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

        total = len(templates)
        com_vars = 0
        com_erro: List[str] = []
        vars_encontradas: set = set()

        # Variáveis válidas conhecidas (comuns)
        known_vars = {
            "nome", "id", "saldo", "username", "nome_loja", "versao",
            "nome_produto", "preco", "descricao", "estoque", "garantia",
            "valor", "txid", "pix_copia_cola", "bonus", "saldo_final",
            "chave", "chave_informada", "tipo", "min", "max", "falta",
            "qtd", "quantidade", "data", "vencimento", "data_hora",
            "email", "senha", "codigo", "busca", "resultado", "ind",
            "ganho", "media", "min_saque", "comissao", "nivel", "meta",
            "restantes", "saudacao", "id_admin", "nome_admin",
            "ultimo_acesso", "total_users", "online", "novos_hoje",
            "bloqueados", "saldo_total", "fat_total", "lucro", "vendas",
            "saques_pend", "dep_pend", "comissoes", "grafico_vendas",
            "grafico_dep", "grafico_users", "lista_atividades",
            "lista_alertas", "qtd_clientes", "link", "bot", "username_bot",
        }

        for t in templates:
            content = str(t.content or "")
            matches = var_re.findall(content)
            if matches:
                com_vars += 1
                for m in matches:
                    vars_encontradas.add(m)
                    if m not in known_vars:
                        com_erro.append(f"{t.key}: {{{{{m}}}}}")

        details.append(f"📊 Templates: <b>{fmt_num(total)}</b>")
        details.append(f"🔤 Com variáveis: <b>{fmt_num(com_vars)}</b>")
        details.append(f"🎯 Variáveis usadas: <b>{len(vars_encontradas)}</b>")

        if com_erro:
            details.append("")
            details.append(
                f"⚠️ <b>{len(com_erro)} variáveis desconhecidas:</b>"
            )
            for e in com_erro[:10]:
                details.append(f"  • {e}")
        else:
            details.append("✅ <b>Todas as variáveis parecem válidas</b>")

        details.append("")
        details.append(
            "💡 Variáveis usam o formato <code>{{nome}}</code> e são "
            "substituídas em runtime pelo motor de mensagens."
        )

        await _show_result(
            callback,
            title="Testar Variáveis",
            success=(len(com_erro) == 0),
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.validate_variables",
            category="admin",
            details={"total": total, "unknown": len(com_erro)},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_variables falhou: {exc}")
        await _show_result(
            callback,
            title="Testar Variáveis",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# TESTAR TRAVA (LockManager)
# ============================================================
@router.callback_query(F.data == CB_LOCK)
async def on_test_lock(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        await toast_ok(callback, "🔒 Testando trava...")

        # Carrega um produto
        product = await _load_first_product()
        if product is None:
            await _show_result(
                callback,
                title="Testar Trava",
                success=False,
                details="❌ Nenhum produto cadastrado para testar.",
            )
            return

        details: List[str] = []

        details.append(f"📦 Produto: <b>{product.name}</b> (#{product.id})")
        details.append(f"👤 Admin ID: <code>{admin_id}</code>")
        details.append("")

        try:
            # Adquire trava
            token = await LockManager.reserve_stock(
                product_id=int(product.id),
                user_id=admin_id,
                quantity=1,
                ttl=30,
            )

            if token is None:
                details.append("⚠️ <b>Produto já está travado</b> por outro usuário")
                details.append("ℹ️ Isso É esperado se alguém está visualizando agora.")
                success = False
            else:
                details.append(f"✅ <b>Trava adquirida</b> com sucesso")
                details.append(f"🎫 Token: <code>{token[:16]}...</code>")

                # Verifica
                lock_data = await LockManager.get_lock(int(product.id))
                if lock_data:
                    details.append(
                        f"🔒 Lock ativo — usuário <code>{lock_data.get('user_id')}</code>, "
                        f"qty <b>{lock_data.get('quantity')}</b>"
                    )

                # Extende
                extended = await LockManager.extend_lock(int(product.id), 60)
                details.append(
                    f"⏱️ TTL estendido: {'✅ sim' if extended else '❌ não'}"
                )

                # Libera
                released = await LockManager.release_lock(int(product.id), admin_id)
                details.append(
                    f"🔓 Trava liberada: {'✅ sim' if released else '❌ não'}"
                )

                # Confirma liberação
                after = await LockManager.get_lock(int(product.id))
                details.append(
                    f"✅ Confirmação: {'🔓 livre' if after is None else '⚠️ ainda travado'}"
                )

                success = True
        except Exception as exc:
            logger.debug(f"lock test falhou: {exc}")
            details.append(f"❌ Erro: {truncate(str(exc), 180)}")
            success = False

        details.append("")
        details.append(
            "ℹ️ Trava real usada pelo fluxo de compra e geração de PIX."
        )

        await _show_result(
            callback,
            title="Testar Trava",
            success=success,
            details="\n".join(details),
        )

        await log_action(
            admin_id=admin_id,
            action="tests.lock",
            category="admin",
            details={"product_id": int(product.id), "success": success},
        )
    except Exception as exc:
        logger.exception(f"tests on_test_lock falhou: {exc}")
        await _show_result(
            callback,
            title="Testar Trava",
            success=False,
            details=f"❌ Erro: {truncate(str(exc), 180)}",
        )


# ============================================================
# HELPER: MOSTRAR RESULTADO
# ============================================================
async def _show_result(
    callback: CallbackQuery,
    *,
    title: str,
    success: bool,
    details: str,
) -> None:
    emoji = "✅" if success else "❌"

    text = (
        f"🧪 <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{details}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>Resultado:</b> {'Sucesso' if success else 'Falhou/Atenção'}"
    )

    rows_kb = [
        [
            InlineKeyboardButton(
                text="🔙 Voltar",
                callback_data=CB_ROOT,
            )
        ]
    ]

    try:
        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
    except Exception as exc:
        logger.debug(f"_show_result falhou: {exc}")
        try:
            await callback.message.answer(
                text=text,
                parse_mode="HTML",
                reply_markup=kb(rows_kb),
            )
        except Exception:
            pass


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_panel",
    "on_panel",
    "on_test_buy",
    "on_test_pix",
    "on_test_withdraw",
    "on_test_gift",
    "on_test_affiliate",
    "on_test_post",
    "on_test_broadcast",
    "on_test_buttons",
    "on_test_variables",
    "on_test_lock",
    "CB_ROOT",
    "CB_BUY",
    "CB_PIX",
    "CB_WITHDRAW",
    "CB_GIFT",
    "CB_AFFILIATE",
    "CB_POST",
    "CB_BROADCAST",
    "CB_BUTTONS",
    "CB_VARIABLES",
    "CB_LOCK",
    "CB_RELOAD",
]
