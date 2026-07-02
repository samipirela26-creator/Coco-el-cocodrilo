"""Helpers de UI compartidos: responder tanto a mensajes normales como a
botones del /menu, y construir el teclado inline del menú."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update


async def _reply(update: Update, text: str, **kwargs) -> None:
    """Responde tanto si el update viene de un mensaje normal como de un botón
    del menú (callback_query) -- así los comandos se pueden reusar tal cual
    desde ambos flujos."""
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text(text, **kwargs)


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Saldo", callback_data="coco_menu:saldo"),
         InlineKeyboardButton("Resumen", callback_data="coco_menu:resumen")],
        [InlineKeyboardButton("Cambio", callback_data="coco_menu:cambio"),
         InlineKeyboardButton("Ayuda", callback_data="coco_menu:ayuda")],
    ])
