"""
Larizinha Store — Módulo Admin: Jogos
Implementa o CRUD completo de jogos conforme DIM.

Telas:
- Painel geral (total, ativos, partidas, arrecadado, prêmios)
- Lista de jogos (com paginação DIM)
- Criar jogo (nome → chave → custo → recompensas)
- Editor individual (todos os campos + recompensas + limites)
- Gerenciar recompensas (adicionar/editar/remover com peso)
- Configurar limites (por usuário, diário)
- Ativar/Desativar
- Histórico de partidas
- Ranking de ganhadores
- Relatórios

Regras invioláveis:
- NUNCA acumula: sempre EDIT (edit_or_send).
- Inputs via ForceReply com AdminFlow (apaga a mensagem do admin após capturar).
- Toda mensagem/botão 100% editáveis pelo painel (message_engine + button_engine).
- Todos os dados vêm do banco em runtime.
- Toda ação crítica é auditada (log_action).
"""
from __future__ import annotations

import json
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
    Game,
    GameSession,
    User,
    session_scope,
)
from bot.logger import logger
from bot.services.message_engine import message_engine
from bot.keyboards.button_engine import button_engine

from bot.handlers.admin.helpers import (
    ADMIN_PAGE_SIZE_DEFAULT,
    AdminFlow,
    SYM_OFF,
    SYM_OK,
    SYM_WARN,
    answer,
    ask_input,
    build_back_row,
    build_confirm_keyboard,
    build_pagination_row,
    consume_input,
    edit_or_send,
    fmt_brl,
    fmt_dt,
    fmt_num,
    humanize_bool,
    is_int,
    kb,
    log_action,
    page_offset,
    parse_int,
    parse_number,
    table,
    toast_err,
    toast_ok,
    total_pages,
    truncate,
)


# ============================================================
# 🎛️ ROUTER
# ============================================================
router = Router(name="admin_games")


# ============================================================
# 🔑 CHAVES
# ============================================================
MSG_KEY_LIST = "admin_games_list"
MSG_KEY_EMPTY = "admin_games_empty"
MSG_KEY_DETAIL = "admin_games_detail"
MSG_KEY_REWARDS = "admin_games_rewards"

SCREEN_LIST = "admin_games_list"
SCREEN_DETAIL = "admin_games_detail"

# Callbacks base
CB_ROOT = "admin:games"
CB_PAGE = "admin:games:page"              # :N
CB_NEW = "admin:games:new"
CB_VIEW = "admin:games:view"              # :ID
CB_SET = "admin:games:set"                # :field:ID
CB_TOGGLE = "admin:games:toggle"          # :field:ID
CB_DELETE = "admin:games:delete"          # :ID
CB_DELETE_YES = "admin:games:delete:yes"  # :ID
CB_REWARDS = "admin:games:rewards"        # :ID
CB_REWARD_ADD = "admin:games:radd"        # :ID
CB_REWARD_DEL = "admin:games:rdel"        # :ID:IDX
CB_REWARD_WEIGHT = "admin:games:rweight"  # :ID:IDX
CB_REWARD_AMOUNT = "admin:games:ramount"  # :ID:IDX
CB_REWARD_NAME = "admin:games:rname"      # :ID:IDX
CB_LIMITS = "admin:games:limits"          # :ID
CB_HISTORY = "admin:games:history"        # :ID
CB_REPORT = "admin:games:report"

# Campos editáveis via texto
TEXT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {"label": "Nome", "validate": "text", "max": 80},
    "key": {"label": "Chave única", "validate": "slug", "max": 40},
    "description": {"label": "Descrição", "validate": "text", "max": 400},
    "cost": {"label": "Custo (R$)", "validate": "money"},
}

# Toggles
TOGGLE_FIELDS: Dict[str, str] = {
    "active": "Ativo",
}

# Limites configuráveis (dentro do JSON `limits`)
LIMIT_FIELDS: Dict[str, str] = {
    "per_user_daily": "Limite diário por usuário",
    "per_user_total": "Limite total por usuário",
    "min_balance": "Saldo mínimo para jogar",
}


# ============================================================
# 🧰 HELPERS
# ============================================================
async def _load_game(gid: int) -> Optional[Game]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Game).where(Game.id == gid))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _load_game_by_key(key: str) -> Optional[Game]:
    try:
        async with session_scope() as session:
            res = await session.execute(select(Game).where(Game.key == key))
            return res.scalar_one_or_none()
    except Exception:
        return None


async def _count_games() -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(select(func.count(Game.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


async def _list_games(
    page: int,
    per_page: int = ADMIN_PAGE_SIZE_DEFAULT,
) -> List[Game]:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(Game)
                .order_by(Game.id.asc())
                .offset(page_offset(page, per_page))
                .limit(per_page)
            )
            return list(res.scalars().all())
    except Exception:
        return []


async def _count_sessions(gid: int) -> int:
    try:
        async with session_scope() as session:
            res = await session.execute(
                select(func.count(GameSession.id)).where(GameSession.game_id == gid)
            )
            return int(res.scalar() or 0)
    except Exception:
        return 0


def _rewards_to_list(rewards: Any) -> List[Dict[str, Any]]:
    if not isinstance(rewards, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in rewards:
        if isinstance(r, dict):
            out.append(dict(r))
        else:
            out.append({"name": str(r), "amount": 0.0, "weight": 1.0})
    return out


def _limits_to_dict(limits: Any) -> Dict[str, Any]:
    if isinstance(limits, dict):
        return dict(limits)
    return {}


def _slugify(v: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9_]+", "_", (v or "").strip().lower())
    return s.strip("_") or "game"


# ============================================================
# LISTA
# ============================================================
async def render_list(
    target: Union[CallbackQuery, Message],
    *,
    page: int = 1,
) -> Optional[Message]:
    total = await _count_games()
    total_p = total_pages(total, ADMIN_PAGE_SIZE_DEFAULT)
    page = max(1, min(page, total_p))

    games = await _list_games(page)

    if not games:
        rendered = await message_engine.render(
            MSG_KEY_EMPTY,
            fallback=(
                "🎮 <b>JOGOS</b>\n\n"
                "<i>Nenhum jogo cadastrado.</i>\n\n"
                "Use ➕ Novo Jogo para começar."
            ),
        )
    else:
        rows: List[Sequence[str]] = []
        for g in games:
            ativo = SYM_OK if g.active else SYM_OFF
            rew = _rewards_to_list(g.rewards)
            rows.append(
                [
                    str(g.id),
                    truncate(g.name or "—", 18),
                    truncate(g.key or "—", 14),
                    fmt_brl(g.cost),
                    f"{len(rew)}",
                    ativo,
                ]
            )

        lista = table(rows, ["ID", "Nome", "Key", "Custo", "Pr.", "S"])
        rendered = await message_engine.render(
            MSG_KEY_LIST,
            fallback=(
                f"🎮 <b>JOGOS</b> ({total})\n\n"
                f"<code>{lista}</code>"
            ),
            total=total,
            lista=lista,
        )

    engine_kb = await button_engine.inline(SCREEN_LIST)
    if engine_kb is not None and engine_kb.inline_keyboard:
        rows_kb = [list(r) for r in engine_kb.inline_keyboard]
    else:
        rows_kb = [
            [
                InlineKeyboardButton(text="➕ Novo Jogo", callback_data=CB_NEW),
                InlineKeyboardButton(text="📊 Relatório", callback_data=CB_REPORT),
            ],
        ]

    for g in games:
        emoji = "🎮" if g.active else "🎲"
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"{emoji} #{g.id} {truncate(g.name or '—', 22)} · "
                    f"{fmt_brl(g.cost)}",
                    callback_data=f"{CB_VIEW}:{g.id}",
                )
            ]
        )

    pag = build_pagination_row(
        current_page=page,
        total_pages=total_p,
        base_callback=CB_PAGE,
        back_callback=CB_ROOT,
    )
    if pag:
        rows_kb.append(pag)

    rows_kb.append(build_back_row("admin:menu"))

    return await edit_or_send(target, text=rendered.content, reply_markup=kb(rows_kb))


@router.callback_query(F.data == CB_ROOT)
async def on_root(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        await AdminFlow.clear(admin_id)
        await render_list(callback, page=1)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_root falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
async def on_page(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            page = int(callback.data.split(":")[-1])
        except Exception:
            page = 1
        await render_list(callback, page=page)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_page falhou: {exc}")


# ============================================================
# CRIAR
# ============================================================
@router.callback_query(F.data == CB_NEW)
async def on_new(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        prompt = (
            "➕ <b>NOVO JOGO · PASSO 1/3</b>\n\n"
            "Envie o <b>nome</b> do jogo.\n"
            "Ex.: <code>Roleta da Sorte</code>, <code>Dado Mágico</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field="game_new_name",
            screen="games:new",
            use_force_reply=True,
            reply_placeholder="Nome do jogo",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_new falhou: {exc}")


@router.message(F.text & ~F.text.startswith("/"))
async def on_games_text_input(message: Message) -> None:
    admin_id = message.from_user.id if message.from_user else 0
    if admin_id not in settings.admin_ids_list:
        return

    state = await AdminFlow.get(admin_id)
    if not state:
        return
    awaiting = (state.get("data") or {}).get("awaiting")
    if not awaiting:
        return

    # Criação (passo 1: nome)
    if awaiting == "game_new_name":
        value, _extra = await consume_input(message, admin_id, expected_field="game_new_name")
        if value is None:
            return
        await _create_step1(message, admin_id, value)
        return

    # Criação (passo 2: key)
    if awaiting == "game_new_key":
        value, _extra = await consume_input(message, admin_id, expected_field="game_new_key")
        if value is None:
            return
        await _create_step2(message, admin_id, value)
        return

    # Criação (passo 3: custo)
    if awaiting == "game_new_cost":
        value, _extra = await consume_input(message, admin_id, expected_field="game_new_cost")
        if value is None:
            return
        await _create_step3(message, admin_id, value)
        return

    # Editar campos
    if awaiting.startswith("set:"):
        parts = awaiting.split(":", 2)
        field = parts[1]
        gid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_field_value(message, admin_id, gid, field, value)
        return

    # Limites
    if awaiting.startswith("glimit:"):
        # glimit:field:ID
        parts = awaiting.split(":")
        field = parts[1]
        gid = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_limit(message, admin_id, gid, field, value)
        return

    # Recompensas (editar campo)
    if awaiting.startswith("grew_"):
        # grew_name:ID:IDX / grew_amount:ID:IDX / grew_weight:ID:IDX
        parts = awaiting.split(":")
        action = parts[0].split("_", 1)[1]
        gid = int(parts[1])
        idx = int(parts[2])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _apply_reward_field(message, admin_id, gid, idx, action, value)
        return

    # Adicionar recompensa (nome → valor → peso)
    if awaiting.startswith("grewadd_"):
        stage = awaiting.split("_", 1)[1].split(":", 1)[0]
        gid = int(awaiting.split(":")[1])
        value, _extra = await consume_input(message, admin_id, expected_field=awaiting)
        if value is None:
            return
        await _handle_reward_add(message, admin_id, gid, stage, value)
        return


async def _create_step1(message: Message, admin_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name or len(name) > 80:
        await message.answer("❌ Nome inválido.")
        return

    suggested_key = _slugify(name)

    await AdminFlow.update(admin_id, {"new_name": name, "new_key_suggest": suggested_key})

    prompt = (
        f"➕ <b>NOVO JOGO · PASSO 2/3</b>\n\n"
        f"✅ Nome: <b>{name}</b>\n\n"
        f"Envie a <b>chave única</b> (slug).\n"
        f"Sugestão: <code>{suggested_key}</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="game_new_key",
        screen="games:new",
        use_force_reply=True,
        reply_placeholder=suggested_key,
    )


async def _create_step2(message: Message, admin_id: int, raw_key: str) -> None:
    key = _slugify(raw_key or "")
    if not key or len(key) > 40:
        await message.answer("❌ Chave inválida.")
        return

    # Verifica duplicidade
    existing = await _load_game_by_key(key)
    if existing is not None:
        await message.answer("❌ Já existe um jogo com esta chave.")
        return

    await AdminFlow.update(admin_id, {"new_key": key})

    prompt = (
        f"➕ <b>NOVO JOGO · PASSO 3/3</b>\n\n"
        f"🔑 Chave: <code>{key}</code>\n\n"
        f"Envie o <b>custo</b> em R$ para jogar.\n"
        "Ex.: <code>5.00</code> ou <code>10</code>\n\n"
        "⚠️ A mensagem será apagada automaticamente."
    )
    await ask_input(
        message,
        prompt,
        field="game_new_cost",
        screen="games:new",
        use_force_reply=True,
        reply_placeholder="5.00",
    )


async def _create_step3(message: Message, admin_id: int, raw_cost: str) -> None:
    cost = parse_number(raw_cost)
    if cost is None or cost < 0:
        await message.answer("❌ Custo inválido.")
        return

    state = await AdminFlow.get(admin_id)
    data = (state or {}).get("data", {}) if state else {}
    name = data.get("new_name")
    key = data.get("new_key")

    if not name or not key:
        await message.answer("❌ Fluxo incompleto. Recomece.")
        await AdminFlow.clear(admin_id)
        return

    try:
        async with session_scope() as session:
            g = Game(
                key=key,
                name=name,
                description="",
                cost=Decimal(str(round(cost, 2))),
                rewards=[],
                limits={"per_user_daily": 50},
                active=True,
            )
            session.add(g)
            await session.flush()
            gid = int(g.id)
    except Exception as exc:
        logger.exception(f"_create_step3 (game) falhou: {exc}")
        await message.answer("❌ Erro ao criar jogo.")
        return

    await AdminFlow.clear(admin_id)

    await log_action(
        admin_id=admin_id,
        action="game.created",
        category="admin",
        details={"game_id": gid, "name": name, "key": key, "cost": cost},
    )

    await message.answer(
        f"✅ <b>Jogo criado!</b>\n\n"
        f"🎮 Nome: <b>{name}</b>\n"
        f"🔑 Chave: <code>{key}</code>\n"
        f"💵 Custo: <b>{fmt_brl(cost)}</b>\n"
        f"🆔 ID: <code>#{gid}</code>\n\n"
        f"⚠️ Adicione recompensas usando o botão 🎁 Recompensas.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="✏️ Editar Jogo", callback_data=f"{CB_VIEW}:{gid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)],
            ]
        ),
    )


# ============================================================
# DETALHE (EDITOR)
# ============================================================
async def _render_detail(gid: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
    g = await _load_game(gid)
    if g is None:
        return None, None

    sessions = await _count_sessions(gid)
    rewards = _rewards_to_list(g.rewards)
    limits = _limits_to_dict(g.limits)

    reward_lines: List[str] = []
    for i, r in enumerate(rewards[:8]):
        name = str(r.get("name") or "—")
        amount = float(r.get("amount") or 0)
        weight = float(r.get("weight") or 1)
        reward_lines.append(
            f"  {i}. {truncate(name, 20)} · {fmt_brl(amount)} · peso {weight:g}"
        )

    rewards_text = "\n".join(reward_lines) if reward_lines else "  (nenhuma)"

    limits_text = ", ".join(
        f"{k}={v}" for k, v in limits.items() if v
    ) if limits else "—"

    rendered = await message_engine.render(
        MSG_KEY_DETAIL,
        fallback=(
            f"🎮 <b>JOGO #{g.id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ Nome: <b>{g.name}</b>\n"
            f"├ Chave: <code>{g.key}</code>\n"
            f"├ Descrição: {truncate(g.description or '—', 60)}\n"
            f"├ Custo: <b>{fmt_brl(g.cost)}</b>\n"
            f"├ Ativo: {humanize_bool(g.active)}\n"
            f"├ Partidas: <b>{fmt_num(sessions)}</b>\n"
            f"└ Limites: <code>{limits_text}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 <b>Recompensas ({len(rewards)}):</b>\n"
            f"{rewards_text}"
        ),
        id=g.id,
        nome=g.name or "—",
        chave=g.key or "—",
        descricao=truncate(g.description or "—", 60),
        custo=fmt_brl(g.cost),
        ativo=humanize_bool(g.active),
        partidas=fmt_num(sessions),
        limites=limits_text,
        premios=f"{len(rewards)}",
        recompensas=rewards_text,
        regras=truncate(g.description or "—", 60),
        total=f"{len(rewards)}",
    )

    engine_kb = await button_engine.inline(SCREEN_DETAIL)
    if engine_kb is not None and engine_kb.inline_keyboard:
        keyboard = engine_kb
    else:
        keyboard = kb(_default_detail_keyboard(g))

    return rendered.content, keyboard


def _default_detail_keyboard(g: Game) -> List[List[InlineKeyboardButton]]:
    def b(text: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=cb)

    return [
        [b("✏️ Nome", f"{CB_SET}:name:{g.id}"), b("✏️ Chave", f"{CB_SET}:key:{g.id}")],
        [b("✏️ Descrição", f"{CB_SET}:description:{g.id}")],
        [b("💵 Custo", f"{CB_SET}:cost:{g.id}")],
        [b("🎁 Gerenciar Recompensas", f"{CB_REWARDS}:{g.id}")],
        [b("🔢 Limites", f"{CB_LIMITS}:{g.id}")],
        [b("🔄 Ativo/Inativo", f"{CB_TOGGLE}:active:{g.id}")],
        [b("📜 Histórico", f"{CB_HISTORY}:{g.id}")],
        [b("🗑️ Excluir", f"{CB_DELETE}:{g.id}")],
        [b("🔙 Voltar", CB_ROOT)],
    ]


@router.callback_query(F.data.startswith(f"{CB_VIEW}:"))
async def on_view(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return
        text, keyboard = await _render_detail(gid)
        if text is None:
            await toast_err(callback, "❌ Jogo não encontrado.")
            return
        await edit_or_send(callback, text=text, reply_markup=keyboard)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_view falhou: {exc}")


# ============================================================
# SET FIELD
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_SET}:"))
async def on_set_field(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:games:set:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        gid = int(parts[4])

        if field not in TEXT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        meta = TEXT_FIELDS[field]
        label = meta["label"]

        hint = ""
        if meta["validate"] == "money":
            hint = "Ex.: <code>5.00</code>"
        elif meta["validate"] == "slug":
            hint = "Letras minúsculas, números e _ (underscore)."
        else:
            hint = f"Até {meta.get('max', 80)} caracteres"

        prompt = (
            f"✏️ <b>EDITAR {label.upper()}</b>\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"set:{field}:{gid}",
            screen=f"games:edit:{gid}",
            extra={"game_id": gid},
            use_force_reply=True,
            reply_placeholder=label,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_set_field falhou: {exc}")


async def _apply_field_value(
    message: Message,
    admin_id: int,
    gid: int,
    field: str,
    value: str,
) -> None:
    meta = TEXT_FIELDS.get(field)
    if not meta:
        await message.answer("❌ Campo inválido.")
        return

    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if meta["validate"] == "money":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = Decimal(str(round(num, 2)))
    elif meta["validate"] == "slug":
        parsed = _slugify(val)
        if not parsed:
            await message.answer("❌ Chave inválida.")
            return
        # Verifica duplicidade se for o campo key
        if field == "key":
            try:
                async with session_scope() as session:
                    from sqlalchemy import func as _func

                    r = await session.execute(
                        select(_func.count(Game.id)).where(
                            Game.key == parsed,
                            Game.id != gid,
                        )
                    )
                    if int(r.scalar() or 0) > 0:
                        await message.answer("❌ Chave já em uso.")
                        return
            except Exception:
                pass
    else:
        if len(val) > meta.get("max", 200):
            await message.answer("❌ Texto muito longo.")
            return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Game).where(Game.id == gid))
            g: Optional[Game] = r.scalar_one_or_none()
            if g is None:
                await message.answer("❌ Jogo não encontrado.")
                return
            setattr(g, field, parsed)
            session.add(g)
    except Exception as exc:
        logger.exception(f"_apply_field_value (game) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"game.set.{field}",
        category="admin",
        details={"game_id": gid},
    )

    text, keyboard = await _render_detail(gid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# TOGGLE
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_TOGGLE}:"))
async def on_toggle(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:games:toggle:field:ID
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        field = parts[3]
        gid = int(parts[4])

        if field not in TOGGLE_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Game).where(Game.id == gid))
                g: Optional[Game] = r.scalar_one_or_none()
                if g is None:
                    await toast_err(callback, "❌ Jogo não encontrado.")
                    return
                current = bool(getattr(g, field))
                setattr(g, field, not current)
                session.add(g)
                new_val = not current
        except Exception as exc:
            logger.exception(f"toggle game falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action=f"game.toggle.{field}",
            category="admin",
            details={"game_id": gid, "value": new_val},
        )

        text, keyboard = await _render_detail(gid)
        if text:
            await edit_or_send(callback, text=text, reply_markup=keyboard)
        await toast_ok(callback, f"🔄 {TOGGLE_FIELDS[field]} alterado.")
    except Exception as exc:
        logger.exception(f"games on_toggle falhou: {exc}")


# ============================================================
# LIMITES
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_LIMITS}:"))
async def on_limits(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        g = await _load_game(gid)
        if g is None:
            await toast_err(callback, "❌ Jogo não encontrado.")
            return

        limits = _limits_to_dict(g.limits)

        lines = [f"🔢 <b>LIMITES · JOGO #{gid}</b>", ""]
        for k, label in LIMIT_FIELDS.items():
            v = limits.get(k)
            lines.append(f"• {label}: <b>{v if v is not None else '—'}</b>")
        text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = []
        for k, label in LIMIT_FIELDS.items():
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"✏️ {label}",
                        callback_data=f"admin:games:limset:{gid}:{k}",
                    )
                ]
            )
        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_limits falhou: {exc}")


@router.callback_query(F.data.startswith("admin:games:limset:"))
async def on_limit_set(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        # admin:games:limset:GID:FIELD
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        gid = int(parts[4])
        field = parts[5]

        if field not in LIMIT_FIELDS:
            await toast_err(callback, "❌ Campo inválido.")
            return

        label = LIMIT_FIELDS[field]
        hint = "Ex.: <code>50</code> (0 = ilimitado)"

        prompt = (
            f"✏️ <b>EDITAR LIMITE</b>\n\n"
            f"🏷️ {label}\n\n"
            f"Envie o novo valor.\n{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"glimit:{field}:{gid}",
            screen=f"games:limit:{gid}",
            extra={"game_id": gid, "field": field},
            use_force_reply=True,
            reply_placeholder="50",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_limit_set falhou: {exc}")


async def _apply_limit(
    message: Message,
    admin_id: int,
    gid: int,
    field: str,
    value: str,
) -> None:
    if field not in LIMIT_FIELDS:
        await message.answer("❌ Campo inválido.")
        return

    num = parse_int(value)
    if num is None or num < 0:
        await message.answer("❌ Número inválido.")
        return

    try:
        async with session_scope() as session:
            r = await session.execute(select(Game).where(Game.id == gid))
            g: Optional[Game] = r.scalar_one_or_none()
            if g is None:
                await message.answer("❌ Jogo não encontrado.")
                return
            limits = _limits_to_dict(g.limits)
            limits[field] = int(num)
            g.limits = limits
            session.add(g)
    except Exception as exc:
        logger.exception(f"_apply_limit falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"game.limit.set.{field}",
        category="admin",
        details={"game_id": gid, "value": int(num)},
    )

    text, keyboard = await _render_detail(gid)
    if text:
        await message.answer(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


# ============================================================
# RECOMPENSAS
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_REWARDS}:"))
async def on_rewards(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        g = await _load_game(gid)
        if g is None:
            await toast_err(callback, "❌ Jogo não encontrado.")
            return

        rewards = _rewards_to_list(g.rewards)

        lines = [f"🎁 <b>RECOMPENSAS · JOGO #{gid}</b>", ""]
        if not rewards:
            lines.append("<i>Nenhuma recompensa cadastrada ainda.</i>")
        else:
            for i, r in enumerate(rewards):
                name = str(r.get("name") or "—")
                amount = float(r.get("amount") or 0)
                weight = float(r.get("weight") or 1)
                lines.append(
                    f"<b>{i}.</b> {truncate(name, 24)}\n"
                    f"    💵 {fmt_brl(amount)} · peso {weight:g}"
                )

        text = "\n".join(lines)

        rows_kb: List[List[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="➕ Adicionar Recompensa",
                    callback_data=f"{CB_REWARD_ADD}:{gid}",
                )
            ],
        ]

        for i, r in enumerate(rewards[:10]):
            rows_kb.append(
                [
                    InlineKeyboardButton(
                        text=f"✏️ #{i} {truncate(str(r.get('name') or '—'), 20)}",
                        callback_data=f"admin:games:redit:{gid}:{i}",
                    )
                ]
            )

        rows_kb.append(
            [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")]
        )

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_rewards falhou: {exc}")


@router.callback_query(F.data.startswith("admin:games:redit:"))
async def on_reward_edit(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 6:
            await toast_err(callback, "❌ Callback inválido.")
            return
        gid = int(parts[4])
        idx = int(parts[5])

        g = await _load_game(gid)
        if g is None:
            await toast_err(callback, "❌ Jogo não encontrado.")
            return

        rewards = _rewards_to_list(g.rewards)
        if idx < 0 or idx >= len(rewards):
            await toast_err(callback, "❌ Recompensa não encontrada.")
            return

        r = rewards[idx]
        name = str(r.get("name") or "—")
        amount = float(r.get("amount") or 0)
        weight = float(r.get("weight") or 1)

        text = (
            f"🎁 <b>EDITAR RECOMPENSA #{idx}</b>\n\n"
            f"├ Nome: <b>{name}</b>\n"
            f"├ Valor: <b>{fmt_brl(amount)}</b>\n"
            f"└ Peso: <b>{weight:g}</b>"
        )

        rows_kb = [
            [
                InlineKeyboardButton(
                    text="✏️ Nome",
                    callback_data=f"{CB_REWARD_NAME}:{gid}:{idx}",
                ),
                InlineKeyboardButton(
                    text="💵 Valor",
                    callback_data=f"{CB_REWARD_AMOUNT}:{gid}:{idx}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚖️ Peso",
                    callback_data=f"{CB_REWARD_WEIGHT}:{gid}:{idx}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑️ Remover",
                    callback_data=f"{CB_REWARD_DEL}:{gid}:{idx}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔙 Voltar",
                    callback_data=f"{CB_REWARDS}:{gid}",
                )
            ],
        ]

        await edit_or_send(callback, text=text, reply_markup=kb(rows_kb))
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_reward_edit falhou: {exc}")


@router.callback_query(F.data.startswith("admin:games:grew:"))
async def on_reward_set(callback: CallbackQuery) -> None:
    """
    Formato: admin:games:grew:ACTION:GID:IDX
    Actions: name, amount, weight
    """
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 7:
            await toast_err(callback, "❌ Callback inválido.")
            return
        action = parts[4]
        gid = int(parts[5])
        idx = int(parts[6])

        if action not in ("name", "amount", "weight"):
            await toast_err(callback, "❌ Ação inválida.")
            return

        hint = ""
        if action == "amount":
            hint = "Ex.: <code>10.00</code>"
        elif action == "weight":
            hint = "Peso relativo. Ex.: <code>50</code> (maior = mais chance)"
        else:
            hint = "Nome da recompensa"

        prompt = (
            f"✏️ <b>EDITAR RECOMPENSA · {action.upper()}</b>\n\n"
            f"{hint}\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"grew_{action}:{gid}:{idx}",
            screen=f"games:grew:{gid}",
            extra={"game_id": gid, "idx": idx, "action": action},
            use_force_reply=True,
            reply_placeholder=action,
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_reward_set falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_REWARD_DEL}:"))
async def on_reward_del(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) < 5:
            await toast_err(callback, "❌ Callback inválido.")
            return
        gid = int(parts[3])
        idx = int(parts[4])

        try:
            async with session_scope() as session:
                r = await session.execute(select(Game).where(Game.id == gid))
                g: Optional[Game] = r.scalar_one_or_none()
                if g is None:
                    await toast_err(callback, "❌ Jogo não encontrado.")
                    return
                rewards = _rewards_to_list(g.rewards)
                if 0 <= idx < len(rewards):
                    rewards.pop(idx)
                    g.rewards = rewards
                    session.add(g)
        except Exception as exc:
            logger.exception(f"reward del falhou: {exc}")
            await toast_err(callback, "❌ Erro.")
            return

        await log_action(
            admin_id=admin_id,
            action="game.reward.removed",
            category="admin",
            details={"game_id": gid, "idx": idx},
        )

        callback.data = f"{CB_REWARDS}:{gid}"  # type: ignore[misc]
        await on_rewards(callback)
        await toast_ok(callback, "🗑️ Recompensa removida.")
    except Exception as exc:
        logger.exception(f"games on_reward_del falhou: {exc}")


async def _apply_reward_field(
    message: Message,
    admin_id: int,
    gid: int,
    idx: int,
    action: str,
    value: str,
) -> None:
    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    parsed: Any = val
    if action == "amount":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        parsed = float(num)
    elif action == "weight":
        num = parse_number(val)
        if num is None or num <= 0:
            await message.answer("❌ Peso inválido.")
            return
        parsed = float(num)

    try:
        async with session_scope() as session:
            r = await session.execute(select(Game).where(Game.id == gid))
            g: Optional[Game] = r.scalar_one_or_none()
            if g is None:
                await message.answer("❌ Jogo não encontrado.")
                return
            rewards = _rewards_to_list(g.rewards)
            if idx < 0 or idx >= len(rewards):
                await message.answer("❌ Recompensa não encontrada.")
                return
            rewards[idx][action] = parsed
            g.rewards = rewards
            session.add(g)
    except Exception as exc:
        logger.exception(f"_apply_reward_field (game) falhou: {exc}")
        await message.answer("❌ Erro ao salvar.")
        return

    await log_action(
        admin_id=admin_id,
        action=f"game.reward.set.{action}",
        category="admin",
        details={"game_id": gid, "idx": idx},
    )

    await message.answer(
        text=f"✅ Recompensa #{idx} · {action} atualizado para <code>{parsed}</code>.",
        parse_mode="HTML",
        reply_markup=kb(
            [
                [InlineKeyboardButton(text="🎁 Ver Recompensas", callback_data=f"{CB_REWARDS}:{gid}")],
                [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith(f"{CB_REWARD_ADD}:"))
async def on_reward_add(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        prompt = (
            "➕ <b>NOVA RECOMPENSA · PASSO 1/3</b>\n\n"
            "Envie o <b>nome</b> da recompensa.\n"
            "Ex.: <code>R$ 10</code>, <code>Prêmio Ouro</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            callback,
            prompt,
            field=f"grewadd_name:{gid}",
            screen=f"games:grewadd:{gid}",
            extra={"game_id": gid},
            use_force_reply=True,
            reply_placeholder="Nome da recompensa",
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_reward_add falhou: {exc}")


async def _handle_reward_add(
    message: Message,
    admin_id: int,
    gid: int,
    stage: str,
    value: str,
) -> None:
    val = (value or "").strip()
    if not val:
        await message.answer("❌ Valor vazio.")
        return

    if stage == "name":
        await AdminFlow.update(admin_id, {
            "new_reward_name": val,
            "new_reward_gid": gid,
        })
        prompt = (
            f"➕ <b>NOVA RECOMPENSA · PASSO 2/3</b>\n\n"
            f"✅ Nome: <b>{val}</b>\n\n"
            "Envie o <b>valor</b> em R$ (0 se não creditar saldo).\n"
            "Ex.: <code>10.00</code> ou <code>0</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field=f"grewadd_amount:{gid}",
            screen=f"games:grewadd:{gid}",
            extra={"game_id": gid},
            use_force_reply=True,
            reply_placeholder="10.00",
        )
        return

    if stage == "amount":
        num = parse_number(val)
        if num is None or num < 0:
            await message.answer("❌ Valor inválido.")
            return
        await AdminFlow.update(admin_id, {"new_reward_amount": float(num)})
        prompt = (
            f"➕ <b>NOVA RECOMPENSA · PASSO 3/3</b>\n\n"
            f"💵 Valor: <b>{fmt_brl(num)}</b>\n\n"
            "Agora envie o <b>peso</b> da recompensa.\n"
            "Peso maior = mais chance de sair.\n"
            "Ex.: <code>50</code>, <code>10</code>, <code>1</code>\n\n"
            "⚠️ A mensagem será apagada automaticamente."
        )
        await ask_input(
            message,
            prompt,
            field=f"grewadd_weight:{gid}",
            screen=f"games:grewadd:{gid}",
            extra={"game_id": gid},
            use_force_reply=True,
            reply_placeholder="50",
        )
        return

    if stage == "weight":
        num = parse_number(val)
        if num is None or num <= 0:
            await message.answer("❌ Peso inválido.")
            return

        state2 = await AdminFlow.get(admin_id)
        data2 = (state2 or {}).get("data", {}) if state2 else {}
        name = data2.get("new_reward_name")
        amount = float(data2.get("new_reward_amount") or 0)

        if not name:
            await message.answer("❌ Fluxo incompleto. Recomece.")
            await AdminFlow.clear(admin_id)
            return

        try:
            async with session_scope() as session:
                r = await session.execute(select(Game).where(Game.id == gid))
                g: Optional[Game] = r.scalar_one_or_none()
                if g is None:
                    await message.answer("❌ Jogo não encontrado.")
                    return
                rewards = _rewards_to_list(g.rewards)
                rewards.append({
                    "name": str(name),
                    "amount": float(amount),
                    "weight": float(num),
                    "type": "balance" if amount > 0 else "none",
                })
                g.rewards = rewards
                session.add(g)
        except Exception as exc:
            logger.exception(f"add reward game falhou: {exc}")
            await message.answer("❌ Erro ao salvar.")
            return

        await AdminFlow.clear(admin_id)

        await log_action(
            admin_id=admin_id,
            action="game.reward.added",
            category="admin",
            details={"game_id": gid, "name": name, "amount": amount, "weight": float(num)},
        )

        await message.answer(
            f"✅ <b>Recompensa adicionada!</b>\n\n"
            f"🎁 {name}\n"
            f"💵 {fmt_brl(amount)}\n"
            f"⚖️ Peso: {float(num):g}",
            parse_mode="HTML",
            reply_markup=kb(
                [
                    [InlineKeyboardButton(text="🎁 Ver Recompensas", callback_data=f"{CB_REWARDS}:{gid}")],
                    [InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")],
                ]
            ),
        )
        return


# ============================================================
# HISTÓRICO
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_HISTORY}:"))
async def on_history(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                res = await session.execute(
                    select(GameSession)
                    .where(GameSession.game_id == gid)
                    .order_by(GameSession.created_at.desc())
                    .limit(30)
                )
                sessions = list(res.scalars().all())
        except Exception:
            sessions = []

        if not sessions:
            text = "📜 <i>Nenhuma partida registrada neste jogo.</i>"
        else:
            lines = [f"📜 <b>HISTÓRICO · JOGO #{gid}</b> ({len(sessions)})", ""]
            for s in sessions:
                result = s.result or {}
                rname = result.get("reward_name") if isinstance(result, dict) else "—"
                ramount = result.get("reward_amount") if isinstance(result, dict) else 0
                try:
                    ramount_f = float(ramount or 0)
                except Exception:
                    ramount_f = 0.0
                emoji = "🎉" if ramount_f > 0 else "😔"
                lines.append(
                    f"{emoji} <code>{s.user_id}</code> · "
                    f"{truncate(str(rname), 20)} · "
                    f"{fmt_brl(ramount_f)} · "
                    f"{fmt_dt(s.created_at, with_time=False)}"
                )
            text = "\n".join(lines)

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=f"{CB_VIEW}:{gid}")]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_history falhou: {exc}")


# ============================================================
# EXCLUIR
# ============================================================
@router.callback_query(F.data.startswith(f"{CB_DELETE}:"))
async def on_delete(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        parts = callback.data.split(":")
        if len(parts) >= 5 and parts[4] == "yes":
            return
        try:
            gid = int(parts[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        g = await _load_game(gid)
        if g is None:
            await toast_err(callback, "❌ Jogo não encontrado.")
            return

        text = (
            f"🗑️ <b>EXCLUIR JOGO</b>\n\n"
            f"🎮 <b>{g.name}</b> (#{g.id})\n\n"
            "Isso remove o jogo e o histórico de partidas.\n"
            "Tem certeza?"
        )
        confirm_kb = build_confirm_keyboard(
            confirm_callback=f"{CB_DELETE_YES}:{gid}",
            cancel_callback=f"{CB_VIEW}:{gid}",
            confirm_text="🗑️ Sim, excluir",
            cancel_text="❌ Cancelar",
        )
        await edit_or_send(callback, text=text, reply_markup=confirm_kb)
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_delete falhou: {exc}")


@router.callback_query(F.data.startswith(f"{CB_DELETE_YES}:"))
async def on_delete_confirm(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return
        try:
            gid = int(callback.data.split(":")[-1])
        except Exception:
            await toast_err(callback, "❌ ID inválido.")
            return

        try:
            async with session_scope() as session:
                # Remove sessões
                await session.execute(
                    GameSession.__table__.delete().where(
                        GameSession.game_id == gid
                    )
                )
                # Remove jogo
                await session.execute(
                    Game.__table__.delete().where(Game.id == gid)
                )
        except Exception as exc:
            logger.exception(f"delete game falhou: {exc}")
            await toast_err(callback, "❌ Erro ao excluir.")
            return

        await log_action(
            admin_id=admin_id,
            action="game.deleted",
            category="admin",
            details={"game_id": gid},
        )

        await render_list(callback, page=1)
        await toast_ok(callback, "🗑️ Excluído.")
    except Exception as exc:
        logger.exception(f"games on_delete_confirm falhou: {exc}")


# ============================================================
# RELATÓRIO
# ============================================================
@router.callback_query(F.data == CB_REPORT)
async def on_report(callback: CallbackQuery) -> None:
    try:
        admin_id = callback.from_user.id
        if admin_id not in settings.admin_ids_list:
            await answer(callback, "⛔ Acesso negado.", alert=True)
            return

        try:
            async with session_scope() as session:
                total_games = int(
                    (await session.execute(select(func.count(Game.id)))).scalar() or 0
                )
                ativos = int(
                    (
                        await session.execute(
                            select(func.count(Game.id)).where(Game.active == True)  # noqa: E712
                        )
                    ).scalar()
                    or 0
                )
                total_sessions = int(
                    (await session.execute(select(func.count(GameSession.id)))).scalar() or 0
                )
                total_arrecadado = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(GameSession.cost), 0))
                        )
                    ).scalar()
                    or 0
                )
                total_premios = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(GameSession.reward), 0))
                        )
                    ).scalar()
                    or 0
                )
        except Exception:
            total_games = ativos = total_sessions = 0
            total_arrecadado = total_premios = 0.0

        lucro = round(total_arrecadado - total_premios, 2)

        text = (
            "📊 <b>RELATÓRIO DE JOGOS</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎮 Total cadastrados: <b>{fmt_num(total_games)}</b>\n"
            f"🟢 Ativos: <b>{fmt_num(ativos)}</b>\n"
            f"🎯 Partidas totais: <b>{fmt_num(total_sessions)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Arrecadado: <b>{fmt_brl(total_arrecadado)}</b>\n"
            f"🎁 Prêmios distribuídos: <b>{fmt_brl(total_premios)}</b>\n"
            f"📈 Lucro líquido: <b>{fmt_brl(lucro)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        await edit_or_send(
            callback,
            text=text,
            reply_markup=kb(
                [[InlineKeyboardButton(text="🔙 Voltar", callback_data=CB_ROOT)]]
            ),
        )
        await answer(callback)
    except Exception as exc:
        logger.exception(f"games on_report falhou: {exc}")


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "router",
    "render_list",
    "on_root",
    "on_page",
    "on_new",
    "on_games_text_input",
    "on_view",
    "on_set_field",
    "on_toggle",
    "on_limits",
    "on_limit_set",
    "on_rewards",
    "on_reward_edit",
    "on_reward_set",
    "on_reward_del",
    "on_reward_add",
    "on_history",
    "on_delete",
    "on_delete_confirm",
    "on_report",
    "CB_ROOT",
    "CB_PAGE",
    "CB_NEW",
    "CB_VIEW",
    "CB_SET",
    "CB_TOGGLE",
    "CB_DELETE",
    "CB_DELETE_YES",
    "CB_REWARDS",
    "CB_REWARD_ADD",
    "CB_REWARD_DEL",
    "CB_REWARD_WEIGHT",
    "CB_REWARD_AMOUNT",
    "CB_REWARD_NAME",
    "CB_LIMITS",
    "CB_HISTORY",
    "CB_REPORT",
    "TEXT_FIELDS",
    "TOGGLE_FIELDS",
    "LIMIT_FIELDS",
]
