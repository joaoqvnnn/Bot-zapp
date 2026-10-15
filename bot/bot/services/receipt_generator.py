"""
Larizinha Store — Receipt Generator
Geração de comprovantes de saque em imagem PNG (Pillow) + envio como spoiler.

Regras invioláveis:
- Comprovantes de saque são enviados SEMPRE como imagem spoiler (has_spoiler=True).
- Imagem contém: valor, chave PIX, txid, data, nome da loja, marca d'água.
- Não acumula mensagens — o handler decide quando editar/enviar.
- Fonte DejaVu (já incluída no Dockerfile).
- Layout 100% determinístico (dados reais do banco).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from bot.config import settings
from bot.logger import logger


# ============================================================
# 🎨 CONSTANTES DE LAYOUT
# ============================================================
IMG_WIDTH = 900
IMG_HEIGHT = 620
PADDING = 60

BG_COLOR = (15, 17, 21)           # fundo escuro
CARD_COLOR = (28, 32, 40)         # cartão interno
ACCENT = (34, 197, 94)            # verde (sucesso)
ACCENT_ALT = (59, 130, 246)       # azul (info)
TEXT_PRIMARY = (240, 240, 240)
TEXT_SECONDARY = (170, 175, 185)
TEXT_MUTED = (120, 125, 135)
WATERMARK_COLOR = (60, 65, 75)
DIVIDER = (55, 60, 70)

FONT_REGULAR = "DejaVuSans.ttf"
FONT_BOLD = "DejaVuSans-Bold.ttf"
FONT_MONO = "DejaVuSansMono.ttf"

DEFAULT_LOGO_TEXT = "LARIZINHA STORE"


# ============================================================
# 🧱 DADOS DE ENTRADA
# ============================================================
@dataclass
class ReceiptData:
    """Dados usados no comprovante."""

    store_name: str
    amount: float
    pix_key: str
    pix_key_type: Optional[str]
    txid: str
    user_id: int
    user_name: Optional[str]
    created_at: Optional[datetime] = None
    status: str = "PAGO"
    note: Optional[str] = None

    def resolved_date(self) -> datetime:
        return self.created_at or datetime.now(timezone.utc)


# ============================================================
# 🔤 HELPERS DE FONTE
# ============================================================
def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Carrega a fonte com fallback seguro."""
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        try:
            # caminho padrão em Debian/Alpine
            return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
        except Exception:
            try:
                return ImageFont.truetype(f"/usr/share/fonts/{name}", size)
            except Exception:
                return ImageFont.load_default()


def _format_brl(value: float) -> str:
    """Formata valores em BRL: R$ 1.234,56"""
    s = f"{value:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _format_dt(dt: datetime) -> str:
    """Formata data/hora no padrão brasileiro."""
    return dt.astimezone().strftime("%d/%m/%Y às %H:%M:%S")


def _mask_pix_key(key: str, key_type: Optional[str]) -> str:
    """Mascara parcialmente a chave PIX por segurança."""
    if not key:
        return "—"
    if key_type and key_type.lower() in ("cpf", "cnpj"):
        digits = "".join(c for c in key if c.isdigit())
        if len(digits) >= 11:
            return f"***.{digits[3:6]}.{digits[6:9]}-**"
    if "@" in key:
        user, _, domain = key.partition("@")
        if len(user) > 2:
            user = user[0] + "*" * (len(user) - 2) + user[-1]
        return f"{user}@{domain}"
    # telefone ou aleatória
    if len(key) > 6:
        return key[:3] + "*" * (len(key) - 6) + key[-3:]
    return key


# ============================================================
# 🧠 GERADOR
# ============================================================
class ReceiptGenerator:
    """
    Gera o comprovante como bytes PNG.

    Uso:
        png = await receipt_generator.generate(ReceiptData(...))
        await bot.send_photo(chat_id, photo=png, has_spoiler=True, caption="...")
    """

    def __init__(self, logo_text: Optional[str] = None) -> None:
        self.logo_text = (logo_text or DEFAULT_LOGO_TEXT).upper()
        self._font_bold_28 = _load_font(FONT_BOLD, 28)
        self._font_bold_22 = _load_font(FONT_BOLD, 22)
        self._font_bold_18 = _load_font(FONT_BOLD, 18)
        self._font_regular_18 = _load_font(FONT_REGULAR, 18)
        self._font_regular_16 = _load_font(FONT_REGULAR, 16)
        self._font_regular_14 = _load_font(FONT_REGULAR, 14)
        self._font_mono_18 = _load_font(FONT_MONO, 18)
        self._font_mono_16 = _load_font(FONT_MONO, 16)
        self._font_big_amount = _load_font(FONT_BOLD, 52)
        self._font_watermark = _load_font(FONT_BOLD, 72)

    # --------------------------------------------------------
    async def generate(self, data: ReceiptData) -> Optional[bytes]:
        """
        Gera o comprovante e devolve como bytes PNG.
        Retorna None em caso de falha (o handler decide o fallback).
        """
        try:
            image = self._render(data)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            buffer.seek(0)
            return buffer.getvalue()
        except Exception as exc:
            logger.exception(f"Falha ao gerar comprovante: {exc}")
            return None

    # --------------------------------------------------------
    async def save(
        self,
        data: ReceiptData,
        path: str,
    ) -> Optional[str]:
        """Gera e salva em disco. Retorna o caminho ou None."""
        png = await self.generate(data)
        if not png:
            return None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                f.write(png)
            return path
        except Exception as exc:
            logger.exception(f"Falha ao salvar comprovante: {exc}")
            return None

    # --------------------------------------------------------
    # 🎨 RENDER
    # --------------------------------------------------------
    def _render(self, data: ReceiptData) -> Image.Image:
        img = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # ------ Marca d'água diagonal ------
        self._draw_watermark(img, draw)

        # ------ Cartão central ------
        card_x0 = PADDING
        card_y0 = PADDING
        card_x1 = IMG_WIDTH - PADDING
        card_y1 = IMG_HEIGHT - PADDING
        draw.rounded_rectangle(
            [card_x0, card_y0, card_x1, card_y1],
            radius=24,
            fill=CARD_COLOR,
            outline=DIVIDER,
            width=2,
        )

        # ------ Cabeçalho ------
        self._draw_header(draw, card_x0, card_y0, card_x1, data)

        # ------ Valor em destaque ------
        self._draw_amount(draw, card_x0, card_y0, card_x1, data)

        # ------ Divisor ------
        div_y = card_y0 + 250
        draw.line([card_x0 + 30, div_y, card_x1 - 30, div_y], fill=DIVIDER, width=1)

        # ------ Detalhes ------
        self._draw_details(draw, card_x0, div_y, card_x1, data)

        # ------ Rodapé ------
        self._draw_footer(draw, card_x1, card_y1, data)

        return img

    # --------------------------------------------------------
    def _draw_watermark(self, img: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        """Marca d'água rotacionada no fundo."""
        try:
            wm_text = self.logo_text
            wm_font = self._font_watermark

            # Cria uma camada separada e rotaciona
            bbox = draw.textbbox((0, 0), wm_text, font=wm_font)
            wm_w = bbox[2] - bbox[0]
            wm_h = bbox[3] - bbox[1]

            txt_layer = Image.new("RGBA", (wm_w + 40, wm_h + 40), (0, 0, 0, 0))
            txt_draw = ImageDraw.Draw(txt_layer)
            txt_draw.text(
                (20, 20),
                wm_text,
                font=wm_font,
                fill=(*WATERMARK_COLOR, 90),
            )
            rotated = txt_layer.rotate(25, expand=True)

            # Cola em vários pontos (padrão repetido)
            for y in range(-100, IMG_HEIGHT, 220):
                for x in range(-200, IMG_WIDTH, 480):
                    img.paste(
                        rotated,
                        (x, y),
                        rotated,
                    )
        except Exception as exc:
            logger.debug(f"Watermark falhou: {exc}")

    # --------------------------------------------------------
    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        y0: int,
        x1: int,
        data: ReceiptData,
    ) -> None:
        # Ícone ✅
        icon_box = [x0 + 30, y0 + 30, x0 + 90, y0 + 90]
        draw.rounded_rectangle(icon_box, radius=14, fill=ACCENT)
        # Check dentro
        cx0, cy0, cx1, cy1 = icon_box
        mid_x = (cx0 + cx1) // 2
        mid_y = (cy0 + cy1) // 2
        draw.line(
            [(cx0 + 14, mid_y), (mid_x - 2, cy1 - 14), (cx1 - 12, cy0 + 16)],
            fill=(255, 255, 255),
            width=5,
        )

        # Título
        draw.text(
            (x0 + 110, y0 + 36),
            "COMPROVANTE DE SAQUE",
            font=self._font_bold_22,
            fill=TEXT_PRIMARY,
        )
        draw.text(
            (x0 + 110, y0 + 66),
            f"Status: {data.status.upper()}",
            font=self._font_regular_16,
            fill=ACCENT,
        )

        # Loja no canto direito
        try:
            store_text = data.store_name.upper()
            bbox = draw.textbbox((0, 0), store_text, font=self._font_bold_18)
            w = bbox[2] - bbox[0]
            draw.text(
                (x1 - 30 - w, y0 + 42),
                store_text,
                font=self._font_bold_18,
                fill=TEXT_SECONDARY,
            )
        except Exception:
            pass

    # --------------------------------------------------------
    def _draw_amount(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        y0: int,
        x1: int,
        data: ReceiptData,
    ) -> None:
        label = "VALOR PAGO"
        amount_text = _format_brl(data.amount)

        # Label
        lbl_bbox = draw.textbbox((0, 0), label, font=self._font_regular_14)
        lbl_w = lbl_bbox[2] - lbl_bbox[0]
        center_x = (x0 + x1) // 2
        draw.text(
            (center_x - lbl_w // 2, y0 + 120),
            label,
            font=self._font_regular_14,
            fill=TEXT_MUTED,
        )

        # Valor grande
        bbox = draw.textbbox((0, 0), amount_text, font=self._font_big_amount)
        w = bbox[2] - bbox[0]
        draw.text(
            (center_x - w // 2, y0 + 145),
            amount_text,
            font=self._font_big_amount,
            fill=ACCENT,
        )

    # --------------------------------------------------------
    def _draw_details(
        self,
        draw: ImageDraw.ImageDraw,
        x0: int,
        y0: int,
        x1: int,
        data: ReceiptData,
    ) -> None:
        left = x0 + 40
        right = x1 - 40
        cursor_y = y0 + 30

        def row(label: str, value: str, mono: bool = False) -> None:
            nonlocal cursor_y
            draw.text(
                (left, cursor_y),
                label,
                font=self._font_regular_16,
                fill=TEXT_MUTED,
            )
            font = self._font_mono_16 if mono else self._font_regular_16
            # Alinha o valor à direita
            bbox = draw.textbbox((0, 0), value, font=font)
            w = bbox[2] - bbox[0]
            draw.text(
                (right - w, cursor_y),
                value,
                font=font,
                fill=TEXT_PRIMARY,
            )
            cursor_y += 34

        # ID do usuário
        row("ID do usuário:", str(data.user_id))

        # Nome
        if data.user_name:
            row("Nome:", data.user_name[:32])

        # Chave PIX (mascarada)
        row(
            "Chave PIX:",
            _mask_pix_key(data.pix_key, data.pix_key_type),
            mono=True,
        )

        # Tipo
        if data.pix_key_type:
            row("Tipo da chave:", str(data.pix_key_type).upper())

        # TXID
        row("ID da operação:", data.txid, mono=True)

        # Data
        row("Data:", _format_dt(data.resolved_date()))

        # Nota (opcional)
        if data.note:
            nonlocal_note = data.note.strip()
            draw.text(
                (left, cursor_y + 10),
                "Observação:",
                font=self._font_regular_16,
                fill=TEXT_MUTED,
            )
            draw.text(
                (left, cursor_y + 32),
                nonlocal_note[:80],
                font=self._font_regular_16,
                fill=TEXT_PRIMARY,
            )

    # --------------------------------------------------------
    def _draw_footer(
        self,
        draw: ImageDraw.ImageDraw,
        x1: int,
        y1: int,
        data: ReceiptData,
    ) -> None:
        footer_text = "Comprovante gerado automaticamente — Larizinha Store"
        try:
            bbox = draw.textbbox((0, 0), footer_text, font=self._font_regular_14)
            w = bbox[2] - bbox[0]
            draw.text(
                (x1 // 2 - w // 2 + PADDING // 2, y1 - 40),
                footer_text,
                font=self._font_regular_14,
                fill=TEXT_MUTED,
            )
        except Exception:
            pass


# ============================================================
# 📌 SINGLETON + ATALHOS
# ============================================================
receipt_generator = ReceiptGenerator()


async def generate_receipt(data: ReceiptData) -> Optional[bytes]:
    """Atalho: `await generate_receipt(ReceiptData(...))`."""
    return await receipt_generator.generate(data)


async def send_receipt_spoiler(
    bot,
    chat_id: int,
    data: ReceiptData,
    *,
    caption: Optional[str] = None,
) -> bool:
    """
    Gera + envia o comprovante como IMAGEM SPOILER.
    Regra inviolável do DIM: TODO comprovante de saque vai como spoiler.
    """
    png = await receipt_generator.generate(data)
    if not png:
        return False

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=png,
            caption=caption or "🧾 <b>Comprovante de saque</b>",
            parse_mode="HTML",
            has_spoiler=True,
        )
        return True
    except Exception as exc:
        logger.exception(f"Falha ao enviar comprovante spoiler: {exc}")
        return False


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "ReceiptGenerator",
    "ReceiptData",
    "receipt_generator",
    "generate_receipt",
    "send_receipt_spoiler",
]
