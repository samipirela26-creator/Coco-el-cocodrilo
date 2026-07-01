"""Handlers del bot de Telegram.
Adaptado de telegram-bot-gastos-llm: agrega /saldo, /resumen y /saldo_inicial;
además soporta multi-moneda (Bs/USD/COP), tasas BCV/Binance, categorías
dinámicas, capturas de pantalla (transferencia/saldo) y notas de voz.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.llm.prompt_builder import build_prompt
from src.llm.base import LLMConnector
from src.storage.db import DBClient
from src.services import fx
from src.utils.validators import validate_expense_data
from src.utils.exceptions import (
    GeminiConnectionError,
    GeminiInvalidJSONError,
    StorageError,
)

logger = logging.getLogger('gastos-bot')

MONEDA_SIMBOLO = {"Bs": "Bs", "USD": "$", "COP": "$"}


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed = context.bot_data.get('allowed_user_ids') or []
    if not allowed:
        return True
    return update.effective_user.id in allowed


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    welcome_message = """¡Hola! 👋

Soy tu bot de finanzas personales. Envíame tus gastos o ingresos en lenguaje natural
(texto, foto de una captura, o nota de voz) y los voy a registrar.

📝 Ejemplos:
• "Compré pan por 500" (Bs por defecto)
• "Gasté 20 dólares en el super"
• "Pagué 3000 pesos por un almuerzo"
• "Me pagaron el sueldo, 50000"

También puedes enviarme:
📸 Una captura de una transferencia -> la registro como gasto/ingreso
📸 Una captura de tu saldo bancario -> comparo con lo que tengo registrado
🎤 Una nota de voz describiendo el gasto

Comandos:
/saldo - ver tu saldo actual (Bs, USD, COP)
/resumen - ver gastos del mes por categoría (con porcentajes)
/saldo_inicial <monto> [moneda] - configurar tu saldo de partida
/cambio - ver tasas BCV y Binance
/help - ver categorías y ayuda"""
    await update.message.reply_text(welcome_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    categories = context.bot_data.get('categories', [])
    categories_str = '\n• '.join(categories)
    help_message = f"""ℹ️ Ayuda - Bot de Gastos

📂 Categorías fijas de gasto:
• {categories_str}

(También puedo crear categorías nuevas automáticamente si el gasto no encaja en ninguna,
ej: "Gasto de Gio", "Comida en la calle".)

📝 Cómo usar:
Envía un mensaje, foto o nota de voz describiendo el gasto o ingreso, por ejemplo:
• "Compré X por Y"
• "Gasté Z en [categoría]"
• "Cobré Z de [fuente]"
Si no mencionas moneda, asumo Bs. Puedes decir "20 dólares" o "3000 pesos" para USD/COP.

Comandos:
/saldo - saldo actual en Bs, USD y COP (con conversión BCV/Binance)
/resumen [mes] - resumen y % de gasto por categoría del mes actual (o YYYY-MM)
/saldo_inicial <monto> [moneda] - fija tu saldo de partida (moneda opcional, default Bs)
/cambio - tasas BCV, Binance y USD->COP"""
    await update.message.reply_text(help_message)


def _format_rates_block(bcv: float, binance: float) -> str:
    return (
        f"🏦 BCV: {bcv:,.2f} Bs/USD\n"
        f"💵 Binance: {binance:,.2f} Bs/USD\n"
        f"🌎 USD -> COP: {fx.COP_PER_USD:,.0f} (tasa fija)"
    )


async def cambio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    bcv = fx.get_bcv_rate(db)
    binance = fx.get_binance_rate(db)
    await update.message.reply_text(f"💱 Tasas actuales\n\n{_format_rates_block(bcv, binance)}")


async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    try:
        balances = db.get_balances()
        bcv = fx.get_bcv_rate(db)
        binance = fx.get_binance_rate(db)

        lines = ["💰 Tu saldo actual:\n"]
        lines.append(f"Bs {balances['Bs']:,.2f}")
        lines.append(f"$ {balances['USD']:,.2f} (USD efectivo)")
        lines.append(f"$ {balances['COP']:,.2f} COP")
        lines.append("")
        if bcv:
            lines.append(f"↳ Tus Bs equivalen a ~${balances['Bs'] / bcv:,.2f} a tasa BCV")
        if binance:
            lines.append(f"↳ Tus Bs equivalen a ~${balances['Bs'] / binance:,.2f} a tasa Binance")
        lines.append("")
        lines.append(_format_rates_block(bcv, binance))

        await update.message.reply_text('\n'.join(lines))
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al consultar el saldo: {e}")


async def saldo_inicial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: /saldo_inicial <monto> [moneda]\n"
            "Ej: /saldo_inicial 100000 (Bs por defecto)\n"
            "Ej: /saldo_inicial 200 USD"
        )
        return
    try:
        monto = float(args[0].replace(',', '.'))
    except ValueError:
        await update.message.reply_text("❌ El monto debe ser un número, ej: /saldo_inicial 100000")
        return

    moneda = 'Bs'
    if len(args) > 1:
        moneda_arg = args[1].strip().upper()
        moneda_map = {"BS": "Bs", "USD": "USD", "COP": "COP"}
        moneda = moneda_map.get(moneda_arg)
        if not moneda:
            await update.message.reply_text("❌ Moneda inválida. Usa Bs, USD o COP.")
            return

    db.set_initial_balance(monto, moneda=moneda)
    nuevo_saldo = db.get_balance(moneda)
    await update.message.reply_text(
        f"✅ Saldo inicial ({moneda}) configurado en {monto:,.2f}\n"
        f"💰 Saldo actual ({moneda}): {nuevo_saldo:,.2f}"
    )


async def resumen_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    from datetime import datetime
    import calendar

    args = context.args
    if args:
        try:
            year, month = map(int, args[0].split('-'))
        except ValueError:
            await update.message.reply_text("Formato inválido. Usa /resumen YYYY-MM, ej: /resumen 2026-06")
            return
    else:
        now = datetime.now()
        year, month = now.year, now.month

    last_day = calendar.monthrange(year, month)[1]
    fecha_desde = f"{year:04d}-{month:02d}-01"
    fecha_hasta = f"{year:04d}-{month:02d}-{last_day:02d}"

    try:
        summaries = db.get_summary(fecha_desde, fecha_hasta)  # dict por moneda
        bcv = fx.get_bcv_rate(db)
        binance = fx.get_binance_rate(db)
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al calcular el resumen: {e}")
        return

    tiene_movimientos = any(
        s['total_gastos'] > 0 or s['total_ingresos'] > 0 for s in summaries.values()
    )
    if not tiene_movimientos:
        await update.message.reply_text(f"No hay movimientos registrados en {year:04d}-{month:02d}.")
        return

    lines = [f"📊 Resumen {year:04d}-{month:02d}\n"]
    for moneda, summary in summaries.items():
        if summary['total_gastos'] == 0 and summary['total_ingresos'] == 0:
            continue
        lines.append(f"— {moneda} —")
        lines.append(f"💸 Total gastado: {summary['total_gastos']:,.2f}")
        lines.append(f"💵 Total ingresos: {summary['total_ingresos']:,.2f}")
        if summary['categorias']:
            for c in summary['categorias']:
                lines.append(f"  • {c['categoria']}: {c['total']:,.2f} ({c['porcentaje']}%)")
        lines.append("")

    lines.append(_format_rates_block(bcv, binance))

    await update.message.reply_text('\n'.join(lines))


# ---------------------------------------------------------------------- #
# Mensajes de texto
# ---------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    message = update.message
    if not message.text:
        return
    await handle_text_message(message.text, update, context)


async def handle_text_message(user_message: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Flujo: 1) construir prompt 2) llamar a Gemini 3) validar 4) guardar en SQLite 5) confirmar.
    """
    user_id = update.effective_user.id
    llm_connector: LLMConnector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        dynamic_categories = db.get_dynamic_categories()
        prompt = build_prompt(user_message, categories, dynamic_categories)
        expense_data = llm_connector.generate(prompt)
        logger.debug(f"Datos extraídos: {expense_data}")

        is_valid, error_message = validate_expense_data(expense_data, categories)
        if not is_valid:
            await update.message.reply_text(
                f"❌ {error_message}\n\n💡 Intenta reformular con monto y categoría claros."
            )
            return

        moneda = expense_data.get('moneda', 'Bs')
        db.append_expense(
            tipo=expense_data['tipo'],
            fecha=expense_data['fecha'],
            descripcion=expense_data['descripcion'],
            categoria=expense_data['categoria'],
            monto=expense_data['monto'],
            user_id=user_id,
            moneda=moneda,
        )

        confirmation_message = format_confirmation_message(expense_data, db.get_balance(moneda))
        await update.message.reply_text(confirmation_message)

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intenta más tarde.")
        logger.error("Error de conexión con Gemini")

    except (GeminiInvalidJSONError,):
        await update.message.reply_text(
            "❌ No pude entender tu mensaje.\n\n💡 Intenta ser más específico:\n"
            "• 'Compré [cosa] por $[monto]'\n• 'Gasté [monto] en [categoría]'"
        )
        logger.error("JSON inválido desde Gemini")

    except StorageError as e:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")
        logger.error(f"Error de storage: {e}")

    except Exception as e:
        await update.message.reply_text("❌ Ocurrió un error inesperado. Intenta nuevamente.")
        logger.exception(f"Error inesperado: {e}")


def format_confirmation_message(expense_data: dict, balance: float) -> str:
    emoji = "💸" if expense_data['tipo'] == 'gasto' else "💵"
    tipo_str = "Gasto" if expense_data['tipo'] == 'gasto' else "Ingreso"
    moneda = expense_data.get('moneda', 'Bs')
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    return f"""✅ {tipo_str} registrado

{emoji} Monto: {simbolo} {expense_data['monto']:.2f} {moneda}
📂 Categoría: {expense_data['categoria']}
📅 Fecha: {expense_data['fecha']}
📝 {expense_data['descripcion']}

💰 Saldo actual ({moneda}): {balance:,.2f}"""


# ---------------------------------------------------------------------- #
# Fotos / capturas de pantalla
# ---------------------------------------------------------------------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Descarga la foto de mayor resolución, la analiza con Gemini Vision y
    decide el flujo según 'captura_tipo':
      - "transferencia": registra un gasto/ingreso.
      - "saldo": no registra transacción; compara el saldo del banco vs el
        saldo que tiene el bot y guarda un snapshot informativo (ver README,
        sección "Captura de saldo bancario").
    """
    if not _is_allowed(update, context):
        return

    user_id = update.effective_user.id
    llm_connector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        photo = update.message.photo[-1]  # mayor resolución
        photo_file = await photo.get_file()
        image_bytes = bytes(await photo_file.download_as_bytearray())

        dynamic_categories = db.get_dynamic_categories()
        data = llm_connector.analyze_image(image_bytes, categories, dynamic_categories)
        logger.debug(f"Datos extraídos de la imagen: {data}")

        captura_tipo = data.get('captura_tipo')
        moneda = data.get('moneda', 'Bs')

        if captura_tipo == 'saldo':
            monto_banco = float(data.get('monto', 0))
            monto_bot = db.get_balance(moneda)
            db.save_balance_snapshot(moneda, monto_banco, monto_bot)
            diferencia = monto_banco - monto_bot
            await update.message.reply_text(
                f"🏦 Detecté una captura de saldo bancario ({moneda}).\n\n"
                f"Tu banco dice: {monto_banco:,.2f}\n"
                f"El bot tiene registrado: {monto_bot:,.2f}\n"
                f"Diferencia: {diferencia:,.2f}\n\n"
                f"No registré ningún movimiento (esto es solo informativo).\n"
                f"Si quieres que el bot use el saldo del banco como referencia, usa:\n"
                f"/saldo_inicial {monto_banco:.2f} {moneda}"
            )
            return

        # captura_tipo == "transferencia" (o desconocido -> tratamos como transferencia)
        is_valid, error_message = validate_expense_data(data, categories)
        if not is_valid:
            await update.message.reply_text(
                f"❌ No pude interpretar la captura correctamente: {error_message}"
            )
            return

        db.append_expense(
            tipo=data['tipo'],
            fecha=data['fecha'],
            descripcion=data['descripcion'],
            categoria=data['categoria'],
            monto=data['monto'],
            user_id=user_id,
            moneda=moneda,
        )
        confirmation_message = format_confirmation_message(data, db.get_balance(moneda))
        await update.message.reply_text(f"📸 Captura de transferencia detectada.\n\n{confirmation_message}")

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intenta más tarde.")
        logger.error("Error de conexión con Gemini (imagen)")
    except (GeminiInvalidJSONError,):
        await update.message.reply_text("❌ No pude interpretar la captura de pantalla.")
        logger.error("JSON inválido desde Gemini (imagen)")
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")
        logger.error(f"Error de storage (imagen): {e}")
    except Exception as e:
        await update.message.reply_text("❌ Ocurrió un error inesperado al procesar la imagen.")
        logger.exception(f"Error inesperado (imagen): {e}")


# ---------------------------------------------------------------------- #
# Notas de voz
# ---------------------------------------------------------------------- #

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Descarga la nota de voz (.ogg/opus), la transcribe y parsea en un solo
    paso vía Gemini, y registra la transacción igual que el flujo de texto.
    """
    if not _is_allowed(update, context):
        return

    user_id = update.effective_user.id
    llm_connector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        voice = update.message.voice
        voice_file = await voice.get_file()
        audio_bytes = bytes(await voice_file.download_as_bytearray())

        dynamic_categories = db.get_dynamic_categories()
        data = llm_connector.transcribe_and_parse_audio(audio_bytes, categories, dynamic_categories)
        logger.debug(f"Datos extraídos del audio: {data}")

        is_valid, error_message = validate_expense_data(data, categories)
        if not is_valid:
            await update.message.reply_text(
                f"❌ No pude entender la nota de voz: {error_message}"
            )
            return

        moneda = data.get('moneda', 'Bs')
        db.append_expense(
            tipo=data['tipo'],
            fecha=data['fecha'],
            descripcion=data['descripcion'],
            categoria=data['categoria'],
            monto=data['monto'],
            user_id=user_id,
            moneda=moneda,
        )
        confirmation_message = format_confirmation_message(data, db.get_balance(moneda))
        await update.message.reply_text(f"🎤 Nota de voz procesada.\n\n{confirmation_message}")

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intenta más tarde.")
        logger.error("Error de conexión con Gemini (audio)")
    except (GeminiInvalidJSONError,):
        await update.message.reply_text(
            "❌ No pude entender tu nota de voz. Intenta hablar más claro o describir monto y categoría."
        )
        logger.error("JSON inválido desde Gemini (audio)")
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")
        logger.error(f"Error de storage (audio): {e}")
    except Exception as e:
        await update.message.reply_text("❌ Ocurrió un error inesperado al procesar la nota de voz.")
        logger.exception(f"Error inesperado (audio): {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción no manejada capturada por el manejador global:", exc_info=context.error)
