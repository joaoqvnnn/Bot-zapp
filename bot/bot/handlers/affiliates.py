"""
Larizinha Store — Handler do Programa de Afiliados
Implementa o fluxo EXATO do DIM:

  Tela Inativo:
    • Status: Inativo
    • Botões: [✅ Ativar] [🔙 Voltar]

  Tela Ativo:
    • Status, comissão, indicações, total ganho, média, saque mínimo
    • Saldo de comissões, nível, próxima meta, link de indicação
    • Botões: [📊 Histórico de Saques] [💸 Sacar]
              [🔐 Cadastrar Senha de Saque] [🔙 Voltar]

  Histórico de Saques:
    • Vazio / Cheio (paginação)
    • [📄 Exportar PDF] [🔙 Voltar]
    • PDF salvo como "Planilha-saldo-{id}.pdf"

  Cadastrar Senha de Saque (WebApp):
    • Abre WebApp em tela minimalista (branco, sem emojis)
    • Campos: Senha, Repetir Senha, E-mail de recuperação
    • Força da senha verde/vermelho
    • Confirmação → só na tela: "✅ Senha cadastrada com sucesso"

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Toda mensagem/botão 100% editáveis pelo painel admin.
- Link de indicação sempre no formato: https://t.me/{bot}?start=ref_{id}
- Comissões lidas em tempo real do banco + Redis.
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Commission,
    CommissionStatus,
    User,
    Withdraw,
    WithdrawStatus,
    session_scope,
)
from bot.logger import audit, logger
from bot.redis_client import FlowState, Throttle
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="affiliates")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_INATIVO = "afiliados_inativo"
MSG_KEY_ATIVO = "afiliados_ativo"
MSG_KEY_HIST_VAZIO = "afiliados_hist_vazio"
MSG_KEY_HIST_CHEIO = "afiliados_hist_cheio"
MSG_KEY_PDF_OK = "afiliados_pdf_ok"

SCREEN_INATIVO = "afiliados_inativo"
SCREEN_ATIVO = "afiliados_ativo"
SCREEN_HIST = "afiliados_hist"

# Callbacks
CB_AFF_OPEN = "aff:open"
CB_AFF_ACTIVATE = "aff:activate"
CB_AFF_HIST = "aff:hist"              # + :page:N
CB_AFF_WITHDRAW = "aff:withdraw"
CB_AFF_PASSWORD = "aff:password"
CB_AFF_EXPORT_PDF = "aff:export"

HIST_PAGE_SIZE = 5


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


async def _build_affiliate_link(user: User) -> str:
    bot_username = settings.BOT_USERNAME or "seubot"
    return f"https://t.me/{bot_username}?start=ref_{user.telegram_id}"


async def _count_referrals(user_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(User.id)).where(User.referred_by_id == user_id)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _get_affiliate_stats(user_id: int) -> dict:
    """
    Retorna as estatísticas do afiliado:
    - indicações (total)
    - total ganho
    - média por indicação
    - pendentes / aprovadas / pagas / estornos
    - nível e progresso
    """
    stats = {
        "ind": 0,
        "ganho": 0.0,
        "media": 0.0,
        "pend": 0.0,
        "aprov": 0.0,
        "pagas": 0.0,
        "estornos": 0.0,
        "nivel": "iniciante",
        "meta": "—",
        "restantes": "—",
    }

    try:
        async with session_scope() as session:
            # Indicações
            r = await session.execute(
                select(func.count(User.id)).where(User.referred_by_id == user_id)
            )
            stats["ind"] = int(r.scalar() or 0)

            # Comissões por status
            for status, key in (
                (CommissionStatus.PENDING, "pend"),
                (CommissionStatus.APPROVED, "aprov"),
                (CommissionStatus.PAID, "pagas"),
                (CommissionStatus.REVERSED, "estornos"),
            ):
                res = await session.execute(
                    select(func.coalesce(func.sum(Commission.amount), 0))
                    .where(Commission.earner_id == user_id)
                    .where(Commission.status == status)
                )
                val = float(res.scalar() or 0)
                stats[key] = round(val, 2)

            stats["ganho"] = round(
                stats["pend"] + stats["aprov"] + stats["pagas"], 2
            )
            if stats["ind"] > 0:
                stats["media"] = round(stats["ganho"] / stats["ind"], 2)

            # Nível
            ures = await session.execute(select(User).where(User.id == user_id))
            u: Optional[User] = ures.scalar_one_or_none()
            if u is not None:
                stats["nivel"] = u.affiliate_level or "iniciante"

    except Exception as exc:
        logger.debug(f"_get_affiliate_stats falhou: {exc}")

    # Metas por nível (valores configuráveis no painel admin)
    metas = {
        "iniciante": (10, "🥉 Bronze"),
        "bronze": (50, "🥈 Prata"),
        "prata": (100, "🥇 Ouro"),
        "ouro": (999999, "👑 Diamante"),
    }
    proximo = {
        "iniciante": metas["iniciante"],
        "bronze": metas["bronze"],
        "prata": metas["prata"],
        "ouro": metas["ouro"],
    }.get(stats["nivel"], metas["iniciante"])
    meta_qtd, proximo_nome = proximo
    stats["meta"] = f"{proximo_nome} · {meta_qtd} indicações"
    stats["restantes"] = str(max(0, meta_qtd - stats["ind"]))

    return stats


# ============================================================
# TELA — AFILIADO INATIVO
# ============================================================
async def _render_inactive(user: User) -> Tuple[str, InlineKeyboardMarkup]:
    link = await _build_affiliate_link(user)
    rendered = await message_engine.render(
        MSG_KEY_INATIVO,
        fallback=(
            "💰 <b>PROGRAMA DE AFILIADOS</b>\n\n"
            "⚙️ Status: <b>Inativo</b>\n"
            f"🧲 Sua comissão: <b>{settings.AFFILIATE_COMMISSION_PERCENT}%</b>\n\n"
            "Ative o programa e comece a ganhar comissões a cada compra "
            "ou recarga dos seus indicados.\n\n"
            f"🔗 Seu link (após ativar): <code>{link}</code>"
        ),
        status="Inativo",
        comissao=settings.AFFILIATE_COMMISSION_PERCENT,
        bot=settings.BOT_USERNAME,
        id=user.telegram_id,
        link=link,
    )

    kb = await button_engine.inline(SCREEN_INATIVO)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Ativar", callback_data=CB_AFF_ACTIVATE)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="profile:open")],
            ]
        )
    return rendered.content, kb


# ============================================================
# TELA — AFILIADO ATIVO
# ============================================================
async def _render_active(user: User) -> Tuple[str, InlineKeyboardMarkup]:
    link = await _build_affiliate_link(user)
    stats = await _get_affiliate_stats(user.id)
    saldo_com = float(user.affiliate_balance or 0)

    rendered = await message_engine.render(
        MSG_KEY_ATIVO,
        fallback=(
            "💰 <b>PROGRAMA DE AFILIADOS</b>\n\n"
            "⚙️ Status: <b>Ativo</b>\n"
            f"🧲 Sua comissão: <b>{settings.AFFILIATE_COMMISSION_PERCENT}%</b> "
            "(de todas recargas do indicado)\n\n"
            f"👥 Indicações: {stats['ind']}\n"
            f"🪙 Total ganho: {_fmt_brl(stats['ganho'])}\n"
            f"📊 Média: {_fmt_brl(stats['media'])}\n"
            f"💰 Saque mínimo: {_fmt_brl(settings.AFFILIATE_MIN_WITHDRAW)}\n\n"
            f"🔥 Saldo de comissões: <b>{_fmt_brl(saldo_com)}</b>\n\n"
            f"🌱 Nível: {stats['nivel'].title()}\n"
            f"🎯 Próxima meta: {stats['meta']} "
            f"({stats['restantes']} restantes)\n\n"
            "ℹ️ <b>INFO:</b> Seus indicados continuarão gerando comissão para sempre.\n"
            "A comissão pode ser alterada a qualquer momento, "
            "fique atento aos avisos.\n\n"
            f"🔗 Seu link: <code>{link}</code>"
        ),
        status="Ativo",
        comissao=settings.AFFILIATE_COMMISSION_PERCENT,
        ind=stats["ind"],
        ganho=stats["ganho"],
        media=stats["media"],
        min_saque=settings.AFFILIATE_MIN_WITHDRAW,
        comissao_saldo=saldo_com,
        nivel=stats["nivel"],
        meta=stats["meta"],
        restantes=stats["restantes"],
        pend=stats["pend"],
        aprov=stats["aprov"],
        pagas=stats["pagas"],
        estornos=stats["estornos"],
        bot=settings.BOT_USERNAME,
        id=user.telegram_id,
        link=link,
    )

    kb = await button_engine.inline(SCREEN_ATIVO)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📊 Histórico de Saques", callback_data=f"{CB_AFF_HIST}:page:1")],
                [InlineKeyboardButton(text="💸 Sacar", callback_data=CB_AFF_WITHDRAW)],
                [InlineKeyboardButton(text="🔐 Cadastrar Senha de Saque", callback_data=CB_AFF_PASSWORD)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="profile:open")],
            ]
        )
    return rendered.content, kb


# ============================================================
# HANDLER: ABRIR AFILIADOS
# ============================================================
@router.callback_query(F.data == CB_AFF_OPEN)
async def on_open_affiliates(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "affiliates", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        if fresh.affiliate_active:
            text, kb = await _render_active(fresh)
        else:
            text, kb = await _render_inactive(fresh)

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
        logger.exception(f"on_open_affiliates falhou: {exc}")


# ============================================================
# HANDLER: ATIVAR AFILIADO
# ============================================================
@router.callback_query(F.data == CB_AFF_ACTIVATE)
async def on_activate_affiliate(callback: CallbackQuery, user: User) -> None:
    try:
        # Gera código de afiliado se ainda não tiver
        async with session_scope() as session:
            res = await session.execute(select(User).where(User.id == user.id))
            u: Optional[User] = res.scalar_one_or_none()
            if u is None:
                await callback.answer("❌ Usuário não encontrado.", show_alert=True)
                return

            if not u.affiliate_code:
                import secrets
                import string

                alphabet = string.ascii_uppercase + string.digits
                u.affiliate_code = "".join(secrets.choice(alphabet) for _ in range(8))

            u.affiliate_active = True
            u.affiliate_level = u.affiliate_level or "iniciante"
            session.add(u)

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_active(fresh)

        try:
            await callback.answer("✅ Programa ativado!")
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
                pass

        audit(
            action="affiliate.activated",
            user_id=user.id,
            details={"code": fresh.affiliate_code},
        )
    except Exception as exc:
        logger.exception(f"on_activate_affiliate falhou: {exc}")


# ============================================================
# HANDLER: SACAR (delega ao fluxo do saque)
# ============================================================
@router.callback_query(F.data == CB_AFF_WITHDRAW)
async def on_affiliate_withdraw(callback: CallbackQuery, user: User) -> None:
    """
    Redireciona para o fluxo de saque (withdraw.py).
    O saldo considerado no fluxo é o `affiliate_balance`.
    """
    try:
        # Delega ao handler de saque
        try:
            from bot.handlers.withdraw import on_open_withdraw

            await on_open_withdraw(callback, user)
            return
        except Exception as exc:
            logger.debug(f"delegação para withdraw falhou: {exc}")

        # Fallback
        try:
            await callback.answer(
                "💸 Use o menu Perfil → Saque para sacar.",
                show_alert=True,
            )
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"on_affiliate_withdraw falhou: {exc}")


# ============================================================
# HANDLER: CADASTRAR SENHA DE SAQUE (WebApp)
# ============================================================
@router.callback_query(F.data == CB_AFF_PASSWORD)
async def on_open_password_webapp(callback: CallbackQuery, user: User) -> None:
    """
    Abre o WebApp na tela /password (branco, minimalista, sem emojis).
    """
    try:
        # Tenta usar o helper do shop.py para gerar URL assinada
        webapp_url = ""
        try:
            from bot.handlers.shop import build_webapp_url

            webapp_url = build_webapp_url(
                user_id=user.id,
                telegram_id=user.telegram_id,
                path="/password",
            )
        except Exception:
            webapp_url = ""

        if not webapp_url:
            try:
                await callback.answer(
                    "🔐 WebApp não configurado. Contate o suporte.",
                    show_alert=True,
                )
            except Exception:
                pass
            return

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔐 Cadastrar Senha",
                        web_app=WebAppInfo(url=webapp_url),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_AFF_OPEN,
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
                    text=(
                        "🔐 <b>Cadastrar Senha de Saque</b>\n\n"
                        "Clique no botão abaixo para abrir a tela de cadastro.\n"
                        "Sua senha será usada para autorizar todos os saques."
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"on_open_password_webapp falhou: {exc}")


# ============================================================
# HISTÓRICO DE SAQUES (paginação)
# ============================================================
async def _count_withdraws(user_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(Withdraw.id)).where(Withdraw.user_id == user_id)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_withdraws_page(
    user_id: int, page: int, per_page: int
) -> List[Withdraw]:
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Withdraw)
                .where(Withdraw.user_id == user_id)
                .order_by(Withdraw.created_at.desc())
                .offset(offset)
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _render_history(user_id: int, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    total = await _count_withdraws(user_id)
    total_pages = max(1, math.ceil(total / HIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    withdraws = await _load_withdraws_page(user_id, page, HIST_PAGE_SIZE)

    if total == 0:
        rendered = await message_engine.render(
            MSG_KEY_HIST_VAZIO,
            fallback=(
                "📊 <b>HISTÓRICO DE SAQUES</b>\n\n"
                "<i>Você ainda não realizou saques.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📄 Exportar PDF", callback_data=CB_AFF_EXPORT_PDF)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_AFF_OPEN)],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["📊 <b>HISTÓRICO DE SAQUES</b>", ""]
    for w in withdraws:
        status_emoji = {
            WithdrawStatus.PENDING: "🟡",
            WithdrawStatus.PROCESSING: "⏳",
            WithdrawStatus.APPROVED: "🔵",
            WithdrawStatus.PAID: "🟢",
            WithdrawStatus.REFUSED: "🔴",
            WithdrawStatus.CANCELED: "⚪️",
        }.get(w.status, "⚪️")

        linhas.append(
            f"{status_emoji} <b>#{w.id}</b> · "
            f"{_fmt_brl(float(w.amount or 0))}\n"
            f"    <i>{_fmt_dt(w.created_at)} · {w.status.value if hasattr(w.status, 'value') else w.status}</i>"
        )

    rendered = await message_engine.render(
        MSG_KEY_HIST_CHEIO,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        pagina=page,
        total_paginas=total_pages,
        total=total,
    )

    rows: List[List[InlineKeyboardButton]] = []

    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Anterior",
                callback_data=f"{CB_AFF_HIST}:page:{page - 1}",
            )
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Próxima ➡️",
                callback_data=f"{CB_AFF_HIST}:page:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [InlineKeyboardButton(text="📄 Exportar PDF", callback_data=CB_AFF_EXPORT_PDF)]
    )
    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_AFF_OPEN)]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(f"{CB_AFF_HIST}:page:"))
async def on_history(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1

        text, kb = await _render_history(user.id, page)

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
                pass
    except Exception as exc:
        logger.exception(f"on_history (aff) falhou: {exc}")


# ============================================================
# EXPORTAR PDF — "Planilha-saldo-{id}.pdf"
# ============================================================
def _build_pdf_bytes(user: User, withdraws: List[Withdraw]) -> Optional[bytes]:
    """
    Gera o PDF do histórico de saques com reportlab.
    Retorna bytes do PDF ou None em caso de erro.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=1.5 * cm,
            rightMargin=1.5 * cm,
            topMargin=1.5 * cm,
            bottomMargin=1.5 * cm,
            title=f"Planilha-saldo-{user.id}",
            author=settings.APP_NAME,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "TitleCustom",
            parent=styles["Title"],
            fontSize=18,
            textColor=colors.HexColor("#111111"),
        )
        sub_style = ParagraphStyle(
            "SubCustom",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#555555"),
        )

        elements = []
        elements.append(
            Paragraph(
                f"<b>{settings.APP_NAME}</b> — Histórico de Saques",
                title_style,
            )
        )
        elements.append(Spacer(1, 0.4 * cm))
        elements.append(
            Paragraph(
                f"<b>Usuário:</b> {user.first_name or 'Cliente'} "
                f"(<b>ID:</b> {user.id} · TG <code>{user.telegram_id}</code>)<br/>"
                f"<b>Emitido em:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}",
                sub_style,
            )
        )
        elements.append(Spacer(1, 0.6 * cm))

        # Cabeçalho
        data = [["#", "Data", "Valor", "Chave PIX", "Status"]]
        for w in withdraws:
            data.append(
                [
                    str(w.id),
                    _fmt_dt(w.created_at),
                    _fmt_brl(float(w.amount or 0)),
                    (w.pix_key or "—")[:30],
                    str(
                        w.status.value
                        if hasattr(w.status, "value")
                        else w.status
                    ).upper(),
                ]
            )

        table = Table(data, colWidths=[1.2 * cm, 4 * cm, 3 * cm, 6 * cm, 3 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222222")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                    ("TOPPADDING", (0, 0), (-1, 0), 8),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
                ]
            )
        )
        elements.append(table)
        elements.append(Spacer(1, 0.6 * cm))

        # Rodapé
        total_valor = sum(float(w.amount or 0) for w in withdraws)
        elements.append(
            Paragraph(
                f"<b>Total de saques:</b> {len(withdraws)} · "
                f"<b>Somatório:</b> {_fmt_brl(total_valor)}",
                sub_style,
            )
        )

        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()
        return pdf_bytes
    except Exception as exc:
        logger.exception(f"_build_pdf_bytes falhou: {exc}")
        return None


@router.callback_query(F.data == CB_AFF_EXPORT_PDF)
async def on_export_pdf(callback: CallbackQuery, user: User) -> None:
    """
    Gera e envia o PDF do histórico como documento.
    Nome do arquivo: Planilha-saldo-{id}.pdf
    """
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Withdraw)
                .where(Withdraw.user_id == user.id)
                .order_by(Withdraw.created_at.desc())
                .limit(500)
            )
            withdraws: List[Withdraw] = list(res.scalars().all())

        pdf_bytes = _build_pdf_bytes(user, withdraws)
        if not pdf_bytes:
            try:
                await callback.answer("❌ Falha ao gerar PDF.", show_alert=True)
            except Exception:
                pass
            return

        filename = f"Planilha-saldo-{user.id}.pdf"

        try:
            await callback.answer("📄 Gerando PDF...")
        except Exception:
            pass

        if callback.message:
            try:
                from aiogram.types import BufferedInputFile

                await callback.message.answer_document(
                    document=BufferedInputFile(pdf_bytes, filename=filename),
                    caption="📄 <b>Histórico de Saques</b>",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.exception(f"envio do PDF falhou: {exc}")

        audit(
            action="affiliate.export_pdf",
            user_id=user.id,
            details={"withdraws": len(withdraws)},
        )
    except Exception as exc:
        logger.exception(f"on_export_pdf falhou: {exc}")


# ============================================================
# COMANDO /afiliados (atalho)
# ============================================================
@router.message(Command("afiliados"))
async def on_cmd_afiliados(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        fresh = await _reload_user(user.id) or user
        if fresh.affiliate_active:
            text, kb = await _render_active(fresh)
        else:
            text, kb = await _render_inactive(fresh)

        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/afiliados falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_affiliates",
    "on_activate_affiliate",
    "on_affiliate_withdraw",
    "on_open_password_webapp",
    "on_history",
    "on_export_pdf",
    "on_cmd_afiliados",
    "CB_AFF_OPEN",
    "CB_AFF_ACTIVATE",
    "CB_AFF_HIST",
    "CB_AFF_WITHDRAW",
    "CB_AFF_PASSWORD",
    "CB_AFF_EXPORT_PDF",
]
