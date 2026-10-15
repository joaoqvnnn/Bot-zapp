"""
Larizinha Store — Módulo Admin: Dashboard
Implementa a mensagem EXATA do DIM, com DADOS REAIS do banco em runtime.

    📊 DASHBOARD · {{data_hora}}

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    👥 USUÁRIOS
    ├ Total: {{total_users}}
    ├ 🟢 Online: {{online}}
    ├ 🆕 Novos hoje: {{novos_hoje}}
    ├ 🚫 Bloqueados: {{bloqueados}}
    └ 💰 Saldo total: R$ {{saldo_total}}
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    💵 FINANCEIRO
    ├ Faturamento total: R$ {{fat_total}}
    ├ Lucro líquido: R$ {{lucro}}
    ├ Vendas realizadas: {{vendas}}
    ├ Saques pendentes: {{saques_pend}}
    ├ Depósitos pendentes: {{dep_pend}}
    └ Comissões geradas: R$ {{comissoes}}
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    📈 GRÁFICOS
    ├ Vendas 7d: {{grafico_vendas}}
    ├ Depósitos 7d: {{grafico_dep}}
    └ Usuários 7d: {{grafico_users}}
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    🕐 ATIVIDADES RECENTES
    {{lista_atividades}}
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ⚠️ ALERTAS DO SISTEMA
    {{lista_alertas}}

Botões: [🔄 Atualizar] [📈 Gráficos] [📊 Relatórios] [🕐 Atividades] [⚠️ Alertas] [🔙 Voltar]

Regras invioláveis:
- NUNCA acumula: navegação sempre via EDIT.
- Tudo editável pelo painel admin (key="admin_dashboard").
- Todos os dados são consultados AO VIVO (sem cache fictício).
- Online é via Presence.online_count() (Redis).
- Gráficos são mini-sparklines em ASCII (blocos ▁▂▃▄▅▆▇█).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Alert,
    Commission,
    CommissionStatus,
    Log,
    Order,
    OrderStatus,
    PixCharge,
    PixStatus,
    Product,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    Withdraw,
    WithdrawStatus,
    session_scope,
)
from bot.logger import logger
from bot.redis_client import Counters, Presence
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine
from bot.handlers.admin.helpers import (
    SYM_ACTIVE,
    SYM_BLOCKED,
    SYM_BANNED,
    SYM_DONE,
    SYM_PENDING,
    SYM_WARN,
    answer,
    edit_or_send,
    fmt_ago,
    fmt_brl,
    fmt_dt,
    fmt_num,
    kb,
    load_admin,
    render_screen,
    toast_ok,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_dashboard")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_DASHBOARD = "admin_dashboard"
SCREEN_DASHBOARD = "admin_dashboard"

CB_DASHBOARD = "admin:dashboard"
CB_DASH_REFRESH = "admin:dashboard:refresh"
CB_DASH_CHARTS = "admin:dashboard:charts"
CB_DASH_REPORTS = "admin:dashboard:reports"
CB_DASH_ACTIVITY = "admin:dashboard:activity"
CB_DASH_ALERTS = "admin:dashboard:alerts"


# ============================================================
# 🧰 SPARKLINE
# ============================================================
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float]) -> str:
    """
    Gera um mini gráfico em ASCII a partir de uma sequência numérica.
    Ex.: [1,2,3,5,8] → "▁▃▄▆█"
    """
    if not values:
        return "—"
    try:
        vmax = max(values)
        vmin = min(values)
    except Exception:
        return "—"

    if vmax == vmin:
        return _SPARK_BLOCKS[0] * len(values)

    span = vmax - vmin
    out: List[str] = []
    for v in values:
        idx = int(round(((v - vmin) / span) * (len(_SPARK_BLOCKS) - 1)))
        idx = max(0, min(len(_SPARK_BLOCKS) - 1, idx))
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


# ============================================================
# 📥 COLETA DE DADOS (tudo real do banco)
# ============================================================
async def _collect_users_stats() -> Dict[str, Any]:
    """Estatísticas de usuários."""
    now = datetime.now(timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    out: Dict[str, Any] = {
        "total_users": 0,
        "novos_hoje": 0,
        "bloqueados": 0,
        "banidos": 0,
        "ativos": 0,
        "saldo_total": 0.0,
        "saldo_bloqueado_total": 0.0,
    }

    try:
        async with session_scope() as session:
            # total
            res = await session.execute(select(func.count(User.id)))
            out["total_users"] = int(res.scalar() or 0)

            # novos hoje
            res = await session.execute(
                select(func.count(User.id)).where(User.created_at >= start_today)
            )
            out["novos_hoje"] = int(res.scalar() or 0)

            # bloqueados
            res = await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.BLOCKED)
            )
            out["bloqueados"] = int(res.scalar() or 0)

            # banidos
            res = await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.BANNED)
            )
            out["banidos"] = int(res.scalar() or 0)

            # ativos
            res = await session.execute(
                select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)
            )
            out["ativos"] = int(res.scalar() or 0)

            # saldo total
            res = await session.execute(
                select(func.coalesce(func.sum(User.balance), 0))
            )
            out["saldo_total"] = float(res.scalar() or 0)

            # saldo bloqueado
            res = await session.execute(
                select(func.coalesce(func.sum(User.balance_locked), 0))
            )
            out["saldo_bloqueado_total"] = float(res.scalar() or 0)

    except Exception as exc:
        logger.debug(f"_collect_users_stats falhou: {exc}")

    # online via Redis (real)
    try:
        out["online"] = int(await Presence.online_count())
    except Exception:
        out["online"] = 0

    return out


async def _collect_financial_stats() -> Dict[str, Any]:
    """Estatísticas financeiras reais."""
    out: Dict[str, Any] = {
        "fat_total": 0.0,
        "lucro": 0.0,
        "vendas": 0,
        "saques_pend": 0,
        "saques_pend_valor": 0.0,
        "dep_pend": 0,
        "dep_pend_valor": 0.0,
        "comissoes": 0.0,
        "comissoes_pend": 0.0,
        "despesas": 0.0,
    }

    try:
        async with session_scope() as session:
            # Faturamento (pedidos entregues)
            res = await session.execute(
                select(func.coalesce(func.sum(Order.final_total), 0)).where(
                    Order.status == OrderStatus.DELIVERED
                )
            )
            out["fat_total"] = float(res.scalar() or 0)

            # Vendas
            res = await session.execute(
                select(func.count(Order.id)).where(
                    Order.status == OrderStatus.DELIVERED
                )
            )
            out["vendas"] = int(res.scalar() or 0)

            # Saques pendentes
            res = await session.execute(
                select(
                    func.count(Withdraw.id),
                    func.coalesce(func.sum(Withdraw.amount), 0),
                ).where(
                    Withdraw.status.in_(
                        [WithdrawStatus.PENDING, WithdrawStatus.PROCESSING]
                    )
                )
            )
            row = res.first()
            if row:
                out["saques_pend"] = int(row[0] or 0)
                out["saques_pend_valor"] = float(row[1] or 0)

            # Depósitos pendentes (PIX)
            res = await session.execute(
                select(
                    func.count(PixCharge.id),
                    func.coalesce(func.sum(PixCharge.amount), 0),
                ).where(PixCharge.status == PixStatus.PENDING)
            )
            row = res.first()
            if row:
                out["dep_pend"] = int(row[0] or 0)
                out["dep_pend_valor"] = float(row[1] or 0)

            # Comissões geradas (aprovadas + pagas)
            res = await session.execute(
                select(func.coalesce(func.sum(Commission.amount), 0)).where(
                    Commission.status.in_(
                        [CommissionStatus.APPROVED, CommissionStatus.PAID]
                    )
                )
            )
            out["comissoes"] = float(res.scalar() or 0)

            # Comissões pendentes
            res = await session.execute(
                select(func.coalesce(func.sum(Commission.amount), 0)).where(
                    Commission.status == CommissionStatus.PENDING
                )
            )
            out["comissoes_pend"] = float(res.scalar() or 0)

            # Despesas (saques pagos)
            res = await session.execute(
                select(func.coalesce(func.sum(Withdraw.amount), 0)).where(
                    Withdraw.status == WithdrawStatus.PAID
                )
            )
            out["despesas"] = float(res.scalar() or 0)

        # Lucro líquido = faturamento - despesas
        out["lucro"] = round(out["fat_total"] - out["despesas"], 2)

    except Exception as exc:
        logger.debug(f"_collect_financial_stats falhou: {exc}")

    return out


async def _collect_charts() -> Dict[str, str]:
    """
    Gráficos 7d em mini-sparkline ASCII.
    - Vendas 7d: valores diários de faturamento (order.final_total)
    - Depósitos 7d: valores diários de depósitos (transactions type=DEPOSIT)
    - Usuários 7d: novos usuários por dia
    """
    out = {"grafico_vendas": "—", "grafico_dep": "—", "grafico_users": "—"}

    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=6)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # Prepara buckets por dia
        days: List[datetime] = [start + timedelta(days=i) for i in range(7)]
        day_keys: List[str] = [d.strftime("%Y-%m-%d") for d in days]

        vendas_map: Dict[str, float] = {k: 0.0 for k in day_keys}
        dep_map: Dict[str, float] = {k: 0.0 for k in day_keys}
        users_map: Dict[str, int] = {k: 0 for k in day_keys}

        async with session_scope() as session:
            # Vendas por dia
            res = await session.execute(
                select(Order.created_at, Order.final_total).where(
                    Order.created_at >= start,
                    Order.status == OrderStatus.DELIVERED,
                )
            )
            for created_at, total in res.all():
                try:
                    k = created_at.strftime("%Y-%m-%d")
                    if k in vendas_map:
                        vendas_map[k] += float(total or 0)
                except Exception:
                    continue

            # Depósitos por dia
            res = await session.execute(
                select(Transaction.created_at, Transaction.amount).where(
                    Transaction.created_at >= start,
                    Transaction.type == TransactionType.DEPOSIT,
                )
            )
            for created_at, amount in res.all():
                try:
                    k = created_at.strftime("%Y-%m-%d")
                    if k in dep_map:
                        dep_map[k] += float(amount or 0)
                except Exception:
                    continue

            # Usuários por dia
            res = await session.execute(
                select(User.created_at).where(User.created_at >= start)
            )
            for (created_at,) in res.all():
                try:
                    k = created_at.strftime("%Y-%m-%d")
                    if k in users_map:
                        users_map[k] += 1
                except Exception:
                    continue

        out["grafico_vendas"] = sparkline([vendas_map[k] for k in day_keys])
        out["grafico_dep"] = sparkline([dep_map[k] for k in day_keys])
        out["grafico_users"] = sparkline([float(users_map[k]) for k in day_keys])

    except Exception as exc:
        logger.debug(f"_collect_charts falhou: {exc}")

    return out


async def _collect_recent_activity(limit: int = 5) -> str:
    """
    Lista as atividades recentes (compras, depósitos, saques).
    Retorna string pronta para o template.
    """
    try:
        async with session_scope() as session:
            # Últimos pedidos
            res = await session.execute(
                select(Order.id, Order.user_id, Order.final_total, Order.created_at)
                .where(Order.status == OrderStatus.DELIVERED)
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
            orders = res.all()

        if not orders:
            return "<i>Sem atividades recentes.</i>"

        lines: List[str] = []
        for oid, uid, total, created in orders:
            lines.append(
                f"🛒 Venda #{oid} · {fmt_brl(float(total or 0))} · {fmt_ago(created)}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.debug(f"_collect_recent_activity falhou: {exc}")
        return "<i>Sem atividades.</i>"


async def _collect_system_alerts(limit: int = 5) -> str:
    """
    Lista alertas do sistema não resolvidos.
    """
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Alert)
                .where(Alert.resolved == False)  # noqa: E712
                .order_by(Alert.created_at.desc())
                .limit(limit)
            )
            alerts = list(res.scalars().all())

        if not alerts:
            return "✅ <i>Nenhum alerta ativo.</i>"

        lines: List[str] = []
        for a in alerts:
            sev = str(a.severity or "info").lower()
            emoji = {"critical": "🔴", "error": "🔴", "warning": "🟡"}.get(sev, "🔵")
            lines.append(f"{emoji} {a.title} · {fmt_ago(a.created_at)}")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug(f"_collect_system_alerts falhou: {exc}")
        return "<i>Sem alertas.</i>"


# ============================================================
# 🧾 RENDER
# ============================================================
async def _render_dashboard(callback: Optional[CallbackQuery] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Coleta tudo e renderiza o texto + teclado.
    """
    # Coleta paralela
    users = await _collect_users_stats()
    fin = await _collect_financial_stats()
    charts = await _collect_charts()
    atividades = await _collect_recent_activity()
    alertas = await _collect_system_alerts()

    data_hora = fmt_dt(datetime.now(timezone.utc))

    # ---------- Texto via message_engine ----------
    rendered = await message_engine.render(
        MSG_KEY_DASHBOARD,
        fallback=(
            f"📊 <b>DASHBOARD</b> · {data_hora}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>USUÁRIOS</b>\n"
            f"├ Total: <b>{fmt_num(users['total_users'])}</b>\n"
            f"├ 🟢 Online: <b>{fmt_num(users.get('online', 0))}</b>\n"
            f"├ 🆕 Novos hoje: <b>{fmt_num(users['novos_hoje'])}</b>\n"
            f"├ 🚫 Bloqueados: <b>{fmt_num(users['bloqueados'])}</b>\n"
            f"└ 💰 Saldo total: <b>{fmt_brl(users['saldo_total'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💵 <b>FINANCEIRO</b>\n"
            f"├ Faturamento total: <b>{fmt_brl(fin['fat_total'])}</b>\n"
            f"├ Lucro líquido: <b>{fmt_brl(fin['lucro'])}</b>\n"
            f"├ Vendas realizadas: <b>{fmt_num(fin['vendas'])}</b>\n"
            f"├ Saques pendentes: <b>{fmt_num(fin['saques_pend'])}</b> "
            f"({fmt_brl(fin['saques_pend_valor'])})\n"
            f"├ Depósitos pendentes: <b>{fmt_num(fin['dep_pend'])}</b> "
            f"({fmt_brl(fin['dep_pend_valor'])})\n"
            f"└ Comissões geradas: <b>{fmt_brl(fin['comissoes'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📈 <b>GRÁFICOS (7d)</b>\n"
            f"├ Vendas: <code>{charts['grafico_vendas']}</code>\n"
            f"├ Depósitos: <code>{charts['grafico_dep']}</code>\n"
            f"└ Usuários: <code>{charts['grafico_users']}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🕐 <b>ATIVIDADES RECENTES</b>\n"
            f"{atividades}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>ALERTAS DO SISTEMA</b>\n"
            f"{alertas}"
        ),
        data_hora=data_hora,
        total_users=fmt_num(users["total_users"]),
        online=fmt_num(users.get("online", 0)),
        novos_hoje=fmt_num(users["novos_hoje"]),
        bloqueados=fmt_num(users["bloqueados"]),
        banidos=fmt_num(users["banidos"]),
        ativos=fmt_num(users["ativos"]),
        saldo_total=fmt_brl(users["saldo_total"]),
        fat_total=fmt_brl(fin["fat_total"]),
        lucro=fmt_brl(fin["lucro"]),
        vendas=fmt_num(fin["vendas"]),
        saques_pend=fmt_num(fin["saques_pend"]),
        dep_pend=fmt_num(fin["dep_pend"]),
        comissoes=fmt_brl(fin["comissoes"]),
        comissoes_pend=fmt_brl(fin["comissoes_pend"]),
        grafico_vendas=charts["grafico_vendas"],
        grafico_dep=charts["grafico_dep"],
        grafico_users=charts["grafico_users"],
        lista_atividades=atividades,
        lista_alertas=alertas,
    )

    # ---------- Teclado ----------
    keyboard = await button_engine.inline(SCREEN_DASHBOARD)
    if keyboard is None:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="🔄 Atualizar", callback_data=CB_DASH_REFRESH),
                    InlineKeyboardButton(text="📈 Gráficos", callback_data=CB_DASH_CHARTS),
                ],
                [
                    InlineKeyboardButton(text="📊 Relatórios", callback_data=CB_DASH_REPORTS),
                    InlineKeyboardButton(text="🕐 Atividades", callback_data=CB_DASH_ACTIVITY),
                ],
                [
                    InlineKeyboardButton(text="⚠️ Alertas", callback_data=CB_DASH_ALERTS),
                    InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu"),
                ],
            ]
        )

    return rendered.content, keyboard


# ============================================================
# HANDLERS
# ============================================================
@router.callback_query(F.data.in_({CB_DASHBOARD, CB_DASH_REFRESH}))
async def on_dashboard(callback: CallbackQuery) -> None:
    """
    Abre/atualiza o Dashboard.
    Sempre EDITA a mensagem atual (não acumula).
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        text, keyboard = await _render_dashboard(callback)

        await edit_or_send(callback, text=text, reply_markup=keyboard)

        if callback.data == CB_DASH_REFRESH:
            await toast_ok(callback, "🔄 Atualizado!")
        else:
            await answer(callback)

        logger.debug(f"Dashboard renderizado para admin {admin_id}")
    except Exception as exc:
        logger.exception(f"on_dashboard falhou: {exc}")
        try:
            await answer(callback, "❌ Erro ao carregar dashboard.", alert=True)
        except Exception:
            pass


@router.callback_query(F.data == CB_DASH_CHARTS)
async def on_dashboard_charts(callback: CallbackQuery) -> None:
    """
    Abre a tela de gráficos (detalhamento dos últimos 7 dias).
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        charts = await _collect_charts()

        text = (
            "📈 <b>GRÁFICOS — ÚLTIMOS 7 DIAS</b>\n\n"
            "Os gráficos abaixo são mini-sparklines em ASCII,\n"
            "gerados a partir dos dados reais do banco.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Vendas</b>: <code>{charts['grafico_vendas']}</code>\n"
            "   ▁ = mínimo · █ = máximo\n\n"
            f"💰 <b>Depósitos</b>: <code>{charts['grafico_dep']}</code>\n\n"
            f"👥 <b>Novos usuários</b>: <code>{charts['grafico_users']}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Use o botão abaixo para voltar ao Dashboard."
        )

        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_DASHBOARD,
                    )
                ]
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_dashboard_charts falhou: {exc}")


@router.callback_query(F.data == CB_DASH_REPORTS)
async def on_dashboard_reports(callback: CallbackQuery) -> None:
    """
    Abre a tela de Relatórios (resumo em números).
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        users = await _collect_users_stats()
        fin = await _collect_financial_stats()

        ticket_medio = 0.0
        if fin["vendas"] > 0:
            ticket_medio = round(fin["fat_total"] / fin["vendas"], 2)

        text = (
            "📊 <b>RELATÓRIOS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>USUÁRIOS</b>\n"
            f"├ Total: <b>{fmt_num(users['total_users'])}</b>\n"
            f"├ Ativos: <b>{fmt_num(users['ativos'])}</b>\n"
            f"├ Bloqueados: <b>{fmt_num(users['bloqueados'])}</b>\n"
            f"└ Banidos: <b>{fmt_num(users['banidos'])}</b>\n\n"
            "💵 <b>FINANCEIRO</b>\n"
            f"├ Faturamento: <b>{fmt_brl(fin['fat_total'])}</b>\n"
            f"├ Vendas: <b>{fmt_num(fin['vendas'])}</b>\n"
            f"├ Ticket médio: <b>{fmt_brl(ticket_medio)}</b>\n"
            f"├ Comissões pagas: <b>{fmt_brl(fin['comissoes'])}</b>\n"
            f"├ Comissões pendentes: <b>{fmt_brl(fin['comissoes_pend'])}</b>\n"
            f"├ Despesas (saques pagos): <b>{fmt_brl(fin['despesas'])}</b>\n"
            f"└ Lucro líquido: <b>{fmt_brl(fin['lucro'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_DASHBOARD,
                    )
                ]
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_dashboard_reports falhou: {exc}")


@router.callback_query(F.data == CB_DASH_ACTIVITY)
async def on_dashboard_activity(callback: CallbackQuery) -> None:
    """
    Abre a tela de Atividades Recentes (lista completa).
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Carrega últimas 15 atividades
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Log)
                    .order_by(Log.created_at.desc())
                    .limit(15)
                )
                logs = list(res.scalars().all())
        except Exception:
            logs = []

        if not logs:
            text = (
                "🕐 <b>ATIVIDADES RECENTES</b>\n\n"
                "<i>Nenhuma atividade registrada ainda.</i>"
            )
        else:
            lines = ["🕐 <b>ATIVIDADES RECENTES (últimas 15)</b>\n"]
            for log in logs:
                emoji = {
                    "admin": "🛡️",
                    "payments": "💳",
                    "security": "🔐",
                    "audit": "📝",
                }.get(str(log.category or ""), "🔹")
                lines.append(
                    f"{emoji} <code>{log.action}</code> · {fmt_ago(log.created_at)}"
                )
            text = "\n".join(lines)

        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_DASHBOARD,
                    )
                ]
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_dashboard_activity falhou: {exc}")


@router.callback_query(F.data == CB_DASH_ALERTS)
async def on_dashboard_alerts(callback: CallbackQuery) -> None:
    """
    Abre a tela de Alertas do Sistema.
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(Alert)
                    .where(Alert.resolved == False)  # noqa: E712
                    .order_by(Alert.created_at.desc())
                    .limit(20)
                )
                alerts = list(res.scalars().all())
        except Exception:
            alerts = []

        if not alerts:
            text = (
                "⚠️ <b>ALERTAS DO SISTEMA</b>\n\n"
                "✅ <i>Nenhum alerta ativo no momento.</i>"
            )
        else:
            lines = [f"⚠️ <b>ALERTAS DO SISTEMA</b> ({len(alerts)})\n"]
            for a in alerts:
                sev = str(a.severity or "info").lower()
                emoji = {"critical": "🔴", "error": "🔴", "warning": "🟡"}.get(sev, "🔵")
                lines.append(
                    f"{emoji} <b>{a.title}</b>\n"
                    f"    {fmt_ago(a.created_at)} · {str(a.category or '')}"
                )
            text = "\n".join(lines)

        keyboard = kb(
            [
                [
                    InlineKeyboardButton(
                        text="🔙 Voltar",
                        callback_data=CB_DASHBOARD,
                    )
                ]
            ]
        )

        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"on_dashboard_alerts falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_dashboard",
    "on_dashboard_charts",
    "on_dashboard_reports",
    "on_dashboard_activity",
    "on_dashboard_alerts",
    "CB_DASHBOARD",
    "CB_DASH_REFRESH",
    "CB_DASH_CHARTS",
    "CB_DASH_REPORTS",
    "CB_DASH_ACTIVITY",
    "CB_DASH_ALERTS",
    "MSG_KEY_DASHBOARD",
    "SCREEN_DASHBOARD",
    "sparkline",
]
