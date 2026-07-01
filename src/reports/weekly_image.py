"""Genera una imagen de reporte semanal (estilo app financiera oscura) con
Pillow: total gastado en Bs de la semana + donut chart por categoría.

Inspirado en apps de finanzas con fondo oscuro, texto blanco, segmentos de
color en el donut y acentos amarillos.
"""
import io
import math
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 900, 1100
BG_COLOR = (13, 17, 28)          # navy/negro oscuro
CARD_COLOR = (20, 25, 38)
TEXT_WHITE = (245, 245, 245)
TEXT_GRAY = (150, 155, 168)
ACCENT_YELLOW = (247, 197, 72)

PALETTE = [
    (247, 197, 72),   # amarillo (acento)
    (99, 179, 237),   # azul
    (240, 113, 103),  # rojo/coral
    (108, 214, 160),  # verde
    (183, 148, 246),  # morado
    (250, 160, 108),  # naranja
    (110, 220, 220),  # cian
    (230, 130, 200),  # rosa
]


def _load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _week_range(reference: datetime = None) -> tuple:
    """Retorna (lunes, domingo) de la semana que acaba de terminar (o la
    actual, si `reference` cae en domingo se usa esa semana)."""
    reference = reference or datetime.now()
    # weekday(): 0=lunes ... 6=domingo
    monday = reference - timedelta(days=reference.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def render_weekly_report(summary: dict, moneda: str = "Bs", reference: datetime = None) -> bytes:
    """
    Renderiza el reporte semanal como imagen PNG.

    Args:
        summary: dict con "total_gastos" y "categorias" (lista de
            {"categoria", "total", "porcentaje"}), típicamente resultado de
            db.get_summary(fecha_desde, fecha_hasta, moneda=moneda)
        moneda: moneda del reporte (para el label, ej "Bs")
        reference: fecha de referencia para calcular el rango lunes-domingo

    Returns:
        Bytes de la imagen PNG.
    """
    monday, sunday = _week_range(reference)

    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_small = _load_font(28)
    font_label = _load_font(34, bold=True)
    font_total = _load_font(64, bold=True)
    font_tab = _load_font(30, bold=True)
    font_cat = _load_font(26)
    font_cat_val = _load_font(26, bold=True)

    # --- Header: tabs ---
    draw.text((60, 50), "GASTOS", font=font_tab, fill=ACCENT_YELLOW)
    date_range_str = f"{monday.strftime('%d %b')} - {sunday.strftime('%d %b %Y')}"
    draw.text((60, 100), date_range_str, font=font_small, fill=TEXT_GRAY)

    # --- Total ---
    draw.text((60, 170), "Total gastado", font=font_label, fill=TEXT_GRAY)
    total = summary.get("total_gastos", 0)
    draw.text((60, 210), f"{moneda} {total:,.2f}", font=font_total, fill=TEXT_WHITE)

    categorias = summary.get("categorias", [])

    # --- Donut chart ---
    cx, cy, r = WIDTH // 2, 520, 200
    thickness = 55

    if not categorias or total <= 0:
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=CARD_COLOR, width=thickness)
        msg = "No hubo gastos"
        msg2 = "esta semana"
        bbox = draw.textbbox((0, 0), msg, font=font_label)
        w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, cy - 25), msg, font=font_label, fill=TEXT_GRAY)
        bbox2 = draw.textbbox((0, 0), msg2, font=font_label)
        w2 = bbox2[2] - bbox2[0]
        draw.text((cx - w2 / 2, cy + 15), msg2, font=font_label, fill=TEXT_GRAY)
    else:
        start_angle = -90.0
        for i, cat in enumerate(categorias):
            color = PALETTE[i % len(PALETTE)]
            sweep = (cat["total"] / total) * 360.0 if total > 0 else 0
            end_angle = start_angle + sweep
            draw.arc(
                (cx - r, cy - r, cx + r, cy + r),
                start=start_angle, end=end_angle, fill=color, width=thickness
            )
            start_angle = end_angle

        # Centro: top categoría
        top_cat = categorias[0]
        bbox = draw.textbbox((0, 0), top_cat["categoria"], font=font_cat_val)
        w = bbox[2] - bbox[0]
        # Si el nombre es muy largo, lo recorta
        label = top_cat["categoria"]
        if w > r * 1.6:
            label = label[:18] + "…"
            bbox = draw.textbbox((0, 0), label, font=font_cat_val)
            w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, cy - 20), label, font=font_cat_val, fill=TEXT_WHITE)
        pct_str = f"{top_cat['porcentaje']}%"
        bbox2 = draw.textbbox((0, 0), pct_str, font=font_small)
        w2 = bbox2[2] - bbox2[0]
        draw.text((cx - w2 / 2, cy + 15), pct_str, font=font_small, fill=ACCENT_YELLOW)

    # --- Leyenda por categoría ---
    legend_y = cy + r + 70
    draw.text((60, legend_y), "Por categoría", font=font_label, fill=TEXT_WHITE)
    legend_y += 55

    for i, cat in enumerate(categorias[:8]):
        color = PALETTE[i % len(PALETTE)]
        draw.ellipse((60, legend_y + 6, 84, legend_y + 30), fill=color)
        draw.text((100, legend_y), cat["categoria"], font=font_cat, fill=TEXT_WHITE)
        val_str = f"{moneda} {cat['total']:,.2f} ({cat['porcentaje']}%)"
        bbox = draw.textbbox((0, 0), val_str, font=font_cat_val)
        w = bbox[2] - bbox[0]
        draw.text((WIDTH - 60 - w, legend_y), val_str, font=font_cat_val, fill=TEXT_GRAY)
        legend_y += 45
        if legend_y > HEIGHT - 60:
            break

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
