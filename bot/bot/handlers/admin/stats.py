"""
Larizinha Store — Módulo Admin: Estatísticas
Implementa relatórios detalhados conforme DIM.

Telas:
- Painel geral de estatísticas (resumo)
- Faturamento por período (diário/semanal/mensal/personalizado)
- Top produtos (mais vendidos)
- Top usuários (maior gasto / saldo)
- Gráficos em mini-sparkline (faturamento, usuários, vendas)
- Exportação CSV (vendas, usuários, produtos)

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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
    Order,
    OrderItem,
    OrderStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    AdminFlow,
    answer,
    ask_input,
    build_back_row,
    consume_input,
    edit_or_send,
    fmt_brl,
    fmt_dt,
    fmt_num,
    is_date,
    kb,
    log_action,
    parse_date,
    send_csv,
    table,
    toast_err,
    toast_ok,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_stats")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_OVERVIEW = "admin_stats_overview"
MSG_KEY_PERIOD = "admin_stats_period"
MSG_KEY_TOP_PRODUCTS = "admin_stats_top_products"
MSG_KEY_TOP_USERS = "admin_stats_top_users"

SCREEN_OVERVIEW = "admin_stats_overview"
SCREEN_PERIOD = "admin_stats_period"

# Callbacks base
CB_ROOT = "admin:stats"
CB_OVERVIEW = "admin:stats:overview"
CB_PERIOD = "admin:stats:period"          # :daily|weekly|monthly|custom
CB_PERIOD_CUSTOM = "admin:stats:custom"
CB_TOP_PROD = "admin:stats:topprod"       # :7|30|90
CB_TOP_USERS = "admin:stats:topusers"     # :spend|balance
CB_CHARTS = "admin:stats:charts"
CB_EXPORT = "admin:stats:export"          # :sales|users|products

# Sparkline blocks
_SPARK = "▁▂▃▄▅▆▇█"


# ============================================================
# 🧰 HELPERS
# ============================================================
def _sparkline(values: Sequence[float]) -> str:
    if not values:
        return "—"
    try:
        vmax = max(values)
        vmin = min(values)
    except Exception:
        return "—"
    if vmax == vmin:
        return _SPARK[0] * len(values)
    span = vmax - vmin
    out = []
    for v in values:
        idx = int(round(((v - vmin) / span) * (len(_SPARK) - 1)))
        idx = max(0, min(len(_SPARK) - 1, idx))
        out.append(_SPARK[idx])
    return "".join(out)


def _period_range(period: str) -> Tuple[datetime, datetime, str]:
    """Retorna (start, end, label)."""
    now = datetime.now(timezone.utc)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now, "Hoje"
    if period == "weekly":
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now, "Últimos 7 dias"
    if period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now, "Este mês"
    if period == "7":
        start = now - timedelta(days=7)
        return start, now, "7 dias"
    if period == "30":
        start = now - timedelta(days=30)
        return start, now, "30 dias"
    if period == "90":
        start = now - timedelta(days=90)
        return start, now, "90 dias"
    # Fallback
    start = now - timedelta(days=7)
    return start, now, "7 dias"


# ============================================================
# COLETA
# ============================================================
async def _collect_overview() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "users_total": 0,
        "users_active": 0,
        "orders_count": 0,
        "orders_value": 0.0,
        "ticket_avg": 0.0,
        "products_count": 0,
        "withdraw_value": 0.0,
        "deposit_value": 0.0,
        "profit": 0.0,
    }
    try:
        async with session_scope() as session:
            out["users_total"] = int(
                (await session.execute(select(func.count(User.id)))).scalar() or 0
            )
            out["users_active"] = int(
                (
                    await session.execute(
                        select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)
                    )
                ).scalar()
                or 0
            )

            # Orders
            res = await session.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.final_total), 0),
                ).where(Order.status == OrderStatus.DELIVERED)
            )
            row = res.first()
            out["orders_count"] = int(row[0] or 0)
            out["orders_value"] = round(float(row[1] or 0), 2)
            if out["orders_count"] > 0:
                out["ticket_avg"] = round(out["orders_value"] / out["orders_count"], 2)

            # Depósitos
            out["deposit_value"] = round(
                float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                                Transaction.type == TransactionType.DEPOSIT
                            )
                        )
                    ).scalar()
                    or 0
                ),
                2,
            )

            # Saques pagos
            out["withdraw_value"] = round(
                float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                                Transaction.type == TransactionType.WITHDRAW
                            )
                        )
                    ).scalar()
                    or 0
                ),
                2,
            )

            out["profit"] = round(out["deposit_value"] - out["withdraw_value"], 2)
    except Exception as exc:
        logger.debug(f"_collect_overview falhou: {exc}")

    return out


async def _collect_period(
    period: str,
) -> Dict[str, Any]:
    start, end, label = _period_range(period)

    out: Dict[str, Any] = {
        "label": label,
        "start": start,
        "end": end,
        "orders_count": 0,
        "orders_value": 0.0,
        "deposits_value": 0.0,
        "deposits_count": 0,
        "withdraws_value": 0.0,
        "new_users": 0,
        "ticket_avg": 0.0,
        "daily_values": [],
        "daily_days": [],
    }

    try:
        async with session_scope() as session:
            # Orders no período
            res = await session.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.final_total), 0),
                )
                .where(Order.status == OrderStatus.DELIVERED)
                .where(Order.created_at >= start)
                .where(Order.created_at <= end)
            )
            row = res.first()
            out["orders_count"] = int(row[0] or 0)
            out["orders_value"] = round(float(row[1] or 0), 2)
            if out["orders_count"] > 0:
                out["ticket_avg"] = round(out["orders_value"] / out["orders_count"], 2)

            # Depósitos no período
            res = await session.execute(
                select(
                    func.count(Transaction.id),
                    func.coalesce(func.sum(Transaction.amount), 0),
                )
                .where(Transaction.type == TransactionType.DEPOSIT)
                .where(Transaction.created_at >= start)
                .where(Transaction.created_at <= end)
            )
            row = res.first()
            out["deposits_count"] = int(row[0] or 0)
            out["deposits_value"] = round(float(row[1] or 0), 2)

            # Saques no período
            out["withdraws_value"] = round(
                float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(Transaction.amount), 0))
                            .where(Transaction.type == TransactionType.WITHDRAW)
                            .where(Transaction.created_at >= start)
                            .where(Transaction.created_at <= end)
                        )
                    ).scalar()
                    or 0
                ),
                2,
            )

            # Novos usuários
            out["new_users"] = int(
                (
                    await session.execute(
                        select(func.count(User.id))
                        .where(User.created_at >= start)
                        .where(User.created_at <= end)
                    )
                ).scalar()
                or 0
            )

            # Série diária (para sparkline) — últimos N dias do range
            days_span = max(1, (end.date() - start.date()).days + 1)
            days_span = min(days_span, 14)
            for i in range(days_span):
                d_start = (end - timedelta(days=days_span - 1 - i)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                d_end = d_start + timedelta(days=1)
                v = (
                    await session.execute(
                        select(func.coalesce(func.sum(Order.final_total), 0))
                        .where(Order.status == OrderStatus.DELIVERED)
                        .where(Order.created_at >= d_start)
                        .where(Order.created_at < d_end)
                    )
                ).scalar()
                out["daily_values"].append(float(v or 0))
                out["daily_days"].append(d_start.strftime("%d/%m"))
    except Exception as exc:
        logger.debug(f"_collect_period falhou: {exc}")

    return out


async def _collect_top_products(days: int = 30, limit: int = 10) -> List[Tuple[str, int, float]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(
                    OrderItem.product_name,
                    func.sum(OrderItem.quantity),
                    func.sum(OrderItem.total),
                )
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.status == OrderStatus.DELIVERED)
                .where(Order.created_at >= since)
                .group_by(OrderItem.product_name)
                .order_by(func.sum(OrderItem.quantity).desc())
                .limit(limit)
            )
            return [
                (str(name or "—"), int(qty or 0), round(float(total or 0), 2))
                for name, qty, total in res.all()
            ]
    except Exception as exc:
        logger.debug(f"_collect_top_products falhou: {exc}")
        return []


async def _collect_top_users(
    mode: str = "spend", limit: int = 10
) -> List[Tuple[str, int, float]]:
    try:
        async with session_scope() as session:
            if mode == "balance":
                res = await session.execute(
                    select(User.first_name, User.telegram_id, User.balance)
                    .order_by(User.balance.desc())
                    .limit(limit)
                )
                return [
                    (str(nome or f"User {tg}"), int(tg), round(float(bal or 0), 2))
                    for nome, tg, bal in res.all()
                ]

            # spend (padrão) = total_spent
            res = await session.execute(
                select(User.first_name, User.telegram_id, User.total_spent)
                .order_by(User.total_spent.desc())
                .limit(limit)
            )
            return [
                (str(nome or f"User {tg}"), int(tg), round(float(sp or 0), 2))
                for nome, tg, sp in res.all()
            ]
    except Exception as exc:
        logger.debug(f"_collect_top_users falhou: {exc}")
        return []


# ============================================================
# PAINEL
# ============================================================
async def render_overview(
    target: Union[CallbackQuery, Message],
) -> Optional[Message]:
    stats = await _collect_overview()

    rendered = await message_engine.render(
        MSG_KEY_OVERVIEW,
        fallback=(
            "📈 <b>ESTATÍSTICAS · VISÃO GERAL</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👥 <b>USUÁRIOS</b>\n"
            f"├ Total: <b>{fmt_num(stats['users_total'])}</b>\n"
            f"└ Ativos: <b>{fmt_num(stats['users_active'])}</b>\n\n"
            "🛒 <b>VENDAS</b>\n"
            f"├ Pedidos entregues: <b>{fmt_num(stats['orders_count'])}</b>\n"
            f"├ Faturamento: <b>{fmt_brl(stats['orders_value'])}</b>\n"
            f"└ Ticket médio: <b>{fmt_brl(stats['ticket_avg'])}</b>\n\n"
            "💰 <b>FINANCEIRO</b>\n"
            f"├ Depósitos totais: <b>{fmt_brl(stats['deposit_value'])}</b>\n"
            f"├ Saques pagos: <b>{fmt_brl(stats['withdraw_value'])}</b>\n"
            f"└ Lucro líquido: <b>{fmt_brl(stats['profit'])}</b>"
        ),
        users_total=fmt_num(stats["users_total"]),
        users_active=fmt_num(stats["users_active"]),
        orders_count=fmt_num(stats["orders_count"]),
        orders_value=fmt_brl(stats["orders_value"]),
        ticket_avg=fmt_brl(stats["ticket_avg"]),
        deposit_value=fmt_brl(stats["deposit_value"]),
        withdraw_value=fmt_brl(stats["withdraw_value"]),
        profit=fmt_brl(stats["profit"]),
    )

    engine_kb = await button_engine.inline(SCREEN_OVERVIEW)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(
            [
                [
                    InlineKeyboardButton(text="📅 Diário", callback_data=f"{CB_PERIOD}:daily"),
                    InlineKeyboardButton(text="📅 Semanal", callback_data=f"{CB_PERIOD}:weekly"),
                ],
                [
                    InlineKeyboardButton(text="📅 Mensal", callback_data=f"{CB_PERIOD}:monthly"),
                    InlineKeyboardButton(text="📅 Personalizado", callback_data=CB_PERIOD_CUSTOM),
                ],
                [
                    InlineKeyboardButton(text="🏆 Top Produtos", callback_data=f"{CB_TOP_PROD}:30"),
                    InlineKeyboardButton(text="👥 Top Usuários", callback_data=f"{CB_TOP_USERS}:spend"),
                ],
                [
                    InlineKeyboardButton(text="📊 Gráficos", callback_data=CB_CHARTS),
                    InlineKeyboardButton(text="📥 Exportar", callback_data=f"{CB_EXPORT}:sales"),
                ],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="admin:menu")],
            ]
        )

    return await edit_or_send(target, text=rendered.content, reply_markup=keyboard)


@router.callback_query(F.data.in_({CB_ROOT, CB_OVERVIEW}))
async def on_overview(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_overview(callback)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_overview falhou: {exc}")


# ============================================================
# POR PERÍODO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_PERIOD}:"))
async def on_period(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            period = callback.data.split(":")[-1]
        except Exception:
            period = "weekly"
        if period not in ("daily", "weekly", "monthly", "7", "30", "90"):
            period = "weekly"

        data = await _collect_period(period)

        spark = _sparkline(data["daily_values"])

        rendered = await message_engine.render(
            MSG_KEY_PERIOD,
            fallback=(
                f"📅 <b>ESTATÍSTICAS · {data['label']}</b>\n"
                f"🕐 De {fmt_dt(data['start'], with_time=False)} "
                f"até {fmt_dt(data['end'], with_time=False)}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛒 Pedidos: <b>{fmt_num(data['orders_count'])}</b>\n"
                f"💵 Faturamento: <b>{fmt_brl(data['orders_value'])}</b>\n"
                f"📊 Ticket médio: <b>{fmt_brl(data['ticket_avg'])}</b>\n\n"
                f"💰 Depósitos: <b>{fmt_brl(data['deposits_value'])}</b> "
                f"({fmt_num(data['deposits_count'])} ops)\n"
                f"💸 Saques: <b>{fmt_brl(data['withdraws_value'])}</b>\n"
                f"👥 Novos usuários: <b>{fmt_num(data['new_users'])}</b>\n\n"
                "📈 <b>Série diária (faturamento)</b>\n"
                f"<code>{spark}</code>\n"
                f"<i>{'  '.join(data['daily_days'][:14])}</i>"
            ),
            label=data["label"],
            start=fmt_dt(data["start"], with_time=False),
            end=fmt_dt(data["end"], with_time=False),
            orders_count=fmt_num(data["orders_count"]),
            orders_value=fmt_brl(data["orders_value"]),
            ticket_avg=fmt_brl(data["ticket_avg"]),
            deposits_value=fmt_brl(data["deposits_value"]),
            deposits_count=fmt_num(data["deposits_count"]),
            withdraws_value=fmt_brl(data["withdraws_value"]),
            new_users=fmt_num(data["new_users"]),
            sparkline=spark,
        )

        rows_kb = [
            [
                InlineKeyboardButton(text="📅 Diário", callback_data=f"{CB_PERIOD}:daily"),
                InlineKeyboardButton(text="📅 7d", callback_data=f"{CB_PERIOD}:7"),
                InlineKeyboardButton(text="📅 30d", callback_data=f"{CB_PERIOD}:30"),
            ],
            [
                InlineKeyboardButton(text="📅 90d", callback_data=f"{CB_PERIOD}:90"),
                InlineKeyboardButton(text="📅 Mensal", callback_data=f"{CB_PERIOD}:monthly"),
            ],
            [
                InlineKeyboardButton(
                    text="📥 Exportar CSV",
                    callback_data=f"{CB_EXPORT}:sales",
                ),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)],
        ]

        await edit_or_send(callback, text=rendered.content, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_period falhou: {exc}")


# ============================================================
# PERÍODO PERSONALIZADO
# ============================================================
@router.callback_query(F.data == CB_PERIOD_CUSTOM)
async def on_period_custom(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "📅 <b>PERÍODO PERSONALIZADO</b>\n\n"
            "Envie a data inicial no formato <code>DD/MM/AAAA</code>.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="stats_custom_start",
            screen="stats:custom",
            use_force_reply=True,
            reply_placeholder="DD/MM/AAAA",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_period_custom falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_stats_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    if awaiting == "stats_custom_start":
        value, _extra = await consume_input(message, admin_id, expected_field="stats_custom_start")
        if value is None:
            return
        dt = parse_date(value)
        if dt is None:
            await message.answer("❌ Data inválida. Use DD/MM/AAAA.")
            return
        await AdminFlow.update(admin_id, {"custom_start": dt.isoformat()})

        prompt = (
            "📅 <b>PERÍODO PERSONALIZADO</b>\n\n"
            "Agora envie a data final no formato <code>DD/MM/AAAA</code>.\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field="stats_custom_end",
            screen="stats:custom",
            use_force_reply=True,
            reply_placeholder="DD/MM/AAAA",
        )
        return

    if awaiting == "stats_custom_end":
        value, _extra = await consume_input(message, admin_id, expected_field="stats_custom_end")
        if value is None:
            return
        dt_end = parse_date(value)
        if dt_end is None:
            await message.answer("❌ Data inválida. Use DD/MM/AAAA.")
            return

        state2 = await AdminFlow.get(admin_id)
        data2 = (state2 or {}).get("data", {}) if state2 else {}
        custom_start_str = data2.get("custom_start")
        if not custom_start_str:
            await message.answer("❌ Fluxo incompleto. Recomece.")
            await AdminFlow.clear(admin_id)
            return

        try:
            dt_start = datetime.fromisoformat(custom_start_str)
        except Exception:
            await message.answer("❌ Data inválida.")
            await AdminFlow.clear(admin_id)
            return

        # Ajusta fim para 23:59
        dt_end = dt_end.replace(hour=23, minute=59, second=59)

        await AdminFlow.clear(admin_id)

        # Coleta dados nesse range
        out: Dict[str, Any] = {
            "orders_count": 0,
            "orders_value": 0.0,
            "deposits_value": 0.0,
            "withdraws_value": 0.0,
            "new_users": 0,
            "ticket_avg": 0.0,
        }
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(
                        func.count(Order.id),
                        func.coalesce(func.sum(Order.final_total), 0),
                    )
                    .where(Order.status == OrderStatus.DELIVERED)
                    .where(Order.created_at >= dt_start)
                    .where(Order.created_at <= dt_end)
                )
                row = res.first()
                out["orders_count"] = int(row[0] or 0)
                out["orders_value"] = round(float(row[1] or 0), 2)
                if out["orders_count"]:
                    out["ticket_avg"] = round(
                        out["orders_value"] / out["orders_count"], 2
                    )

                out["deposits_value"] = round(
                    float(
                        (
                            await session.execute(
                                select(func.coalesce(func.sum(Transaction.amount), 0))
                                .where(Transaction.type == TransactionType.DEPOSIT)
                                .where(Transaction.created_at >= dt_start)
                                .where(Transaction.created_at <= dt_end)
                            )
                        ).scalar()
                        or 0
                    ),
                    2,
                )

                out["withdraws_value"] = round(
                    float(
                        (
                            await session.execute(
                                select(func.coalesce(func.sum(Transaction.amount), 0))
                                .where(Transaction.type == TransactionType.WITHDRAW)
                                .where(Transaction.created_at >= dt_start)
                                .where(Transaction.created_at <= dt_end)
                            )
                        ).scalar()
                        or 0
                    ),
                    2,
                )

                out["new_users"] = int(
                    (
                        await session.execute(
                            select(func.count(User.id))
                            .where(User.created_at >= dt_start)
                            .where(User.created_at <= dt_end)
                        )
                    ).scalar()
                    or 0
                )
        except Exception as exc:
            logger.debug(f"custom range falhou: {exc}")

        text = (
            f"📅 <b>PERÍODO PERSONALIZADO</b>\n"
            f"🕐 {fmt_dt(dt_start, with_time=False)} → "
            f"{fmt_dt(dt_end, with_time=False)}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Pedidos: <b>{fmt_num(out['orders_count'])}</b>\n"
            f"💵 Faturamento: <b>{fmt_brl(out['orders_value'])}</b>\n"
            f"📊 Ticket médio: <b>{fmt_brl(out['ticket_avg'])}</b>\n"
            f"💰 Depósitos: <b>{fmt_brl(out['deposits_value'])}</b>\n"
            f"💸 Saques: <b>{fmt_brl(out['withdraws_value'])}</b>\n"
            f"👥 Novos usuários: <b>{fmt_num(out['new_users'])}</b>"
        )

        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]]
            ),
        )
        return


# ============================================================
# TOP PRODUTOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOP_PROD}:"))
async def on_top_products(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            days = int(callback.data.split(":")[-1])
        except Exception:
            days = 30
        if days not in (7, 30, 90):
            days = 30

        items = await _collect_top_products(days=days, limit=10)

        if not items:
            text = f"🏆 <i>Sem vendas nos últimos {days} dias.</i>"
        else:
            rows: List[Sequence[str]] = []
            medals = ["🥇", "🥈", "🥉"]
            for i, (name, qty, total) in enumerate(items, start=1):
                m = medals[i - 1] if i <= 3 else ""
                rows.append([f"{i}° {m}", truncate(name, 22), str(qty), fmt_brl(total)])

            lista = table(rows, ["#", "Produto", "Qtd", "Faturamento"])
            text = (
                f"🏆 <b>TOP PRODUTOS · {days} DIAS</b>\n\n"
                f"<code>{lista}</code>"
            )

        rows_kb = [
            [
                InlineKeyboardButton(text="7 dias", callback_data=f"{CB_TOP_PROD}:7"),
                InlineKeyboardButton(text="30 dias", callback_data=f"{CB_TOP_PROD}:30"),
                InlineKeyboardButton(text="90 dias", callback_data=f"{CB_TOP_PROD}:90"),
            ],
            [
                InlineKeyboardButton(
                    text="📥 Exportar CSV",
                    callback_data=f"{CB_EXPORT}:products",
                ),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_top_products falhou: {exc}")


# ============================================================
# TOP USUÁRIOS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOP_USERS}:"))
async def on_top_users(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            mode = callback.data.split(":")[-1]
        except Exception:
            mode = "spend"
        if mode not in ("spend", "balance"):
            mode = "spend"

        users = await _collect_top_users(mode=mode, limit=10)

        title = "💸 MAIORES GASTOS" if mode == "spend" else "💰 MAIORES SALDOS"

        if not users:
            text = f"👥 <i>Sem dados.</i>"
        else:
            rows: List[Sequence[str]] = []
            medals = ["🥇", "🥈", "🥉"]
            for i, (name, tg, val) in enumerate(users, start=1):
                m = medals[i - 1] if i <= 3 else ""
                rows.append([f"{i}° {m}", truncate(name, 20), str(tg), fmt_brl(val)])

            lista = table(rows, ["#", "Nome", "Telegram", "Valor"])
            text = f"🏆 <b>{title}</b>\n\n<code>{lista}</code>"

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="💸 Top Gastos",
                    callback_data=f"{CB_TOP_USERS}:spend",
                ),
                InlineKeyboardButton(
                    text="💰 Top Saldos",
                    callback_data=f"{CB_TOP_USERS}:balance",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📥 Exportar CSV",
                    callback_data=f"{CB_EXPORT}:users",
                ),
            ],
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_top_users falhou: {exc}")


# ============================================================
# GRÁFICOS
# ============================================================
@router.callback_query(F.data == CB_CHARTS)
async def on_charts(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        # Coleta 14 dias de faturamento, depósitos e novos usuários
        now = datetime.now(timezone.utc)
        values_orders: List[float] = []
        values_deposits: List[float] = []
        values_users: List[float] = []

        try:
            async with session_scope() as session:
                for i in range(14):
                    d_start = (now - timedelta(days=13 - i)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    d_end = d_start + timedelta(days=1)

                    v = (
                        await session.execute(
                            select(func.coalesce(func.sum(Order.final_total), 0))
                            .where(Order.status == OrderStatus.DELIVERED)
                            .where(Order.created_at >= d_start)
                            .where(Order.created_at < d_end)
                        )
                    ).scalar()
                    values_orders.append(float(v or 0))

                    v = (
                        await session.execute(
                            select(func.coalesce(func.sum(Transaction.amount), 0))
                            .where(Transaction.type == TransactionType.DEPOSIT)
                            .where(Transaction.created_at >= d_start)
                            .where(Transaction.created_at < d_end)
                        )
                    ).scalar()
                    values_deposits.append(float(v or 0))

                    v = (
                        await session.execute(
                            select(func.count(User.id))
                            .where(User.created_at >= d_start)
                            .where(User.created_at < d_end)
                        )
                    ).scalar()
                    values_users.append(float(v or 0))
        except Exception as exc:
            logger.debug(f"charts falhou: {exc}")

        text = (
            "📊 <b>GRÁFICOS · ÚLTIMOS 14 DIAS</b>\n\n"
            "Mini-sparklines em ASCII (▁ = mínimo, █ = máximo)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💵 <b>Faturamento</b>\n"
            f"<code>{_sparkline(values_orders)}</code>\n\n"
            "💰 <b>Depósitos</b>\n"
            f"<code>{_sparkline(values_deposits)}</code>\n\n"
            "👥 <b>Novos usuários</b>\n"
            f"<code>{_sparkline(values_users)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_OVERVIEW)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"stats on_charts falhou: {exc}")


# ============================================================
# EXPORTAR CSV
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_EXPORT}:"))
async def on_export(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            kind = callback.data.split(":")[-1]
        except Exception:
            kind = "sales"

        headers: List[str] = []
        rows: List[List[Any]] = []
        filename = f"stats_{kind}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

        try:
            async with session_scope() as session:
                if kind == "sales":
                    headers = ["ID", "User", "Valor", "Método", "Status", "Data"]
                    res = await session.execute(
                        select(Order)
                        .order_by(Order.created_at.desc())
                        .limit(1000)
                    )
                    for o in res.scalars().all():
                        rows.append(
                            [
                                o.id,
                                o.user_id,
                                float(o.final_total or 0),
                                str(getattr(o.method, "value", o.method)),
                                str(getattr(o.status, "value", o.status)),
                                fmt_dt(o.created_at),
                            ]
                        )

                elif kind == "users":
                    headers = [
                        "ID", "Telegram", "Nome", "Saldo",
                        "Total gasto", "Compras", "Status", "Criado",
                    ]
                    res = await session.execute(
                        select(User).order_by(User.id.asc()).limit(5000)
                    )
                    for u in res.scalars().all():
                        rows.append(
                            [
                                u.id,
                                u.telegram_id,
                                u.first_name or "",
                                float(u.balance or 0),
                                float(u.total_spent or 0),
                                int(u.purchases_count or 0),
                                str(getattr(u.status, "value", u.status)),
                                fmt_dt(u.created_at),
                            ]
                        )

                elif kind == "products":
                    headers = ["Produto", "Qtd", "Faturamento"]
                    items = await _collect_top_products(days=90, limit=500)
                    for name, qty, total in items:
                        rows.append([name, qty, total])

                else:
                    headers = ["Vazio"]
                    rows = []
        except Exception as exc:
            logger.debug(f"export {kind} falhou: {exc}")

        if not rows:
            await toast_err(callback, "❌ Sem dados para exportar.")
            return

        ok = await send_csv(
            callback.bot,
            callback.message.chat.id if callback.message else admin_id,
            headers,
            rows,
            filename=filename,
            caption=f"📥 <b>Estatísticas · {kind}</b> ({len(rows)})",
        )

        if ok:
            await toast_ok(callback, "📥 CSV enviado!")
        else:
            await toast_err(callback, "❌ Erro ao gerar CSV.")

        await log_action(
            admin_id=admin_id,
            action=f"stats.export.{kind}",
            category="admin",
            details={"count": len(rows)},
        )
    except Exception as exc:
        logger.exception(f"stats on_export falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_overview",
    "on_overview",
    "on_period",
    "on_period_custom",
    "on_stats_text_input",
    "on_top_products",
    "on_top_users",
    "on_charts",
    "on_export",
    "CB_ROOT",
    "CB_OVERVIEW",
    "CB_PERIOD",
    "CB_PERIOD_CUSTOM",
    "CB_TOP_PROD",
    "CB_TOP_USERS",
    "CB_CHARTS",
    "CB_EXPORT",
]
