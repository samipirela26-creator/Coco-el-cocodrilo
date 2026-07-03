"""Helpers de UI compartidos: responder tanto a mensajes normales como a
botones del /menu, y construir los teclados inline del bot (menú, deshacer,
navegación de mes, selector de categoría)."""
import io
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update


async def _reply(update: Update, text: str, **kwargs) -> None:
    """Responde tanto si el update viene de un mensaje normal como de un botón
    del menú (callback_query) -- así los comandos se pueden reusar tal cual
    desde ambos flujos."""
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text(text, **kwargs)


async def _reply_photo(update: Update, photo_bytes: bytes, caption: str = None, **kwargs) -> None:
    """Igual que `_reply` pero para enviar una imagen (mismo soporte para
    mensaje normal o botón de callback)."""
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_photo(photo=io.BytesIO(photo_bytes), caption=caption, **kwargs)


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Saldo", callback_data="coco_menu:saldo"),
         InlineKeyboardButton("Resumen", callback_data="coco_menu:resumen")],
        [InlineKeyboardButton("Cambio", callback_data="coco_menu:cambio"),
         InlineKeyboardButton("Ayuda", callback_data="coco_menu:ayuda")],
    ])


def _saldo_confirm_keyboard() -> InlineKeyboardMarkup:
    """Botones para confirmar/cancelar un ajuste de saldo (foto o texto/voz)
    ANTES de sobrescribir la billetera -- ver format_ajuste_preview_message
    en formatters.py y saldo_callback en handlers.py."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí, actualizar", callback_data="coco_saldo:si"),
        InlineKeyboardButton("❌ No, cancelar", callback_data="coco_saldo:no"),
    ]])


def _deshacer_keyboard() -> InlineKeyboardMarkup:
    """Botón bajo cada confirmación de gasto/ingreso para deshacer sin tener
    que escribir /deshacer. Siempre deshace el ÚLTIMO movimiento del perfil
    (mismo alcance que el comando)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Deshacer", callback_data="coco_deshacer:last")],
    ])


def _resumen_nav_keyboard(prev_year: int, prev_month: int, next_year: int, next_month: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Mes anterior", callback_data=f"coco_resumen:{prev_year:04d}-{prev_month:02d}"),
         InlineKeyboardButton("Mes siguiente ▶", callback_data=f"coco_resumen:{next_year:04d}-{next_month:02d}")],
    ])


def _category_keyboard(categories: list) -> InlineKeyboardMarkup:
    """Botones de categoría para cuando el LLM no logró decidir una (ver
    handlers._offer_category_picker). Dos por fila + un botón de cancelar."""
    rows = []
    row = []
    for cat in categories:
        row.append(InlineKeyboardButton(cat, callback_data=f"coco_cat:{cat}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Cancelar", callback_data="coco_cat:__cancel__")])
    return InlineKeyboardMarkup(rows)
