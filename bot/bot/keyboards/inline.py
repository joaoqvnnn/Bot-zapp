"""
Larizinha Store — Inline Keyboards & Paginação
Regras invioláveis aplicadas aqui:
- Paginação do catálogo:
    * Página 1:        [Próxima Página ➡️]
    * Páginas do meio: [🔙 Voltar] [⬅️ Anterior] [Próxima Página ➡️]
    * Última página:   [🔙 Voltar] [⬅️ Anterior]
    * 🔙 Voltar  = volta para a PÁGINA 1
    * ⬅️ Anterior = página imediatamente anterior
- Botões de URL, WebApp, Copy (silencioso) suportados.
- Nada de texto acumulando aqui — só build.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Union

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)


# ============================================================
# 🔘 LINHA / KEYBOARD
# ============================================================
InlineRowItem = Union[InlineKeyboardButton, Sequence[InlineKeyboardButton]]


def build_inline_row(
    items: Sequence[InlineKeyboardButton],
) -> List[InlineKeyboardButton]:
    """Retorna a própria lista (helper semântico)."""
    return list(items)


def build_inline_keyboard(
    rows: Sequence[Sequence[InlineKeyboardButton]],
    *,
    resize: bool = True,  # ignorado em inline, mas mantido por compat
) -> InlineKeyboardMarkup:
    """
    Constrói um InlineKeyboardMarkup a partir de uma matriz de botões.
    """
    keyboard: List[List[InlineKeyboardButton]] = []
    for row in rows:
        if not row:
            continue
        keyboard.append(list(row))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ============================================================
# 🔗 BOTÕES ESPECIAIS
# ============================================================
def build_url_button(text: str, url: str) -> InlineKeyboardButton:
    """Botão de URL (abre link externo)."""
    return InlineKeyboardButton(text=text, url=url)


def build_webapp_button(text: str, url: str) -> InlineKeyboardButton:
    """Botão que abre WebApp dentro do Telegram."""
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))


def build_copy_button(
    text: str,
    copy_text: str,
    *,
    toast: Optional[str] = None,
) -> InlineKeyboardButton:
    """
    Botão que copia um texto (silencioso).
    Requer Bot API 7.11+ (campo copy_text em InlineKeyboardButton).
    """
    try:
        return InlineKeyboardButton(
            text=text,
            copy_text=CopyTextButton(text=copy_text),
        )
    except Exception:
        # Fallback para versões antigas: usa callback_data especial "copy:<hash>"
        # (o handler copia e responde silenciosamente).
        import hashlib

        digest = hashlib.sha1(copy_text.encode("utf-8")).hexdigest()[:16]
        return InlineKeyboardButton(
            text=text,
            callback_data=f"copy:{digest}",
        )


def build_callback_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """Botão de callback (ação interna)."""
    return InlineKeyboardButton(text=text, callback_data=callback_data)


# ============================================================
# 📄 PAGINAÇÃO DIM (regra exata)
# ============================================================
DEFAULT_NEXT_TEXT = "Próxima Página ➡️"
DEFAULT_PREV_TEXT = "⬅️ Anterior"
DEFAULT_BACK_TEXT = "🔙 Voltar"


def build_pagination(
    *,
    current_page: int,
    total_pages: int,
    callback_base: str,
    back_callback: Optional[str] = None,
    back_text: str = DEFAULT_BACK_TEXT,
    prev_text: str = DEFAULT_PREV_TEXT,
    next_text: str = DEFAULT_NEXT_TEXT,
) -> List[InlineKeyboardButton]:
    """
    Constrói a linha (ou linhas) de paginação EXATAMENTE como o DIM especifica:

      - Página 1 (current == 1):
          [Próxima Página ➡️]

      - Páginas do meio (1 < current < total):
          [🔙 Voltar] [⬅️ Anterior] [Próxima Página ➡️]

      - Última página (current == total):
          [🔙 Voltar] [⬅️ Anterior]

    Args:
        current_page: página atual (1-indexed)
        total_pages: total de páginas
        callback_base: prefixo usado nos callbacks.
            Ex.: "catalog:page" → gera "catalog:page:1", "catalog:page:2", ...
        back_callback: callback do botão "🔙 Voltar".
            Se None, usa `<callback_base>:1` (voltar para a página 1).
        back_text / prev_text / next_text: textos (editáveis pelo painel).

    Returns:
        Lista de InlineKeyboardButton (uma linha só).
    """
    # Garantias de sanidade
    current_page = max(1, int(current_page))
    total_pages = max(1, int(total_pages))
    if current_page > total_pages:
        current_page = total_pages

    buttons: List[InlineKeyboardButton] = []

    is_first = current_page == 1
    is_last = current_page == total_pages
    is_middle = not is_first and not is_last
    only_one = total_pages == 1

    # ---------- Único página → só Voltar (se quiser) ----------
    if only_one:
        if back_callback:
            buttons.append(
                InlineKeyboardButton(text=back_text, callback_data=back_callback)
            )
        return buttons

    # ---------- Primeira página ----------
    if is_first:
        buttons.append(
            InlineKeyboardButton(
                text=next_text,
                callback_data=f"{callback_base}:{current_page + 1}",
            )
        )
        return buttons

    # ---------- Páginas do meio ----------
    if is_middle:
        # 🔙 Voltar (sempre leva à página 1)
        if back_callback is None:
            back_callback = f"{callback_base}:1"
        buttons.append(
            InlineKeyboardButton(text=back_text, callback_data=back_callback)
        )
        # ⬅️ Anterior
        buttons.append(
            InlineKeyboardButton(
                text=prev_text,
                callback_data=f"{callback_base}:{current_page - 1}",
            )
        )
        # Próxima Página ➡️
        buttons.append(
            InlineKeyboardButton(
                text=next_text,
                callback_data=f"{callback_base}:{current_page + 1}",
            )
        )
        return buttons

    # ---------- Última página ----------
    if is_last:
        if back_callback is None:
            back_callback = f"{callback_base}:1"
        buttons.append(
            InlineKeyboardButton(text=back_text, callback_data=back_callback)
        )
        buttons.append(
            InlineKeyboardButton(
                text=prev_text,
                callback_data=f"{callback_base}:{current_page - 1}",
            )
        )
        return buttons

    # Fallback (não deve acontecer)
    return buttons


def build_pagination_keyboard(
    *,
    current_page: int,
    total_pages: int,
    callback_base: str,
    back_callback: Optional[str] = None,
    extra_rows_above: Optional[Sequence[Sequence[InlineKeyboardButton]]] = None,
    extra_rows_below: Optional[Sequence[Sequence[InlineKeyboardButton]]] = None,
    back_text: str = DEFAULT_BACK_TEXT,
    prev_text: str = DEFAULT_PREV_TEXT,
    next_text: str = DEFAULT_NEXT_TEXT,
) -> InlineKeyboardMarkup:
    """
    Monta o teclado completo do catálogo:
        [ produtos / categorias (extra_rows_above) ]
        [ linha de paginação ]
        [ rodapé extra (extra_rows_below) ]
    """
    pag_row = build_pagination(
        current_page=current_page,
        total_pages=total_pages,
        callback_base=callback_base,
        back_callback=back_callback,
        back_text=back_text,
        prev_text=prev_text,
        next_text=next_text,
    )

    rows: List[List[InlineKeyboardButton]] = []
    if extra_rows_above:
        for r in extra_rows_above:
            rows.append(list(r))
    if pag_row:
        rows.append(pag_row)
    if extra_rows_below:
        for r in extra_rows_below:
            rows.append(list(r))

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# 🧩 HELPERS DE GRID (produtos, categorias, etc.)
# ============================================================
def chunk_buttons(
    buttons: Sequence[InlineKeyboardButton],
    per_row: int = 2,
) -> List[List[InlineKeyboardButton]]:
    """Distribui botões em linhas de N."""
    rows: List[List[InlineKeyboardButton]] = []
    per_row = max(1, int(per_row))
    for i in range(0, len(buttons), per_row):
        rows.append(list(buttons[i : i + per_row]))
    return rows


def build_grid_keyboard(
    buttons: Iterable[InlineKeyboardButton],
    *,
    per_row: int = 2,
) -> InlineKeyboardMarkup:
    """Constrói um teclado com N botões por linha."""
    return InlineKeyboardMarkup(inline_keyboard=chunk_buttons(list(buttons), per_row))


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    # Básico
    "build_inline_row",
    "build_inline_keyboard",
    # Especiais
    "build_url_button",
    "build_webapp_button",
    "build_copy_button",
    "build_callback_button",
    # Paginação DIM
    "build_pagination",
    "build_pagination_keyboard",
    # Grid
    "chunk_buttons",
    "build_grid_keyboard",
    # Constantes
    "DEFAULT_NEXT_TEXT",
    "DEFAULT_PREV_TEXT",
    "DEFAULT_BACK_TEXT",
]
