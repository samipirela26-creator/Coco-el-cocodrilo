"""Handlers del bot de Telegram.
Adaptado de telegram-bot-gastos-llm: agrega /saldo, /resumen y /saldo_inicial;
además soporta multi-moneda (Bs/USD/COP), billeteras por cuenta
(BDV/Binance/Efectivo), tasas BCV/Binance, categorías dinámicas, capturas de
pantalla (transferencia/saldo, con adopción directa del saldo leído) y notas
de voz.
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
CUENTA_EMOJI = {"BDV": "🏦", "Binance": "💻", "Efectivo": "💵"}


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed = context.bot_data.get('allowed_user_ids') or []
    if not allowed:
        return True
    return update.effective_user.id in allowed


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    welcome_message = """¡Hola! 👋

Soy tu bot de finanzas personales. Envíame tus gastos, ingresos o saldos en
lenguaje natural (texto, foto de una captura, o nota de voz) y los voy a
registrar.

📝 Ejemplos de gasto/ingreso:
• "Compré pan por 500" (Bs por defecto)
• "Gasté 20 dólares en el super"
• "Pagué 3000 pesos por un almuerzo"
• "Me pagaron el sueldo, 50000"

💰 Ejemplos para declarar cuánto tienes (sin que sea un gasto/ingreso nuevo):
• "Tengo 50 dólares en efectivo"
• "En Binance tengo 200"
• "Me quedan 300 mil bolívares en el BDV"

También puedes enviarme:
📸 Una captura de una transferencia -> la registro como gasto/ingreso
📸 Una captura de tu saldo (BDV o Binance) -> actualizo esa billetera directo
🎤 Una nota de voz describiendo el gasto o el saldo

Comandos:
/saldo - ver tus 4 billeteras (BDV, Binance, Efectivo USD, Efectivo COP)
/resumen - ver gastos del mes por categoría (con porcentajes)
/saldo_inicial <monto> [moneda] [cuenta] - configurar el saldo de una billetera
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

👛 Billeteras que manejo (independientes entre sí):
• BDV (Bs) — tu cuenta bancaria en bolívares
• Binance (USD) — tus dólares digitales/USDT
• Efectivo (USD) — dólares en cash
• Efectivo (COP) — pesos en cash

📝 Cómo usar:
Envía un mensaje, foto o nota de voz describiendo el gasto, ingreso o saldo, por ejemplo:
• "Compré X por Y" / "Gasté Z en [categoría]" / "Cobré Z de [fuente]"
• "Tengo Z dólares en efectivo" / "En Binance tengo Z" -> actualiza esa billetera directo
Si no mencionas moneda, asumo Bs. Puedes decir "20 dólares" o "3000 pesos" para USD/COP.
En USD, si no mencionas Binance/USDT/cripto, asumo que es Efectivo.

Comandos:
/saldo - tus 4 billeteras, con conversión BCV/Binance de tus Bs
/resumen [mes] - resumen y % de gasto por categoría del mes actual (o YYYY-MM)
/saldo_inicial <monto> [moneda] [cuenta] - fija el saldo de una billetera
  (moneda: Bs/USD/COP; cuenta obligatoria si moneda es USD: Binance o Efectivo)
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
        wallets = {(w['moneda'], w['cuenta']): w['balance'] for w in db.get_all_wallets()}
        bcv = fx.get_bcv_rate(db)
        binance = fx.get_binance_rate(db)

        bs_bdv = wallets.get(('Bs', 'BDV'), 0.0)
        usd_binance = wallets.get(('USD', 'Binance'), 0.0)
        usd_efectivo = wallets.get(('USD', 'Efectivo'), 0.0)
        cop_efectivo = wallets.get(('COP', 'Efectivo'), 0.0)

        lines = ["💰 Tu saldo actual:\n"]
        lines.append(f"{CUENTA_EMOJI['BDV']} BDV (Bs): {bs_bdv:,.2f}")
        if bcv:
            lines.append(f"   ↳ ≈ $ {bs_bdv / bcv:,.2f} a tasa BCV")
        if binance:
            lines.append(f"   ↳ ≈ $ {bs_bdv / binance:,.2f} a tasa Binance")
        lines.append(f"{CUENTA_EMOJI['Binance']} Binance (USD): $ {usd_binance:,.2f}")
        lines.append(f"{CUENTA_EMOJI['Efectivo']} Efectivo (USD): $ {usd_efectivo:,.2f}")
        lines.append(f"{CUENTA_EMOJI['Efectivo']} Efectivo (COP): $ {cop_efectivo:,.2f}")

        if binance:
            total_usd = (bs_bdv / binance) + usd_binance + usd_efectivo
            lines.append("")
            lines.append(f"🌎 Total aprox. en USD (BDV a tasa Binance + Binance + Efectivo USD): $ {total_usd:,.2f}")

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
            "Uso: /saldo_inicial <monto> [moneda] [cuenta]\n"
            "Ej: /saldo_inicial 100000 (Bs, cuenta BDV)\n"
            "Ej: /saldo_inicial 200 USD Binance\n"
            "Ej: /saldo_inicial 50 USD Efectivo\n"
            "Ej: /saldo_inicial 300000 COP"
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

    cuenta = None
    if len(args) > 2:
        cuenta_arg = args[2].strip()
        cuenta_map = {"bdv": "BDV", "binance": "Binance", "efectivo": "Efectivo"}
        cuenta = cuenta_map.get(cuenta_arg.lower())
        if not cuenta:
            await update.message.reply_text("❌ Cuenta inválida. Usa BDV, Binance o Efectivo.")
            return
    elif moneda == 'USD':
        await update.message.reply_text(
            "❌ Para USD tienes que indicar la cuenta:\n"
            "/saldo_inicial <monto> USD Binance\n"
            "/saldo_inicial <monto> USD Efectivo"
        )
        return

    try:
        cuenta_resuelta, anterior = db.set_wallet_balance(moneda, cuenta, monto, fuente='comando')
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al actualizar el saldo: {e}")
        return

    await update.message.reply_text(
        f"✅ Saldo de {cuenta_resuelta} ({moneda}) actualizado\n"
        f"Antes: {anterior:,.2f}\n"
        f"Ahora: {monto:,.2f}"
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
# Guardado compartido (texto, voz, y fotos de transferencia)
# ---------------------------------------------------------------------- #

def format_confirmation_message(expense_data: dict, balance: float, cuenta: str) -> str:
    emoji = "💸" if expense_data['tipo'] == 'gasto' else "💵"
    tipo_str = "Gasto" if expense_data['tipo'] == 'gasto' else "Ingreso"
    moneda = expense_data.get('moneda', 'Bs')
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    return f"""✅ {tipo_str} registrado

{emoji} Monto: {simbolo} {expense_data['monto']:.2f} {moneda}
📂 Categoría: {expense_data['categoria']}
👛 Cuenta: {cuenta}
📅 Fecha: {expense_data['fecha']}
📝 {expense_data['descripcion']}

💰 Saldo en {cuenta} ({moneda}): {balance:,.2f}"""


def format_ajuste_message(moneda: str, cuenta: str, anterior: float, nuevo: float) -> str:
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    return f"""✅ Saldo actualizado

👛 {cuenta} ({moneda})
Antes: {simbolo} {anterior:,.2f}
Ahora: {simbolo} {nuevo:,.2f}"""


async def _save_and_confirm(data: dict, user_id: int, db: DBClient, update: Update,
                             prefix: str = "", fuente: str = 'texto') -> None:
    """Guarda una transacción (gasto/ingreso) o un ajuste de saldo ya parseado
    y validado, y responde con la confirmación correspondiente. Compartido
    por texto, voz y fotos de transferencia."""
    moneda = data.get('moneda', 'Bs')

    if data['tipo'] == 'ajuste_saldo':
        cuenta_resuelta, anterior = db.set_wallet_balance(moneda, data.get('cuenta'), data['monto'], fuente=fuente)
        await update.message.reply_text(f"{prefix}{format_ajuste_message(moneda, cuenta_resuelta, anterior, data['monto'])}")
        return

    cuenta_resuelta = db.append_expense(
        tipo=data['tipo'],
        fecha=data['fecha'],
        descripcion=data['descripcion'],
        categoria=data['categoria'],
        monto=data['monto'],
        user_id=user_id,
        moneda=moneda,
        cuenta=data.get('cuenta'),
    )
    balance = db.get_wallet_balance(moneda, cuenta_resuelta)
    confirmation_message = format_confirmation_message(data, balance, cuenta_resuelta)
    await update.message.reply_text(f"{prefix}{confirmation_message}")


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
    Flujo: 1) construir prompt 2) llamar a Gemini 3) validar 4) guardar/ajustar saldo 5) confirmar.
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

        await _save_and_confirm(expense_data, user_id, db, update, fuente='texto')

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intenta más tarde.")
        logger.error("Error de conexión con Gemini")

    except (GeminiInvalidJSONError,):
        await update.message.reply_text(
            "❌ No pude entender tu mensaje.\n\n💡 Intenta ser más específico:\n"
            "• 'Compré [cosa] por $[monto]'\n• 'Gasté [monto] en [categoría]'\n• 'Tengo [monto] en efectivo'"
        )
        logger.error("JSON inválido desde Gemini")

    except StorageError as e:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")
        logger.error(f"Error de storage: {e}")

    except Exception as e:
        await update.message.reply_text("❌ Ocurrió un error inesperado. Intenta nuevamente.")
        logger.exception(f"Error inesperado: {e}")


# ---------------------------------------------------------------------- #
# Fotos / capturas de pantalla
# ---------------------------------------------------------------------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Descarga la foto de mayor resolución, la analiza con Gemini Vision y
    decide el flujo según 'captura_tipo':
      - "transferencia": registra un gasto/ingreso.
      - "saldo": ADOPTA directo el saldo leído como la billetera correspondiente
        (BDV si es Bs, Binance/Efectivo si es USD según la app detectada) --
        sin pedir confirmación, pero mostrando antes/después para que el
        usuario note al toque si el OCR leyó mal algo (ver db.set_wallet_balance).
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
            cuenta_resuelta, anterior = db.set_wallet_balance(moneda, data.get('cuenta'), monto_banco, fuente='foto')
            await update.message.reply_text(
                f"📸 Detecté una captura de saldo.\n\n"
                f"{format_ajuste_message(moneda, cuenta_resuelta, anterior, monto_banco)}"
            )
            return

        # captura_tipo == "transferencia" (o desconocido -> tratamos como transferencia)
        is_valid, error_message = validate_expense_data(data, categories)
        if not is_valid:
            await update.message.reply_text(
                f"❌ No pude interpretar la captura correctamente: {error_message}"
            )
            return

        await _save_and_confirm(data, user_id, db, update, prefix="📸 Captura de transferencia detectada.\n\n", fuente='foto')

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
    paso vía Gemini, y guarda igual que el flujo de texto (gasto/ingreso o
    ajuste de saldo).
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

        await _save_and_confirm(data, user_id, db, update, prefix="🎤 Nota de voz procesada.\n\n", fuente='audio')

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
