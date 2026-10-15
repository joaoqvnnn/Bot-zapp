"""
Larizinha Store — Handler de Jogos
Implementa o sistema de jogos interativos com:
- Vitrine de jogos ativos (nome, descrição, custo, prêmios possíveis)
- Confirmação antes de jogar (mostra custo e saldo)
- Execução real do jogo:
    • Debita saldo (atômico)
    • Sorteia resultado pelas probabilidades configuradas em `Game.rewards`
    • Credita prêmio no saldo se houver
    • Registra em `GameSession`
    • Atualiza contadores
- Histórico do usuário (paginação)
- Ranking de ganhadores (top 10 do mês)
- Antiabuso: limite por usuário (Game.limits.per_user_daily),
  cooldown entre partidas, verificação de saldo
- Interface: mensagens e botões 100% editáveis pelo painel admin
- NUNCA acumula: navegação sempre via EDIT
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

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
    Game,
    GameSession,
    Transaction,
    TransactionType,
    User,
    session_scope,
)
from bot.logger import audit, log_payment, logger
from bot.redis_client import FlowState, LockManager, Throttle, get_redis
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="games")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_VITRINE = "games_vitrine"
MSG_KEY_CONFIRMAR = "game_confirmar"
MSG_KEY_RESULTADO = "game_resultado"
MSG_KEY_HIST = "games_hist"
MSG_KEY_RANKING = "games_ranking"
MSG_KEY_SEM_JOGOS = "games_sem"
MSG_KEY_SALDO_INSUF = "game_saldo_insuf"
MSG_KEY_LIMITE = "game_limite"
MSG_KEY_COOLDOWN = "game_cooldown"

SCREEN_VITRINE = "games_vitrine"
SCREEN_CONFIRMAR = "game_confirmar"
SCREEN_RESULTADO = "game_resultado"
SCREEN_HIST = "games_hist"
SCREEN_RANKING = "games_ranking"

# Callbacks
CB_GAMES_OPEN = "games:open"
CB_GAMES_PLAY = "games:play"          # games:play:ID
CB_GAMES_CONFIRM = "games:confirm"    # games:confirm:ID
CB_GAMES_CANCEL = "games:cancel"
CB_GAMES_AGAIN = "games:again"        # games:again:ID
CB_GAMES_HIST = "games:hist"          # games:hist:page:N
CB_GAMES_RANK = "games:rank"

HIST_PAGE_SIZE = 5
COOLDOWN_SECONDS = 5       # entre partidas
DEFAULT_DAILY_LIMIT = 50   # partidas por dia


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


async def _load_game(game_id: int) -> Optional[Game]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Game).where(Game.id == game_id))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _list_active_games() -> List[Game]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Game)
                .where(Game.active == True)  # noqa: E712
                .order_by(Game.cost.asc(), Game.id.asc())
            )
            return list(res.scalars().all())
    except Exception:
        return []


# ============================================================
# ⏱️ COOLDOWN / LIMITE
# ============================================================
async def _check_cooldown(user_id: int) -> int:
    """
    Retorna segundos restantes do cooldown (0 = liberado).
    """
    try:
        r = await get_redis()
        key = f"{settings.REDIS_PREFIX}:games:cooldown:{user_id}"
        ttl = await r.ttl(key)
        return max(0, int(ttl or 0))
    except Exception:
        return 0


async def _set_cooldown(user_id: int, seconds: int = COOLDOWN_SECONDS) -> None:
    try:
        r = await get_redis()
        key = f"{settings.REDIS_PREFIX}:games:cooldown:{user_id}"
        await r.set(key, "1", ex=seconds)
    except Exception:
        pass


async def _check_daily_limit(user: User, game: Game) -> Tuple[bool, int]:
    """
    Retorna (pode_jogar, partidas_restantes).
    Lê o limite de `game.limits.per_user_daily` (fallback DEFAULT_DAILY_LIMIT).
    """
    limits = game.limits or {}
    try:
        limit = int(limits.get("per_user_daily") or DEFAULT_DAILY_LIMIT)
    except Exception:
        limit = DEFAULT_DAILY_LIMIT

    if limit <= 0:
        return True, 0  # ilimitado

    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(GameSession.id))
                .where(GameSession.game_id == game.id)
                .where(GameSession.user_id == user.id)
                .where(GameSession.created_at >= since)
            )
            count = int(res.scalar() or 0)
        remaining = max(0, limit - count)
        return (remaining > 0), remaining
    except Exception:
        return True, 0


# ============================================================
# 🎲 SORTEIO DO RESULTADO
# ============================================================
def _draw_reward(game: Game) -> Optional[Dict[str, Any]]:
    """
    Sorteia um resultado a partir de `Game.rewards`.

    Formato esperado:
      [
        {"name": "Nada",  "amount": 0.00, "weight": 60, "type": "none"},
        {"name": "R$ 5",  "amount": 5.00, "weight": 25, "type": "balance"},
        {"name": "R$ 20", "amount": 20.00,"weight": 12, "type": "balance"},
        {"name": "R$ 100", "amount": 100.00,"weight": 3, "type": "balance"},
      ]

    Cada entrada DEVE ter "weight" (float > 0). Fallback: 1.
    Retorna sempre um dict (ou None se a lista estiver vazia).
    """
    rewards = game.rewards or []
    if not isinstance(rewards, list) or not rewards:
        return None

    entries: List[Tuple[int, Dict[str, Any], float]] = []
    for idx, r in enumerate(rewards):
        if not isinstance(r, dict):
            continue
        try:
            w = float(r.get("weight") or 1.0)
        except Exception:
            w = 1.0
        if w <= 0:
            w = 1.0
        entries.append((idx, r, w))

    if not entries:
        return None

    total = sum(w for _, _, w in entries)
    pick = random.uniform(0, total)
    upto = 0.0
    chosen = None
    for idx, r, w in entries:
        upto += w
        if pick <= upto:
            chosen = dict(r)
            break

    if chosen is None:
        chosen = dict(entries[-1][1])
    return chosen


# ============================================================
# TELA — VITRINE DE JOGOS
# ============================================================
async def _render_vitrine(user: User) -> Tuple[str, InlineKeyboardMarkup]:
    games = await _list_active_games()

    if not games:
        rendered = await message_engine.render(
            MSG_KEY_SEM_JOGOS,
            fallback=(
                "🎮 <b>Jogos</b>\n\n"
                "<i>Nenhum jogo disponível no momento.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["🎮 <b>Jogos Interativos</b>", ""]
    for g in games:
        preco = float(g.cost or 0)
        desc = (g.description or "—")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        linhas.append(
            f"🎯 <b>{g.name}</b>\n"
            f"    💵 {_fmt_brl(preco)} · 📝 {desc}"
        )
    linhas.append("")
    linhas.append(f"💰 Seu saldo: <b>{_fmt_brl(float(user.balance or 0))}</b>")

    rendered = await message_engine.render(
        MSG_KEY_VITRINE,
        fallback="\n".join(linhas),
        lista="\n".join(linhas),
        saldo=float(user.balance or 0),
    )

    kb = await button_engine.inline(SCREEN_VITRINE)
    if kb is None:
        rows: List[List[InlineKeyboardButton]] = []
        for g in games:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🎮 Jogar {g.name} · {_fmt_brl(float(g.cost or 0))}",
                        callback_data=f"{CB_GAMES_PLAY}:{g.id}",
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton(text="📜 Meu Histórico", callback_data=f"{CB_GAMES_HIST}:page:1")]
        )
        rows.append(
            [InlineKeyboardButton(text="🏆 Ranking", callback_data=CB_GAMES_RANK)]
        )
        rows.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data="catalog:open")]
        )
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    return rendered.content, kb


@router.callback_query(F.data == CB_GAMES_OPEN)
async def on_open_games(callback: CallbackQuery, user: User) -> None:
    try:
        if not await Throttle.allow(user.telegram_id, "games", cooldown=0.4):
            await callback.answer()
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)

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
        logger.exception(f"on_open_games falhou: {exc}")


# ============================================================
# TELA — CONFIRMAÇÃO
# ============================================================
async def _render_confirm(user: User, game: Game) -> Tuple[str, InlineKeyboardMarkup]:
    preco = float(game.cost or 0)
    ok, remaining = await _check_daily_limit(user, game)

    rendered = await message_engine.render(
        MSG_KEY_CONFIRMAR,
        fallback=(
            "🎮 <b>Confirmar partida</b>\n\n"
            f"🎯 Jogo: <b>{game.name}</b>\n"
            f"💵 Custo: <b>{_fmt_brl(preco)}</b>\n"
            f"💰 Seu saldo: <b>{_fmt_brl(float(user.balance or 0))}</b>\n"
            f"📊 Partidas restantes hoje: <b>{remaining if remaining > 0 else '∞'}</b>\n\n"
            "🎁 Prêmios variam conforme a sorte!\n"
            "Ao confirmar, o saldo será debitado e o resultado será sorteado."
        ),
        nome=game.name,
        preco=preco,
        saldo=float(user.balance or 0),
        restantes=remaining,
    )

    kb = await button_engine.inline(SCREEN_CONFIRMAR)
    if kb is None:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Jogar",
                        callback_data=f"{CB_GAMES_CONFIRM}:{game.id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data=CB_GAMES_CANCEL,
                    ),
                ]
            ]
        )
    return rendered.content, kb


@router.callback_query(F.data.startswith(f"{CB_GAMES_PLAY}:"))
async def on_play_game(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            game_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Jogo inválido.", show_alert=True)
            return

        game = await _load_game(game_id)
        if game is None or not game.active:
            await callback.answer("❌ Jogo indisponível.", show_alert=True)
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_confirm(fresh, game)

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
        logger.exception(f"on_play_game falhou: {exc}")


# ============================================================
# CANCELAR
# ============================================================
@router.callback_query(F.data == CB_GAMES_CANCEL)
async def on_cancel_game(callback: CallbackQuery, user: User) -> None:
    try:
        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)

        try:
            await callback.answer("❌ Cancelado.")
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
        logger.exception(f"on_cancel_game falhou: {exc}")


# ============================================================
# CONFIRMAR — executa a jogada
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_GAMES_CONFIRM}:"))
async def on_confirm_game(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            game_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Jogo inválido.", show_alert=True)
            return

        # ---------- Cooldown ----------
        cd = await _check_cooldown(user.id)
        if cd > 0:
            rendered = await message_engine.render(
                MSG_KEY_COOLDOWN,
                fallback=f"⏳ Aguarde <b>{cd}s</b> para jogar novamente.",
                segundos=cd,
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
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="🔙 Voltar",
                                        callback_data=CB_GAMES_OPEN,
                                    )
                                ]
                            ]
                        ),
                    )
                except Exception:
                    pass
            return

        # ---------- Trava de ação (evita duplo clique) ----------
        if not await LockManager.acquire_action(user.id, f"game:{game_id}", ttl=10):
            await callback.answer("⏳ Processando...", show_alert=False)
            return

        try:
            # ---------- Carrega game + user com lock ----------
            async with session_scope() as session:
                gres = await session.execute(
                    select(Game).where(Game.id == game_id)
                )
                game: Optional[Game] = gres.scalar_one_or_none()
                if game is None or not game.active:
                    await callback.answer("❌ Jogo indisponível.", show_alert=True)
                    return

                ures = await session.execute(
                    select(User).where(User.id == user.id).with_for_update()
                )
                u: Optional[User] = ures.scalar_one_or_none()
                if u is None:
                    await callback.answer("❌ Usuário não encontrado.", show_alert=True)
                    return

                # ---------- Limite diário ----------
                ok, remaining = await _check_daily_limit(u, game)
                if not ok:
                    rendered = await message_engine.render(
                        MSG_KEY_LIMITE,
                        fallback=(
                            "🚫 <b>Limite diário atingido.</b>\n\n"
                            "Você já jogou o máximo permitido hoje. "
                            "Volte amanhã para jogar novamente!"
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
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_GAMES_OPEN,
                                            )
                                        ]
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                preco = float(game.cost or 0)

                # ---------- Saldo ----------
                if float(u.balance or 0) < preco:
                    rendered = await message_engine.render(
                        MSG_KEY_SALDO_INSUF,
                        fallback=(
                            "❌ <b>Saldo insuficiente</b>\n\n"
                            f"💰 Seu saldo: {_fmt_brl(float(u.balance or 0))}\n"
                            f"💵 Custo da partida: {_fmt_brl(preco)}"
                        ),
                        saldo=float(u.balance or 0),
                        preco=preco,
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
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="💠 Recarregar",
                                                callback_data="recharge:open",
                                            )
                                        ],
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_GAMES_OPEN,
                                            )
                                        ],
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                # ---------- Sorteia resultado ----------
                reward = _draw_reward(game)
                if reward is None:
                    # Sem prêmios configurados: cancela sem cobrar
                    rendered = await message_engine.render(
                        MSG_KEY_SEM_JOGOS,
                        fallback="❌ Este jogo está sem prêmios configurados.",
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
                                reply_markup=InlineKeyboardMarkup(
                                    inline_keyboard=[
                                        [
                                            InlineKeyboardButton(
                                                text="🔙 Voltar",
                                                callback_data=CB_GAMES_OPEN,
                                            )
                                        ]
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                    return

                # ---------- Debita custo ----------
                new_balance = round(float(u.balance or 0) - preco, 2)
                u.balance = Decimal(str(new_balance))
                session.add(u)

                # ---------- Credita prêmio ----------
                reward_name = str(reward.get("name") or "Resultado")
                reward_amount = float(reward.get("amount") or 0)
                reward_type = str(reward.get("type") or "balance").lower()

                if reward_type == "balance" and reward_amount > 0:
                    new_balance = round(new_balance + reward_amount, 2)
                    u.balance = Decimal(str(new_balance))
                    session.add(u)

                    session.add(
                        Transaction(
                            user_id=u.id,
                            type=TransactionType.BONUS,
                            amount=Decimal(str(round(reward_amount, 2))),
                            balance_after=Decimal(str(new_balance)),
                            description=f"Prêmio jogo: {game.name} — {reward_name}",
                            reference=f"game:{game.id}",
                        )
                    )

                # ---------- Transaction do custo ----------
                session.add(
                    Transaction(
                        user_id=u.id,
                        type=TransactionType.PURCHASE,
                        amount=Decimal(str(round(preco, 2))),
                        balance_after=Decimal(str(new_balance)),
                        description=f"Jogada: {game.name}",
                        reference=f"game:{game.id}",
                    )
                )

                # ---------- Registra GameSession ----------
                gs = GameSession(
                    game_id=game.id,
                    user_id=u.id,
                    result={
                        "reward_name": reward_name,
                        "reward_amount": reward_amount,
                        "reward_type": reward_type,
                    },
                    cost=Decimal(str(round(preco, 2))),
                    reward=Decimal(str(round(reward_amount, 2))),
                )
                session.add(gs)
                await session.flush()

                final_balance = new_balance
                game_name = game.name
                game_id_local = int(game.id)

            # ---------- Cooldown ----------
            await _set_cooldown(user.id, COOLDOWN_SECONDS)

            # ---------- TELA DE RESULTADO ----------
            is_win = reward_amount > 0
            emoji = "🎉" if is_win else "😔"

            rendered = await message_engine.render(
                MSG_KEY_RESULTADO,
                fallback=(
                    f"{emoji} <b>Resultado da partida</b>\n\n"
                    f"🎯 Jogo: <b>{game_name}</b>\n"
                    f"🎁 Resultado: <b>{reward_name}</b>\n"
                    f"💵 Prêmio: <b>{_fmt_brl(reward_amount)}</b>\n"
                    f"💰 Novo saldo: <b>{_fmt_brl(final_balance)}</b>"
                ),
                jogo=game_name,
                resultado=reward_name,
                premio=reward_amount,
                saldo=final_balance,
                emoji=emoji,
                venceu=is_win,
            )

            kb = await button_engine.inline(SCREEN_RESULTADO)
            if kb is None:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🎮 Jogar Novamente",
                                callback_data=f"{CB_GAMES_AGAIN}:{game_id_local}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="📜 Meu Histórico",
                                callback_data=f"{CB_GAMES_HIST}:page:1",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🔙 Voltar",
                                callback_data=CB_GAMES_OPEN,
                            )
                        ],
                    ]
                )

            try:
                await callback.answer("🎲 Sorteado!")
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

            log_payment(
                "game.played",
                user_id=user.id,
                amount=preco,
                status="paid" if not is_win else "won",
                details={
                    "game_id": game_id_local,
                    "reward": reward_name,
                    "reward_amount": reward_amount,
                },
            )
            audit(
                action="game.played",
                user_id=user.id,
                details={"game_id": game_id_local, "reward_amount": reward_amount},
            )
        finally:
            await LockManager.release_action(user.id, f"game:{game_id}")
    except Exception as exc:
        logger.exception(f"on_confirm_game falhou: {exc}")
        try:
            await callback.answer("❌ Erro ao processar jogo.", show_alert=True)
        except Exception:
            pass


# ============================================================
# JOGAR NOVAMENTE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_GAMES_AGAIN}:"))
async def on_play_again(callback: CallbackQuery, user: User) -> None:
    try:
        try:
            game_id = int(callback.data.split(":")[-1])
        except Exception:
            await callback.answer("❌ Jogo inválido.", show_alert=True)
            return

        game = await _load_game(game_id)
        if game is None or not game.active:
            await callback.answer("❌ Jogo indisponível.", show_alert=True)
            return

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_confirm(fresh, game)

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
        logger.exception(f"on_play_again falhou: {exc}")


# ============================================================
# HISTÓRICO DO USUÁRIO
# ============================================================
async def _count_user_sessions(user_id: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(GameSession.id)).where(GameSession.user_id == user_id)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _load_user_sessions_page(
    user_id: int, page: int, per_page: int = HIST_PAGE_SIZE
) -> List[GameSession]:
    offset = max(0, (page - 1) * per_page)
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(GameSession)
                .where(GameSession.user_id == user_id)
                .order_by(GameSession.created_at.desc())
                .offset(offset)
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _render_history(user_id: int, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    total = await _count_user_sessions(user_id)
    total_pages = max(1, math.ceil(total / HIST_PAGE_SIZE))
    page = max(1, min(page, total_pages))

    sessions = await _load_user_sessions_page(user_id, page, HIST_PAGE_SIZE)

    if total == 0:
        rendered = await message_engine.render(
            MSG_KEY_HIST,
            fallback=(
                "📜 <b>Meu Histórico de Jogos</b>\n\n"
                "<i>Você ainda não jogou nenhuma partida.</i>"
            ),
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎮 Jogar", callback_data=CB_GAMES_OPEN)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_GAMES_OPEN)],
            ]
        )
        return rendered.content, kb

    linhas: List[str] = ["📜 <b>Meu Histórico de Jogos</b>", ""]
    for s in sessions:
        result = s.result or {}
        rname = result.get("reward_name") if isinstance(result, dict) else "—"
        ramt = result.get("reward_amount") if isinstance(result, dict) else 0
        try:
            ramt_f = float(ramt or 0)
        except Exception:
            ramt_f = 0.0

        emoji = "🎉" if ramt_f > 0 else "😔"
        linhas.append(
            f"{emoji} <b>#{s.id}</b> · {rname}\n"
            f"    💵 {_fmt_brl(ramt_f)} · {_fmt_dt(s.created_at)}"
        )

    rendered = await message_engine.render(
        MSG_KEY_HIST,
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
                callback_data=f"{CB_GAMES_HIST}:page:{page - 1}",
            )
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Próxima ➡️",
                callback_data=f"{CB_GAMES_HIST}:page:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [InlineKeyboardButton(text="🎮 Jogar", callback_data=CB_GAMES_OPEN)]
    )
    rows.append(
        [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_GAMES_OPEN)]
    )

    return rendered.content, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(f"{CB_GAMES_HIST}:page:"))
async def on_user_history(callback: CallbackQuery, user: User) -> None:
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
        logger.exception(f"on_user_history falhou: {exc}")


# ============================================================
# RANKING DE GANHADORES
# ============================================================
@router.callback_query(F.data == CB_GAMES_RANK)
async def on_games_ranking(callback: CallbackQuery, user: User) -> None:
    try:
        since = datetime.now(timezone.utc) - timedelta(days=30)
        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(
                        User.first_name,
                        User.telegram_id,
                        func.coalesce(func.sum(GameSession.reward), 0).label("total"),
                    )
                    .join(GameSession, GameSession.user_id == User.id)
                    .where(GameSession.created_at >= since)
                    .group_by(User.id, User.first_name, User.telegram_id)
                    .order_by(func.sum(GameSession.reward).desc())
                    .limit(10)
                )
                rows_data = res.all()
        except Exception:
            rows_data = []

        linhas: List[str] = ["🏆 <b>Ranking de Ganhadores (30 dias)</b>", ""]
        if not rows_data:
            linhas.append("<i>Sem dados ainda.</i>")
        else:
            medals = ["🥇", "🥈", "🥉"]
            for i, (nome, tg, total) in enumerate(rows_data, start=1):
                medal = medals[i - 1] if i <= 3 else ""
                label = (nome or f"Usuário {tg}")[:24]
                linhas.append(
                    f"{i}°) {label} {medal} — {_fmt_brl(float(total or 0))}"
                )

        rendered = await message_engine.render(
            MSG_KEY_RANKING,
            fallback="\n".join(linhas),
            lista="\n".join(linhas),
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎮 Jogar", callback_data=CB_GAMES_OPEN)],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_GAMES_OPEN)],
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
    except Exception as exc:
        logger.exception(f"on_games_ranking falhou: {exc}")


# ============================================================
# COMANDO /jogos
# ============================================================
@router.message(Command("jogos"))
async def on_cmd_games(message: Message, user: User) -> None:
    try:
        try:
            await message.delete()
        except Exception:
            pass

        fresh = await _reload_user(user.id) or user
        text, kb = await _render_vitrine(fresh)
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception(f"/jogos falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "on_open_games",
    "on_play_game",
    "on_cancel_game",
    "on_confirm_game",
    "on_play_again",
    "on_user_history",
    "on_games_ranking",
    "on_cmd_games",
    "CB_GAMES_OPEN",
    "CB_GAMES_PLAY",
    "CB_GAMES_CONFIRM",
    "CB_GAMES_CANCEL",
    "CB_GAMES_AGAIN",
    "CB_GAMES_HIST",
    "CB_GAMES_RANK",
    "COOLDOWN_SECONDS",
    "DEFAULT_DAILY_LIMIT",
]
