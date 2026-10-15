"""
Larizinha Store — Handler de Compra
Coração do fluxo de compra.

Regras invioláveis (DIM):
- Compra com saldo suficiente → aprovação direta (baixa de saldo + entrega).
- Compra sem saldo → tela de PIX complementar (com o valor faltante).
- Comprar Mais de Um → valida quantidade vs. estoque e saldo.
- Trava REAL de concorrência via LockManager (product:{id}:lock).
- Entrega automática via ProductCredential (marca `used`).
- Mensagem de sucesso editável key="compra_ok" com:
    • Produto, Valor, Data, Vencimento
    • E-mail, Senha (revelada)
    • Nota do produto
    • Botão [🟢 Ativar] e [🔗 Acessar Site] + [🔙 Voltar]
- /cancelar a qualquer momento volta para boas-vindas.
- NUNCA acumula — sempre EDIT.
- Toda mensagem/botão 100% editáveis pelo painel admin.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
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
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    Product,
    ProductCredential,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import audit, log_payment, logger
from bot.redis_client import FlowState, LockManager, Throttle
from bot.services.cart_tracker import cart_tracker
from bot.services.channel_post import channel_post
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="buy")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_COMPRA_OK = "compra_ok"
MSG_KEY_COMPRA_PIX = "saldo_insuficiente"
MSG_KEY_COMPRA_MULTI = "comprar_mais"
MSG_KEY_MULTI_QTD = "comprar_mais_qtd"
MSG_KEY_ESTOQUE_INSUF = "comprar_mais_estoque_insuf"
MSG_KEY_CANCELAR = "cmd_cancelar"

SCREEN_COMPRA_OK = "compra_ok"

FLOW_NAME_MULTI = "buy_multi"
FLOW_NAME_SINGLE = "buy_single"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ".").replace("X", ",")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%d/%m/%Y às %H:%M")


async def _load_product(product_id: int) -> Optional[Product]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Product).where(Product.id == product_id)
            )
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _available_credentials_count(product_id: int) -> int:
    """Conta credenciais disponíveis (não usadas)."""
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


async def _get_user_purchases_count(user_id: int, product_id: int) -> int:
    """Conta quantas vezes o usuário já comprou esse produto."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(OrderItem.id))
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.user_id == user_id)
                .where(Order.status == OrderStatus.DELIVERED)
                .where(OrderItem.product_id == product_id)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


def _parse_credentials(raw: str) -> dict:
    """
    Converte o payload bruto de uma credencial em campos legíveis.
    Aceita:
      - JSON {"email": "...", "senha": "..."}
      - pares "email|senha" ou "email:senha"
      - texto simples (retorna em "senha")
    """
    if not raw:
        return {"email": "—", "senha": "—"}
    raw = raw.strip()

    # JSON
    if raw.startswith("{"):
        try:
            import json

            data = json.loads(raw)
            return {
                "email": str(data.get("email") or data.get("login") or "—"),
                "senha": str(data.get("senha") or data.get("password") or "—"),
                "extra": {k: v for k, v in data.items() if k not in ("email", "login", "senha", "password")},
            }
        except Exception:
            pass

    # pipe ou dois-pontos
    for sep in ("|", ":", ";"):
        if sep in raw:
            parts = raw.split(sep, 1)
            return {"email": parts[0].strip(), "senha": parts[1].strip()}

    # texto simples
    return {"email": "—", "senha": raw}


# ============================================================
# 🔒 RESERVA DE ESTOQUE + TRAVA
# ============================================================
async def _reserve_credential(product_id: int) -> Optional[ProductCredential]:
    """
    Reserva 1 credencial atomicamente.
    Retorna o registro reservado (ainda não marcado como usado).
    """
    try:
        async with session_scope() as session:
            # SELECT ... FOR UPDATE SKIP LOCKED para concorrência real
            stmt = (
                select(ProductCredential)
                .where(ProductCredential.product_id == product_id)
                .where(ProductCredential.used == False)  # noqa: E712
                .order_by(ProductCredential.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            res = await session.execute(stmt)
            cred = res.scalar_one_or_none()
            if cred is None:
                return None
            # Marca como usado agora (atômico)
            cred.used = True
            cred.used_at = datetime.now(timezone.utc)
            session.add(cred)
            session.flush()
            return cred
    except Exception as exc:
        logger.exception(f"_reserve_credential falhou: {exc}")
        return None


async def _release_credential(cred_id: int) -> None:
    """Devolve credencial (rollback)."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(ProductCredential).where(ProductCredential.id == cred_id)
            )
            cred = res.scalar_one_or_none()
            if cred:
                cred.used = False
                cred.used_at = None
                cred.order_id = None
                session.add(cred)
    except Exception:
        pass


# ============================================================
# 💰 BAIXA DE SALDO (atômica)
# ============================================================
async def _debit_balance(
    *,
    user_id: int,
    amount: float,
    description: str,
    reference: Optional[str] = None,
) -> bool:
    """Debita saldo e cria Transaction. Retorna False se saldo insuficiente."""
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.id == user_id).with_for_update()
            )
            user: Optional[User] = res.scalar_one_or_none()
            if user is None:
                return False
            if float(user.balance or 0) < amount:
                return False

            new_balance = round(float(user.balance) - amount, 2)
            user.balance = Decimal(str(new_balance))
            user.total_spent = Decimal(
                str(round(float(user.total_spent or 0) + amount, 2))
            )
            session.add(user)

            tx = Transaction(
                user_id=user.id,
                type=TransactionType.PURCHASE,
                amount=Decimal(str(round(amount, 2))),
                balance_after=Decimal(str(new_balance)),
                description=description,
                reference=reference,
            )
            session.add(tx)
            return True
    except Exception as exc:
        logger.exception(f"_debit_balance falhou: {exc}")
        return False


# ============================================================
# 🧾 MONTAR COMPRA (order + items)
# ============================================================
async def _create_order(
    *,
    user_id: int,
    product: Product,
    quantity: int,
    unit_price: float,
    creds: List[dict],
    method: PaymentMethod = PaymentMethod.BALANCE,
) -> Optional[Order]:
    """Cria o Order + OrderItem com as credenciais entregues."""
    try:
        total = round(unit_price * quantity, 2)
        expires = None
        if product.expiration_days:
            expires = datetime.now(timezone.utc) + timedelta(days=int(product.expiration_days))

        async with session_scope() as session:
            order = Order(
                user_id=user_id,
                total=Decimal(str(total)),
                discount=Decimal("0"),
                final_total=Decimal(str(total)),
                method=method,
                status=OrderStatus.DELIVERED,
                delivered_at=datetime.now(timezone.utc),
                expires_at=expires,
                metadata_={
                    "product_id": int(product.id),
                    "product_name": product.name,
                    "quantity": quantity,
                },
            )
            session.add(order)
            await session.flush()

            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                product_name=product.name,
                unit_price=Decimal(str(round(unit_price, 2))),
                quantity=quantity,
                total=Decimal(str(total)),
                credentials=creds,
            )
            session.add(item)

            # Atualiza contadores do produto
            res = await session.execute(
                select(Product).where(Product.id == product.id)
            )
            dbp: Optional[Product] = res.scalar_one_or_none()
            if dbp is not None:
                dbp.sold_count = int(dbp.sold_count or 0) + quantity
                if not dbp.unlimited:
                    dbp.stock = max(0, int(dbp.stock or 0) - quantity)
                    if dbp.stock <= 0:
                        dbp.sold_out = True
                session.add(dbp)

            # Atualiza contadores do usuário
            ures = await session.execute(
                select(User).where(User.id == user_id)
            )
            u: Optional[User] = ures.scalar_one_or_none()
            if u is not None:
                u.purchases_count = int(u.purchases_count or 0) + 1
                session.add(u)

            await session.flush()
            order_id = int(order.id)

        # Recarrega fora da sessão para devolver
        async with session_scope() as session:
            res = await session.execute(
                select(Order).where(Order.id == order_id)
            )
            return res.scalar_one_or_none()
    except Exception as exc:
        logger.exception(f"_create_order falhou: {exc}")
        return None


# ============================================================
# 📨 MENSAGEM DE SUCESSO (editável)
# ============================================================
async def _render_success(
    *,
    order: Order,
    product: Product,
    user: User,
    creds: List[dict],
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Renderiza a mensagem de sucesso (key="compra_ok").
    Mostra e-mail e senha, nota do produto e botões de ação.
    """
    # Primeira credencial para exibir (caso 1 unidade)
    first = creds[0] if creds else {"email": "—", "senha": "—"}

    venc = "—"
    if order.expires_at:
        venc = order.expires_at.astimezone().strftime("%d/%m/%Y")

    rendered = await message_engine.render(
        MSG_KEY_COMPRA_OK,
        fallback=(
            "✅ <b>Compra realizada com sucesso!</b>\n"
            f"🛍 Produto: {product.name}\n"
            f"💰 Valor: {_fmt_brl(float(order.final_total or 0))}\n"
            f"📆 Data: {_fmt_dt(order.created_at)}\n"
            f"⏰ Vencimento: {venc}\n\n"
            f"📧 E-mail: <code>{first.get('email', '—')}</code>\n"
            f"🔐 Senha: <code>{first.get('senha', '—')}</code>\n\n"
            f"📃 {product.note_product or ''}\n\n"
            "🔴 Status: DESATIVADO"
        ),
        nome=product.name,
        preco=float(order.final_total or 0),
        data=_fmt_dt(order.created_at),
        vencimento=venc,
        email=first.get("email", "—"),
        senha=first.get("senha", "—"),
        nota_produto=product.note_product or "",
        nota_ativacao=product.note_activation or "",
        link_ativacao=product.activation_link or "",
        garantia=int(product.warranty_days or 0),
    )

    # Teclado: tenta o do painel primeiro; fallback → padrão do DIM
    keyboard = await button_engine.inline(SCREEN_COMPRA_OK)
    if keyboard is None:
        rows: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="🟢 Ativar",
                    callback_data=f"activate:{int(order.id)}",
                )
            ],
        ]
        if product.activation_link:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🔗 Acessar Site",
                        url=product.activation_link,
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data="catalog:open",
                )
            ]
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)

    return rendered.content, keyboard


# ============================================================
# 🛒 COMPRA ÚNICA — ponto de entrada (chamado pelo catálogo)
# ============================================================
async def execute_single_purchase(callback: CallbackQuery, user: User, product_id: int) -> None:
    """
    Fluxo:
      1) Carrega produto.
      2) Verifica limite por usuário.
      3) Verifica estoque.
      4) Tenta reservar trava (LockManager) — evita duplo clique.
      5) Verifica saldo.
      6) Se suficiente → debita + reserva credencial + cria Order + sucesso.
      7) Se insuficiente → tela de PIX complementar.
      8) Commit lock / release lock conforme sucesso.
    """
    # ---------- Trava de ação ----------
    if not await LockManager.acquire_action(user.id, "buy", ttl=15):
        await callback.answer("⏳ Aguarde, processando...", show_alert=False)
        return

    try:
        product = await _load_product(product_id)
        if product is None:
            await callback.answer("❌ Produto não encontrado.", show_alert=True)
            return

        preco = float(product.price_promo or product.price or 0)

        # ---------- Limite por usuário ----------
        if int(product.per_user_limit or 0) > 0:
            bought = await _get_user_purchases_count(user.id, product.id)
            if bought >= int(product.per_user_limit):
                await callback.answer(
                    f"⚠️ Limite atingido ({product.per_user_limit} por usuário).",
                    show_alert=True,
                )
                return

        # ---------- Estoque ----------
        if not product.unlimited:
            available = await _available_credentials_count(int(product.id))
            if available <= 0:
                # Produto esgotado → mensagem editável
                rendered = await message_engine.render(
                    "produto_esgotado",
                    fallback=(
                        f"❌ <b>{product.name} esgotado</b>\n\n"
                        "Assim que reabastecermos, você será avisado."
                    ),
                    nome_produto=product.name,
                )
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔔 Me avise quando voltar",
                                callback_data=f"alerts:toggle:{product.id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔙 Voltar",
                                callback_data="catalog:open",
                            )
                        ],
                    ]
                )
                try:
                    await callback.answer()
                except Exception:
                    pass
                if callback.message:
                    await callback.message.edit_text(
                        text=rendered.content,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_web_page_preview=True,
                    )
                return

        # ---------- Reserva de trava REAL no produto ----------
        lock_token = await LockManager.reserve_stock(
            product_id=int(product.id),
            user_id=user.id,
            quantity=1,
            ttl=settings.PRODUCT_LOCK_TTL,
        )
        if lock_token is None:
            await callback.answer(
                "⏳ Este produto está sendo comprado agora. Tente novamente em instantes.",
                show_alert=True,
            )
            return

        # ---------- Saldo ----------
        saldo = float(user.balance or 0)

        if saldo >= preco:
            # ---------- SUCESSO DIRETO ----------
            ok_debit = await _debit_balance(
                user_id=user.id,
                amount=preco,
                description=f"Compra: {product.name}",
            )
            if not ok_debit:
                await LockManager.release_lock(int(product.id), user.id)
                await callback.answer("❌ Saldo insuficiente.", show_alert=True)
                return

            # Reserva credencial
            cred = await _reserve_credential(int(product.id))
            if cred is None:
                # rollback
                await _credit_back(user.id, preco, "Estorno: sem estoque disponível")
                await LockManager.release_lock(int(product.id), user.id)
                await callback.answer(
                    "❌ Estoque esgotado no momento da compra.",
                    show_alert=True,
                )
                return

            creds_payload = [_parse_credentials(cred.payload)]
            order = await _create_order(
                user_id=user.id,
                product=product,
                quantity=1,
                unit_price=preco,
                creds=creds_payload,
                method=PaymentMethod.BALANCE,
            )

            # Vincula credencial ao pedido
            if order is not None:
                try:
                    async with session_scope() as session:
                        res = await session.execute(
                            select(ProductCredential).where(ProductCredential.id == cred.id)
                        )
                        c: Optional[ProductCredential] = res.scalar_one_or_none()
                        if c is not None:
                            c.order_id = order.id
                            session.add(c)
                except Exception:
                    pass

            # Commit lock (vira venda)
            await LockManager.commit_lock(int(product.id), user.id)

            # Cancela carrinho abandonado
            try:
                await cart_tracker.mark_converted(user.id, int(product.id))
            except Exception:
                pass

            # Libera view
            try:
                await LockManager.release_view(user.id)
            except Exception:
                pass

            # ---------- Mensagem de sucesso ----------
            if order is None:
                await callback.answer(
                    "✅ Compra aprovada, mas houve erro ao registrar o pedido.",
                    show_alert=True,
                )
                return

            text, keyboard = await _render_success(
                order=order, product=product, user=user, creds=creds_payload
            )

            try:
                await callback.answer("✅ Compra realizada!")
            except Exception:
                pass

            if callback.message:
                try:
                    await callback.message.edit_text(
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    await callback.message.answer(
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )

            # ---------- Auto-post no canal ----------
            try:
                await channel_post.post_buy(
                    callback.bot,
                    product_id=int(product.id),
                    product_name=product.name,
                    user_id=user.id,
                    user_name=user.first_name or "Cliente",
                    validity=f"{int(product.expiration_days or 30)}d",
                    status="Acesso liberado",
                )
            except Exception:
                pass

            # ---------- Notificar admin (opcional, sem acumular) ----------
            try:
                for admin_id in settings.admin_ids_list[:3]:
                    try:
                        await callback.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"💰 <b>Nova venda</b>\n\n"
                                f"👤 {user.first_name or 'Cliente'} "
                                f"(<code>{user.telegram_id}</code>)\n"
                                f"📦 {product.name}\n"
                                f"💵 {_fmt_brl(preco)}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            log_payment(
                "purchase.completed",
                user_id=user.id,
                amount=preco,
                status="paid",
                details={"product_id": int(product.id), "order_id": int(order.id)},
            )
            return

        # ---------- SALDO INSUFICIENTE ----------
        await LockManager.release_lock(int(product.id), user.id)
        faltam = round(preco - saldo, 2)

        rendered = await message_engine.render(
            MSG_KEY_COMPRA_PIX,
            fallback=(
                "❌ <b>Saldo insuficiente!</b>\n\n"
                f"💰 Seu saldo: {_fmt_brl(saldo)}\n"
                f"💵 Valor do produto: {_fmt_brl(preco)}\n"
                f"📉 Faltam: {_fmt_brl(faltam)}\n\n"
                f"💡 Deseja gerar um PIX no valor de {_fmt_brl(faltam)} "
                f"para completar a compra?"
            ),
            saldo=saldo,
            preco=preco,
            falta=faltam,
            produto=product.name,
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💠 Gerar PIX de {_fmt_brl(faltam)}",
                        callback_data=f"recharge:pix:{faltam}:product:{product.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data="catalog:open",
                    )
                ],
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
                await callback.message.answer(
                    text=rendered.content,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
    except Exception as exc:
        logger.exception(f"execute_single_purchase falhou: {exc}")
        try:
            await callback.answer("❌ Erro ao processar compra.", show_alert=True)
        except Exception:
            pass
    finally:
        await LockManager.release_action(user.id, "buy")


# ============================================================
# 🧮 ESTORNO INTERNO
# ============================================================
async def _credit_back(user_id: int, amount: float, description: str) -> None:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(User).where(User.id == user_id).with_for_update()
            )
            u: Optional[User] = res.scalar_one_or_none()
            if u is None:
                return
            new_balance = round(float(u.balance or 0) + amount, 2)
            u.balance = Decimal(str(new_balance))
            u.total_spent = Decimal(
                str(max(0.0, float(u.total_spent or 0) - amount))
            )
            session.add(u)

            tx = Transaction(
                user_id=u.id,
                type=TransactionType.REFUND,
                amount=Decimal(str(round(amount, 2))),
                balance_after=Decimal(str(new_balance)),
                description=description,
            )
            session.add(tx)
    except Exception:
        pass


# ============================================================
# 🛒 COMPRAR MAIS DE UM
# ============================================================
async def start_multi_purchase(callback: CallbackQuery, user: User, product_id: int) -> None:
    """
    Inicia fluxo multi: salva FlowState e pede a quantidade por Reply.
    """
    product = await _load_product(product_id)
    if product is None:
        await callback.answer("❌ Produto não encontrado.", show_alert=True)
        return

    estoque = await _available_credentials_count(int(product.id))
    if not product.unlimited and estoque <= 0:
        await callback.answer("❌ Estoque esgotado.", show_alert=True)
        return

    preco = float(product.price_promo or product.price or 0)

    # Salva estado
    await FlowState.set(
        user_id=user.id,
        name=FLOW_NAME_MULTI,
        data={
            "product_id": int(product.id),
            "product_name": product.name,
            "price": preco,
            "stock": estoque,
            "chat_id": callback.message.chat.id if callback.message else user.telegram_id,
        },
        ttl=15 * 60,
    )

    rendered = await message_engine.render(
        MSG_KEY_COMPRA_MULTI,
        fallback=(
            "🛒 <b>Comprar Mais de Um</b>\n\n"
            f"📦 Produto: <b>{product.name}</b>\n"
            f"💵 Preço unitário: {_fmt_brl(preco)}\n"
            f"📊 Estoque disponível: {estoque if not product.unlimited else '∞'}\n\n"
            "Quantos logins deseja comprar?\n\n"
            "💡 Digite /cancelar a qualquer momento para sair."
        ),
        nome_produto=product.name,
        preco=preco,
        estoque=estoque,
    )

    # Reply keyboard para o usuário digitar o número
    from bot.keyboards.reply import build_cancel_reply

    kb = build_cancel_reply(placeholder="Digite a quantidade...")

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

        # Envia uma mensagem nova (reply) para forçar o teclado
        try:
            await callback.message.answer(
                text="⌨️ Envie a quantidade:",
                reply_markup=kb,
            )
        except Exception:
            pass


# ============================================================
# RECEBER A QUANTIDADE (mensagem em texto)
# ============================================================
@router.message(F.text & ~F.text.startswith("/"))
async def on_receive_quantity(message: Message, user: User) -> None:
    """
    Só trata a mensagem se o usuário estiver no fluxo de compra múltipla.
    Caso contrário, ignora (deixa outros handlers rodarem).
    """
    state = await FlowState.get(user.id, FLOW_NAME_MULTI)
    if not state:
        return

    raw = (message.text or "").strip()

    # Cancelar
    if raw.lower() in {"❌ cancelar", "cancelar", "sair"}:
        await FlowState.clear(user.id, FLOW_NAME_MULTI)
        try:
            await message.delete()
        except Exception:
            pass
        try:
            from bot.handlers.commands import _send_welcome  # type: ignore

            await _send_welcome(message, user)
        except Exception:
            await message.answer("✅ Operação cancelada.")
        return

    # Parse da quantidade
    try:
        qty = int(raw)
        if qty <= 0:
            raise ValueError
    except Exception:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.answer(
                "❌ Quantidade inválida. Envie um número inteiro positivo.",
            )
        except Exception:
            pass
        return

    # Apaga a mensagem do usuário (por segurança/limpeza)
    try:
        await message.delete()
    except Exception:
        pass

    # Checa estoque
    stock = int(state.get("stock", 0))
    product_id = int(state.get("product_id", 0))
    price = float(state.get("price", 0))
    product = await _load_product(product_id)
    if product is None:
        await FlowState.clear(user.id, FLOW_NAME_MULTI)
        await message.answer("❌ Produto não encontrado.")
        return

    if not product.unlimited and qty > stock:
        rendered = await message_engine.render(
            MSG_KEY_ESTOQUE_INSUF,
            fallback=(
                f"❌ Quantidade maior que o estoque disponível.\n\n"
                f"📦 Estoque atual: {stock}"
            ),
            estoque=stock,
            qtd=qty,
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
        )
        return

    total = round(price * qty, 2)
    saldo = float(user.balance or 0)

    if total > saldo:
        faltam = round(total - saldo, 2)
        rendered = await message_engine.render(
            MSG_KEY_COMPRA_PIX,
            fallback=(
                "❌ <b>Saldo insuficiente!</b>\n\n"
                f"💰 Seu saldo: {_fmt_brl(saldo)}\n"
                f"💵 Valor total: {_fmt_brl(total)}\n"
                f"📉 Faltam: {_fmt_brl(faltam)}\n\n"
                f"💡 Deseja gerar um PIX no valor de {_fmt_brl(faltam)} "
                f"para completar a compra?"
            ),
            saldo=saldo,
            preco=total,
            falta=faltam,
            produto=product.name,
            qtd=qty,
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💠 Gerar PIX de {_fmt_brl(faltam)}",
                        callback_data=f"recharge:pix:{faltam}:product:{product_id}:qty:{qty}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data="catalog:open",
                    )
                ],
            ]
        )
        await message.answer(
            text=rendered.content,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    # ---------- Reserva trava do produto ----------
    lock_token = await LockManager.reserve_stock(
        product_id=product_id,
        user_id=user.id,
        quantity=qty,
        ttl=settings.PRODUCT_LOCK_TTL,
    )
    if lock_token is None:
        await message.answer(
            "⏳ Produto reservado por outro usuário. Tente novamente em instantes."
        )
        return

    # ---------- Debita ----------
    ok = await _debit_balance(
        user_id=user.id,
        amount=total,
        description=f"Compra: {qty}x {product.name}",
    )
    if not ok:
        await LockManager.release_lock(product_id, user.id)
        await message.answer("❌ Saldo insuficiente.")
        return

    # ---------- Reserva credenciais ----------
    creds_payload: List[dict] = []
    reserved_ids: List[int] = []
    for _ in range(qty):
        c = await _reserve_credential(product_id)
        if c is None:
            # rollback
            for rid in reserved_ids:
                await _release_credential(rid)
            await _credit_back(user.id, total, "Estorno: estoque insuficiente")
            await LockManager.release_lock(product_id, user.id)
            await message.answer("❌ Estoque insuficiente no momento.")
            return
        reserved_ids.append(int(c.id))
        creds_payload.append(_parse_credentials(c.payload))

    # ---------- Cria pedido ----------
    order = await _create_order(
        user_id=user.id,
        product=product,
        quantity=qty,
        unit_price=price,
        creds=creds_payload,
        method=PaymentMethod.BALANCE,
    )
    if order is None:
        for rid in reserved_ids:
            await _release_credential(rid)
        await _credit_back(user.id, total, "Estorno: erro ao registrar pedido")
        await LockManager.release_lock(product_id, user.id)
        await message.answer("❌ Erro ao registrar pedido.")
        return

    # Vincula credenciais ao pedido
    try:
        async with session_scope() as session:
            for rid in reserved_ids:
                res = await session.execute(
                    select(ProductCredential).where(ProductCredential.id == rid)
                )
                c: Optional[ProductCredential] = res.scalar_one_or_none()
                if c is not None:
                    c.order_id = order.id
                    session.add(c)
    except Exception:
        pass

    # Commit lock
    await LockManager.commit_lock(product_id, user.id)

    # Cancelar carrinho abandonado
    try:
        await cart_tracker.mark_converted(user.id, product_id)
    except Exception:
        pass

    # Limpa FlowState
    await FlowState.clear(user.id, FLOW_NAME_MULTI)

    # ---------- Mensagem de sucesso ----------
    text, keyboard = await _render_success(
        order=order, product=product, user=user, creds=creds_payload
    )

    await message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    log_payment(
        "purchase.completed.multi",
        user_id=user.id,
        amount=total,
        status="paid",
        details={"product_id": product_id, "qty": qty, "order_id": int(order.id)},
    )


# ============================================================
# /cancelar — durante fluxo de compra
# ============================================================
@router.message(Command("cancelar"))
async def on_cancel(message: Message, user: User) -> None:
    try:
        await FlowState.clear(user.id, FLOW_NAME_MULTI)
        await FlowState.clear(user.id, FLOW_NAME_SINGLE)
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass
    try:
        from bot.handlers.commands import _send_welcome  # type: ignore

        await _send_welcome(message, user)
    except Exception:
        await message.answer("✅ Operação cancelada.")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "execute_single_purchase",
    "start_multi_purchase",
    "on_receive_quantity",
    "on_cancel",
    "FLOW_NAME_MULTI",
    "FLOW_NAME_SINGLE",
]
