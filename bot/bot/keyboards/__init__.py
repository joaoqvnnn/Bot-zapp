"""
Larizinha Store — Teclados (agregador)
Centraliza a exportação dos builders de teclado inline e reply.

Regra-mãe: TODO botão é editável pelo painel admin e renderizado em runtime
pelo `button_engine`. Este pacote apenas:
  - fornece builders utilitários (inline / reply / force_reply);
  - reexporta as funções do motor de botões para uso padronizado.
"""
from __future__ import annotations

# ============================================================
# 📦 BUILDER INLINE
# ============================================================
from bot.keyboards.inline import (
    build_inline_keyboard,
    build_inline_row,
    build_pagination,
    build_url_button,
    build_webapp_button,
    build_copy_button,
)

# ============================================================
# 📦 BUILDER REPLY
# ============================================================
from bot.keyboards.reply import (
    build_force_reply,
    build_reply_keyboard,
    build_reply_keyboard_remove,
    build_reply_row,
    build_one_time_reply,
)

# ============================================================
# 📦 BUTTON ENGINE (renderiza botões vindos do banco)
# ============================================================
from bot.keyboards.button_engine import (
    ButtonEngine,
    button_engine,
    render_inline,
    render_reply,
)

# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    # Inline builders
    "build_inline_keyboard",
    "build_inline_row",
    "build_pagination",
    "build_url_button",
    "build_webapp_button",
    "build_copy_button",
    # Reply builders
    "build_force_reply",
    "build_reply_keyboard",
    "build_reply_keyboard_remove",
    "build_reply_row",
    "build_one_time_reply",
    # Button engine
    "ButtonEngine",
    "button_engine",
    "render_inline",
    "render_reply",
]
