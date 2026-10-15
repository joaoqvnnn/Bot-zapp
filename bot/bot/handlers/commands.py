"""
Larizinha Store — Comandos do Telegram
Implementa todos os comandos:

    /start       → bloqueio de canal (se aplicável) ou boas-vindas
    /pix <valor> → gerar PIX de recarga
    /historico   → histórico de compras
    /alertas     → notificações de estoque
    /afiliados   → programa de afiliados
    /id          → mostra ID do usuário
    /saldo       → saldo atual
    /ranking     → ranking do bot
    /termos      → termos de uso
    /admin       → acesso ao painel administrativo
    /cancelar    → cancela fluxo em andamento

Regras invioláveis:
- TODAS as mensagens são editáveis via message_engine (key por comando).
- NUNCA acumula: em callback, edita; em comando, envia (não há o que editar).
- Botões são renderizados via button_engine (screen por comando).
- Se o usuário estiver em um fluxo (FlowState), /cancelar limpa e volta às boas-vindas.
"""
from __future__ import annotations

from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.config import settings
from bot.database import Order, OrderItem, OrderStatus, User, session_scope
from bot.logger import logger
from bot.redis_client import FlowState, LockManager, Presence
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="commands")


# ============================================================
# 🔑 CHAVES DE MENSAGEM / TELAS
# ============================================================
MSG_BOAS_VINDAS = "boas_vindas"
SCREEN_BOAS_VINDAS = "boas_vindas"

MSG_CMD_ID = "cmd_id"
MSG_CMD_SALDO = "cmd_saldo"
MSG_CMD_TERMOS = "cmd_termos"
MSG_CMD_ADMIN_OK = "admin_entrada"
MSG_CMD_ADMIN_DENY = "admin_negado"
MSG_CMD_CANCELAR = "cmd_cancelar"

MSG_CMD_PIX_FORMATO = "cmd_pix_formato"
MSG_CMD_HISTORICO = "cmd_historico"
MSG_CMD_ALERTAS = "cmd_alertas"
MSG_CMD_AFILIADOS = "cmd_afiliados"
MSG_CMD_RANKING = "cmd_ranking"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _fmt_brl(value: float) -> str:
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _mask_id(uid: int) -> str:
    return f"<code>{uid}</code>"


# ============================================================
# /start
# ============================================================
@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject, user: User) -> None:
    """
    /start com deep-link (ex.: /start ref_123). Trata referral e cai no start padrão.
    """
    payload = (command.args or "").strip()
    if payload.startswith("ref_"):
        try:
            referrer_tg = int(payload[4:])
            await _bind_referral(user, referrer_tg)
        except Exception as exc:
            logger.debug(f"referral inválido: {payload} — {exc}")

    await _send_welcome(message, user)


@router.message(CommandStart())
async def cmd_start(message: Message, user: User) -> None:
    """Comando /start padrão — boas-vindas."""
    await _send_welcome(message, user)


async def _send_welcome(message: Message, user: User) -> None:
    """
    Envia a mensagem de boas-vindas.
    O bloqueio de canal (se aplicável) já foi tratado no middleware.
    """
    try:
        # apaga a mensagem /start do usuário para não acumular
        try:
            await message.delete()
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_BOAS_VINDAS,
            fallback=(
                f"🎬 <b>Bem-vindo(a) à {settings.APP_NAME}</b> ✨\n\n"
                f"👤 ID: <code>{user.telegram_id}</code>\n"
                f"💰 Saldo: {_fmt_brl(float(user.balance or 0))}\n\n"
                f"👇 Use os botões abaixo para começar."
            ),
            nome=user.first_name or "Cliente",
            id=user.telegram_id,
            saldo=float(user.balance or 0),
            nome_loja=settings.APP_NAME,
            versao=settings.APP_VERSION,
            qtd_clientes=1000,
        )

        keyboard = await button_engine.inline(SCREEN_BOAS_VINDAS)

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        await Presence.beat(user.telegram_id)
    except Exception as exc:
        logger.exception(f"/start falhou: {exc}")


async def _bind_referral(user: User, referrer_telegram_id: int) -> None:
    """
    Associa o usuário ao padrinho (se ainda não tiver).
    """
    if user.referred_by_id:
        return
    if referrer_telegram_id == user.telegram_id:
        return

    try:
        async with session_scope() as session:
            # Verifica se o padrinho existe
            from sqlalchemy import select

            res = await session.execute(
                select(User).where(User.telegram_id == referrer_telegram_id)
            )
            referrer = res.scalar_one_or_none()
            if referrer is None:
                return

            # Atualiza o usuário atual
            res2 = await session.execute(
                select(User).where(User.id == user.id)
            )
            u = res2.scalar_one_or_none()
            if u is None or u.referred_by_id:
                return
            u.referred_by_id = referrer.id
            session.add(u)

            logger.bind(user_id=user.id).info(
                f"Referral vinculado: {user.id} → {referrer.id}"
            )
    except Exception as exc:
        logger.debug(f"_bind_referral falhou: {exc}")


# ============================================================
# /id
# ============================================================
@router.message(Command("id"))
async def cmd_id(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_CMD_ID,
            fallback=(
                "🆔 <b>Seu ID</b>\n\n"
                f"<code>{user.telegram_id}</code>\n\n"
                "Use este ID para receber recargas ou suporte."
            ),
            id=user.telegram_id,
            nome=user.first_name or "Cliente",
        )

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
        )
    except Exception as exc:
        logger.exception(f"/id falhou: {exc}")


# ============================================================
# /saldo
# ============================================================
@router.message(Command("saldo"))
async def cmd_saldo(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_CMD_SALDO,
            fallback=(
                "╭───────────────────╮\n"
                f"💰 Carteira id: <code>{user.id}</code>\n"
                f"💸 Saldo: <b>{_fmt_brl(float(user.balance or 0))}</b>\n"
                "╰───────────────────╯"
            ),
            id=user.id,
            saldo=float(user.balance or 0),
            saldo_bloqueado=float(user.balance_locked or 0),
        )

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
        )
    except Exception as exc:
        logger.exception(f"/saldo falhou: {exc}")


# ============================================================
# /termos
# ============================================================
@router.message(Command("termos"))
async def cmd_termos(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_CMD_TERMOS,
            fallback=(
                "📜 <b>TERMOS DE USO</b>\n\n"
                "Ao utilizar nossos serviços, você declara estar de acordo com:\n\n"
                "• Uso pessoal e intransferível\n"
                "• Não compartilhar credenciais\n"
                "• Não solicitar reembolso após entrega\n"
                "• Respeitar garantia informada no produto\n\n"
                f"© {settings.APP_NAME} · v{settings.APP_VERSION}"
            ),
            nome_loja=settings.APP_NAME,
            versao=settings.APP_VERSION,
        )

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/termos falhou: {exc}")


# ============================================================
# /historico
# ============================================================
@router.message(Command("historico"))
async def cmd_historico(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        # Carrega pedidos entregues
        try:
            async with session_scope() as session:
                from sqlalchemy import select

                res = await session.execute(
                    select(Order)
                    .where(Order.user_id == user.id)
                    .where(Order.status == OrderStatus.DELIVERED)
                    .order_by(Order.created_at.desc())
                    .limit(10)
                )
                orders = list(res.scalars().all())
        except Exception as exc:
            logger.debug(f"carregar histórico falhou: {exc}")
            orders = []

        if not orders:
            rendered = await message_engine.render(
                "historico_vazio",
                fallback="📜 <b>Você ainda não realizou compras.</b>\n\nVisite o catálogo para começar!",
                nome=user.first_name or "Cliente",
            )
        else:
            linhas = ["📜 <b>ÚLTIMAS COMPRAS</b>", ""]
            for o in orders:
                data = o.created_at.strftime("%d/%m/%Y") if o.created_at else "—"
                linhas.append(
                    f"• #{o.id} — {_fmt_brl(float(o.final_total or 0))} — {data}"
                )
            rendered = await message_engine.render(
                "historico_cheio",
                fallback="\n".join(linhas),
                lista="\n".join(linhas),
                total=len(orders),
            )

        keyboard = await button_engine.inline("historico")

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/historico falhou: {exc}")


# ============================================================
# /alertas
# ============================================================
@router.message(Command("alertas"))
async def cmd_alertas(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        # Lista de produtos que o usuário está seguindo
        seguindo = list(user.alerts_enabled or [])

        rendered = await message_engine.render(
            MSG_CMD_ALERTAS,
            fallback=(
                "⚠️ <b>Sistema de alertas</b>\n\n"
                "Seja notificado quando seu serviço favorito for abastecido 🤩\n\n"
                "Selecione abaixo os produtos que deseja acompanhar:"
            ),
            total=len(seguindo),
        )

        keyboard = await button_engine.inline("alerts")

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/alertas falhou: {exc}")


# ============================================================
# /afiliados
# ============================================================
@router.message(Command("afiliados"))
async def cmd_afiliados(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        ativo = bool(user.affiliate_active)
        key = "afiliados_ativo" if ativo else "afiliados_inativo"

        link = (
            f"https://t.me/{settings.BOT_USERNAME}?start=ref_{user.telegram_id}"
            if settings.BOT_USERNAME
            else f"ref_{user.telegram_id}"
        )

        rendered = await message_engine.render(
            key,
            fallback=(
                "💰 <b>PROGRAMA DE AFILIADOS</b>\n\n"
                f"⚙️ Status: {'Ativo' if ativo else 'Inativo'}\n"
                f"🧲 Sua comissão: {settings.AFFILIATE_COMMISSION_PERCENT}%\n"
                f"👥 Indicações: —\n"
                f"🪙 Total ganho: R$ 0,00\n"
                f"🔗 Seu link: <code>{link}</code>"
            ),
            status="Ativo" if ativo else "Inativo",
            comissao=settings.AFFILIATE_COMMISSION_PERCENT,
            ind=0,
            ganho=float(user.affiliate_balance or 0),
            media=0.0,
            min_saque=settings.AFFILIATE_MIN_WITHDRAW,
            comissao_saldo=float(user.affiliate_balance or 0),
            nivel=user.affiliate_level or "iniciante",
            meta="—",
            restantes="—",
            bot=settings.BOT_USERNAME,
            id=user.telegram_id,
            link=link,
        )

        keyboard = await button_engine.inline("afiliados")

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/afiliados falhou: {exc}")


# ============================================================
# /ranking
# ============================================================
@router.message(Command("ranking"))
async def cmd_ranking(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        # Ranking: top 10 produtos mais vendidos (mês atual)
        linhas = ["🏆 <b>Ranking dos serviços mais vendidos</b>", ""]
        try:
            from sqlalchemy import func, select

            async with session_scope() as session:
                res = await session.execute(
                    select(
                        OrderItem.product_name,
                        func.sum(OrderItem.quantity).label("qtd"),
                    )
                    .join(Order, Order.id == OrderItem.order_id)
                    .where(Order.status == OrderStatus.DELIVERED)
                    .group_by(OrderItem.product_name)
                    .order_by(func.sum(OrderItem.quantity).desc())
                    .limit(10)
                )
                rows = res.all()

            if not rows:
                linhas.append("<i>Sem dados ainda.</i>")
            else:
                medals = ["🥇", "🥈", "🥉"]
                for i, (nome, qtd) in enumerate(rows, start=1):
                    medal = medals[i - 1] if i <= 3 else ""
                    linhas.append(f"{i}°) {nome} {medal} — {int(qtd)} pedidos")
        except Exception as exc:
            logger.debug(f"ranking falhou: {exc}")
            linhas.append("<i>Sem dados disponíveis.</i>")

        rendered = await message_engine.render(
            MSG_CMD_RANKING,
            fallback="\n".join(linhas),
            lista="\n".join(linhas),
        )

        keyboard = await button_engine.inline("ranking")

        await message.answer(
            text=rendered.content,
            parse_mode=rendered.parse_mode or "HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/ranking falhou: {exc}")


# ============================================================
# /pix <valor>
# ============================================================
@router.message(Command("pix"))
async def cmd_pix(message: Message, user: User, command: CommandObject) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        raw = (command.args or "").strip().replace(",", ".")
        if not raw:
            # formato incorreto → mostra exemplo
            rendered = await message_engine.render(
                MSG_CMD_PIX_FORMATO,
                fallback=(
                    "⚠️ <b>Formato incorreto</b>\n\n"
                    "Envie: <code>/pix 10</code>\n"
                    "ou: <code>/pix 5.25</code>"
                ),
            )
            await message.answer(
                text=rendered.content,
                parse_mode=rendered.parse_mode or "HTML",
            )
            return

        try:
            valor = float(raw)
        except ValueError:
            rendered = await message_engine.render(
                MSG_CMD_PIX_FORMATO,
                fallback="⚠️ Valor inválido. Exemplo: <code>/pix 10</code>",
            )
            await message.answer(
                text=rendered.content,
                parse_mode=rendered.parse_mode or "HTML",
            )
            return

        # Delegação: encaminha para o handler de recarga via callback interno
        # Aqui, para simplificar, enviamos a mensagem de "informe valor"
        # O fluxo PIX real é tratado em handlers/recharge.py
        from bot.handlers.recharge import start_recharge_with_amount  # opcional

        try:
            await start_recharge_with_amount(message, user, valor)
            return
        except Exception:
            # se ainda não existe o handler, apenas avisa
            await message.answer(
                f"💠 Para gerar um PIX de <b>{_fmt_brl(valor)}</b>, use o botão "
                "💠 Recarregar Saldo.",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.exception(f"/pix falhou: {exc}")


# ============================================================
# /cancelar
# ============================================================
@router.message(Command("cancelar"))
async def cmd_cancelar(message: Message, user: User) -> None:
    """Cancela qualquer fluxo em andamento e volta para boas-vindas."""
    try:
        try:
            await message.delete()
        except Exception:
            pass

        # Limpa todos os FlowStates do usuário
        for name in ("pix", "withdraw", "gift", "recharge", "buy", "search"):
            try:
                await FlowState.clear(user_id=user.id, name=name)
            except Exception:
                pass

        # Libera travas de view
        try:
            await LockManager.release_view(user.id)
        except Exception:
            pass

        rendered = await message_engine.render(
            MSG_CMD_CANCELAR,
            fallback="✅ Operação cancelada.\n\nVoltando ao início...",
            nome=user.first_name or "Cliente",
        )

        # Envia a confirmação, depois as boas-vindas (após 1s)
        try:
            tmp = await message.answer(
                text=rendered.content,
                parse_mode=rendered.parse_mode or "HTML",
            )
            import asyncio

            async def _auto():
                await asyncio.sleep(2)
                try:
                    await tmp.delete()
                except Exception:
                    pass
                # reenvia boas-vindas
                try:
                    await _send_welcome(message, user)
                except Exception:
                    pass

            asyncio.create_task(_auto())
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"/cancelar falhou: {exc}")


# ============================================================
# /admin
# ============================================================
@router.message(Command("admin"))
async def cmd_admin(message: Message, user: User) -> None:
    """
    Entrada do painel admin.
    Se não for admin → mostra mensagem de negado (editável).
    Se for admin → delega ao handler do painel.
    """
    try:
        try:
            await message.delete()
        except Exception:
            pass

        is_admin = user.telegram_id in settings.admin_ids_list
        if not is_admin:
            rendered = await message_engine.render(
                MSG_CMD_ADMIN_DENY,
                fallback="⛔ Você não tem permissão para acessar o painel.",
            )
            await message.answer(
                text=rendered.content,
                parse_mode=rendered.parse_mode or "HTML",
            )
            return

        # Delega para o handler do painel admin
        try:
            from bot.handlers.admin import open_admin_panel  # opcional

            await open_admin_panel(message, user)
            return
        except Exception:
            # fallback: mostra mensagem bruta
            await message.answer(
                "🛡️ <b>Painel Administrativo</b>\n\n"
                "Use o menu abaixo para navegar.",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.exception(f"/admin falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "cmd_start",
    "cmd_start_deeplink",
    "cmd_id",
    "cmd_saldo",
    "cmd_termos",
    "cmd_historico",
    "cmd_alertas",
    "cmd_afiliados",
    "cmd_ranking",
    "cmd_pix",
    "cmd_cancelar",
    "cmd_admin",
]
