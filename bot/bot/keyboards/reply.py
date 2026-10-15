"""
Larizinha Store — Reply Keyboards & ForceReply
Regras invioláveis:
- Todo input abre teclado via ForceReply OU ReplyKeyboardMarkup
- Parâmetros obrigatórios: one_time_keyboard=True, resize_keyboard=True, selective=True
- Sempre usar selective=True (só afeta quem interagiu)
- Suporte a botões editáveis (texto vindo do painel admin)
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Union

from aiogram.types import (
    ForceReply,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


# ============================================================
# 🔘 BUILDERS BÁSICOS
# ============================================================
def build_reply_row(items: Sequence[str]) -> List[KeyboardButton]:
    """Constrói uma linha (lista) de KeyboardButton a partir de textos."""
    return [KeyboardButton(text=str(t)) for t in items]


def build_reply_keyboard(
    rows: Sequence[Sequence[str]],
    *,
    one_time: bool = True,
    resize: bool = True,
    selective: bool = True,
    placeholder: Optional[str] = None,
    persistent: bool = False,
) -> ReplyKeyboardMarkup:
    """
    Constrói um ReplyKeyboardMarkup.

    Args:
        rows: matriz de textos. Ex.: [["A", "B"], ["C"]]
        one_time: fecha o teclado após 1 uso (padrão: True)
        resize: adapta o tamanho dos botões (padrão: True)
        selective: aplica somente a usuários específicos (padrão: True)
        placeholder: texto do input (aparece na barra de digitação)
        persistent: mantém o teclado mesmo após o bot enviar msg

    Returns:
        ReplyKeyboardMarkup pronto para uso.
    """
    keyboard: List[List[KeyboardButton]] = []
    for row in rows:
        if not row:
            continue
        keyboard.append(build_reply_row(list(row)))

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        one_time_keyboard=one_time,
        resize_keyboard=resize,
        selective=selective,
        input_field_placeholder=placeholder,
        is_persistent=persistent,
    )


def build_one_time_reply(
    rows: Sequence[Sequence[str]],
    *,
    placeholder: Optional[str] = None,
) -> ReplyKeyboardMarkup:
    """
    Atalho: teclado one-time, resize e selective obrigatórios.
    Ideal para TODA entrada de input do usuário.
    """
    return build_reply_keyboard(
        rows,
        one_time=True,
        resize=True,
        selective=True,
        placeholder=placeholder,
    )


def build_reply_keyboard_remove(selective: bool = True) -> ReplyKeyboardRemove:
    """Remove o teclado reply do chat."""
    return ReplyKeyboardRemove(selective=selective)


# ============================================================
# ✏️ FORCE REPLY
# ============================================================
def build_force_reply(
    *,
    placeholder: Optional[str] = None,
    selective: bool = True,
) -> ForceReply:
    """
    Força o usuário a responder citando a mensagem do bot.
    Sempre selective=True por padrão.
    """
    return ForceReply(
        force_reply=True,
        selective=selective,
        input_field_placeholder=placeholder,
    )


# ============================================================
# 🧩 HELPERS ESPECÍFICOS DE FLUXO
# ============================================================
def build_cancel_reply(placeholder: str = "Digite /cancelar para sair") -> ReplyKeyboardMarkup:
    """
    Teclado padrão usado em TODOS os fluxos que aceitam input do usuário.
    Um único botão de cancelar + input placeholder.
    """
    return build_one_time_reply(
        rows=[["❌ Cancelar"]],
        placeholder=placeholder,
    )


def build_menu_reply(
    buttons: Iterable[str],
    *,
    per_row: int = 2,
    placeholder: Optional[str] = None,
) -> ReplyKeyboardMarkup:
    """
    Distribui uma lista de textos em linhas de `per_row` botões.
    """
    items = [str(b) for b in buttons]
    rows: List[List[str]] = []
    for i in range(0, len(items), per_row):
        rows.append(items[i : i + per_row])
    return build_one_time_reply(rows, placeholder=placeholder)


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "build_reply_row",
    "build_reply_keyboard",
    "build_one_time_reply",
    "build_reply_keyboard_remove",
    "build_force_reply",
    "build_cancel_reply",
    "build_menu_reply",
]
