"""Genera una imagen del saldo actual (estilo app financiera oscura, mismo
look que weekly_image.py) con Pillow: barras horizontales por billetera.

Usada por /saldo para mostrar de un vistazo cómo está repartido el dinero
entre BDV, Binance y Efectivo (Bs/USD/COP), en vez de solo texto.
"""
import io
from datetime import datetime
from PIL import Image, ImageDraw

from src.reports.weekly_image import (
    WIDTH, BG_COLOR, CARD_COLOR, TEXT_WHITE, TEXT_GRAY, ACCENT_YELLOW, PALETTE,
    _load_font,
)

HEIGHT = 700


def render_balance_report(items: list, total_usd: float = None, reference: datetime = None) -> bytes:
    """
    Renderiza el saldo por billetera como imagen PNG.

    Args:
        items: lista de {"label": str, "value_str": str, "usd_equiv": float}
            una entrada por billetera con saldo (ej. "BDV (Bs)", "12.345,67",
            123.45). `usd_equiv` se usa solo para el largo relativo de la
            barra -- no se muestra directo si no se quiere.
        total_usd: total aproximado en USD (si se conoce), se muestra arriba
            a modo de titular, igual que el "Total gastado" del resumen.
        reference: fecha de referencia para el subtítulo (hoy por defecto)

    Returns:
        Bytes de la imagen PNG.
    """
    reference = reference or datetime.now()
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_small = _load_font(28)
    font_label = _load_font(34, bold=True)
    font_total = _load_font(60, bold=True)
    font_tab = _load_font(30, bold=True)
    font_item = _load_font(28, bold=True)
    font_item_val = _load_font(26)

    # --- Header ---
    draw.text((60, 50), "SALDO", font=font_tab, fill=ACCENT_YELLOW)
    draw.text((60, 100), reference.strftime("%d %b %Y"), font=font_small, fill=TEXT_GRAY)

    if total_usd is not None:
        draw.text((60, 170), "Total aprox.", font=font_label, fill=TEXT_GRAY)
        draw.text((60, 210), f"$ {total_usd:,.2f}", font=font_total, fill=TEXT_WHITE)
        bars_top = 320
    else:
        bars_top = 190

    # --- Barras por billetera ---
    max_equiv = max((it.get("usd_equiv") or 0 for it in items), default=0) or 1
    bar_x, bar_w_max = 60, WIDTH - 120
    bar_h, gap = 46, 30
    y = bars_top

    if not items:
        msg = "Todavía no hay saldo registrado"
        draw.text((60, y + 30), msg, font=font_label, fill=TEXT_GRAY)

    for i, item in enumerate(items):
        color = PALETTE[i % len(PALETTE)]
        draw.text((bar_x, y), item["label"], font=font_item, fill=TEXT_WHITE)
        val_str = item["value_str"]
        bbox = draw.textbbox((0, 0), val_str, font=font_item_val)
        w = bbox[2] - bbox[0]
        draw.text((WIDTH - 60 - w, y + 4), val_str, font=font_item_val, fill=TEXT_GRAY)
        y += 38

        equiv = item.get("usd_equiv") or 0
        frac = max(equiv / max_equiv, 0.03) if max_equiv else 0.03
        draw.rounded_rectangle((bar_x, y, bar_x + bar_w_max, y + bar_h), radius=12, fill=CARD_COLOR)
        draw.rounded_rectangle((bar_x, y, bar_x + int(bar_w_max * frac), y + bar_h), radius=12, fill=color)
        y += bar_h + gap

        if y > HEIGHT - 60:
            break

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
