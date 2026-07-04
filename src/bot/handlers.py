"""Manejadores de entrada libre del bot: mensajes de texto, fotos (capturas
de saldo/transferencia) y notas de voz. Comparten el guardado/confirmación
vía `_save_and_confirm`.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.llm.prompt_builder import build_prompt
from src.llm.base import LLMConnector
from src.storage.db import DBClient
from src.utils.validators import validate_expense_data
from src.utils.exceptions import (
    GeminiConnectionError,
    GeminiInvalidJSONError,
    StorageError,
)
from src.bot.access import _is_allowed, _perfil_de, _check_cooldown
from src.bot.formatters import (
    format_confirmation_message, format_ajuste_message, format_ajuste_preview_message,
    format_transferencia_message, format_diezmo_pagado_message,
    format_deuda_nueva_message, format_deuda_pago_message,
    format_meta_nueva_message, format_meta_aporte_message,
)
from src.bot.replies import (
    _reply, _deshacer_keyboard, _category_keyboard, _saldo_confirm_keyboard,
    _cuenta_nueva_confirm_keyboard,
)
from src.bot.constants import MONEDA_SIMBOLO
from src.storage.constants import resolve_cuenta, CUENTAS_POR_MONEDA
from src.bot.commands import menu_command, _maybe_welcome_new_profile

logger = logging.getLogger('gastos-bot')


def _cuenta_es_nueva(db: DBClient, perfil: str, moneda: str, cuenta: str) -> bool:
    """True si `cuenta` no coincide (ni por may/min) con ninguna cuenta ya
    conocida de este perfil+moneda -- ni una de las fijas (BDV/Binance/
    Efectivo) ni una personalizada creada antes. Sirve para decidir si hay
    que ofrecer crearla en vez de forzarla silenciosamente a la cuenta por
    defecto de esa moneda (ver resolve_cuenta en constants.py)."""
    cuenta = (cuenta or "").strip()
    if not cuenta:
        return False
    if db.find_matching_cuenta(perfil, moneda, cuenta):
        return False
    fijas = CUENTAS_POR_MONEDA.get(moneda, ())
    if cuenta.lower() in (v.lower() for v in fijas):
        return False
    return True


async def _pedir_confirmacion_ajuste(context: ContextTypes.DEFAULT_TYPE, update: Update, db: DBClient,
                                      perfil: str, moneda: str, cuenta: str, monto: float,
                                      fuente: str, respuesta: str = "", prefix: str = "") -> None:
    """Muestra una vista previa del ajuste de saldo (antes/nuevo) y pide
    confirmación con botones ANTES de escribir nada en la base de datos --
    ver format_ajuste_preview_message. Guarda los datos pendientes en
    `context.user_data` hasta que llegue la respuesta del botón (ver
    saldo_callback). Compartido por texto/voz (ajuste_saldo) y fotos
    (captura_tipo == 'saldo').

    Si `cuenta` no coincide con ninguna cuenta conocida de este perfil (ej.
    "Mercantil" cuando solo existen BDV/Binance/Efectivo), en vez de forzarla
    a la cuenta por defecto se ofrece crearla como cuenta nueva -- ver
    _cuenta_es_nueva y cuenta_nueva_callback."""
    if _cuenta_es_nueva(db, perfil, moneda, cuenta):
        cuenta_limpia = cuenta.strip()
        context.user_data['pending_cuenta_nueva'] = {
            'perfil': perfil, 'moneda': moneda, 'cuenta': cuenta_limpia,
            'monto': monto, 'fuente': fuente, 'respuesta': respuesta,
        }
        simbolo = MONEDA_SIMBOLO.get(moneda, '')
        mensaje = (
            f"🐊 No tengo registrada la cuenta \"{cuenta_limpia}\" en {moneda}. "
            f"¿Desea que la abra y le asigne ese saldo?\n\n"
            f"🏦 {cuenta_limpia} ({moneda}): {simbolo} {monto:,.2f}"
        )
        await _reply(update, f"{prefix}{mensaje}", reply_markup=_cuenta_nueva_confirm_keyboard())
        return

    cuenta_resuelta = db.resolve_cuenta_perfil(perfil, moneda, cuenta)
    anterior = db.get_wallet_balance(perfil, moneda, cuenta_resuelta)
    context.user_data['pending_ajuste'] = {
        'perfil': perfil, 'moneda': moneda, 'cuenta': cuenta_resuelta,
        'monto': monto, 'fuente': fuente, 'respuesta': respuesta,
    }
    mensaje = format_ajuste_preview_message(moneda, cuenta_resuelta, anterior, monto, respuesta)
    await _reply(update, f"{prefix}{mensaje}", reply_markup=_saldo_confirm_keyboard())


async def cuenta_nueva_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de confirmación de una cuenta nueva detectada automáticamente
    (ver _pedir_confirmacion_ajuste): solo AHORA se crea la cuenta y se le
    asigna el saldo, o se descarta si el usuario dijo que no (en ese caso no
    se toca nada -- ni se crea la cuenta ni se fuerza el monto a otra)."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    decision = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_cuenta_nueva', None)
    await query.answer()
    if decision != "si" or not pending:
        try:
            await query.edit_message_text("🐊 Entendido, no abrí ninguna cuenta nueva.")
        except Exception:
            pass
        return

    db: DBClient = context.bot_data['db']
    try:
        db.create_account(pending['perfil'], pending['moneda'], pending['cuenta'], 0.0)
        cuenta_resuelta, anterior = db.set_wallet_balance(
            pending['perfil'], pending['moneda'], pending['cuenta'], pending['monto'], fuente=pending['fuente']
        )
        mensaje = format_ajuste_message(pending['moneda'], cuenta_resuelta, anterior, pending['monto'], pending.get('respuesta'))
        await query.edit_message_text(f"✨ Cuenta nueva abierta.\n\n{mensaje}")
    except StorageError as e:
        await query.edit_message_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")


async def saldo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de confirmación de _pedir_confirmacion_ajuste: solo AHORA se
    escribe el ajuste en la base de datos (o se descarta si canceló)."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    decision = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_ajuste', None)
    await query.answer()
    if decision != "si" or not pending:
        try:
            await query.edit_message_text("🐊 Cancelado, no toqué su saldo.")
        except Exception:
            pass
        return

    db: DBClient = context.bot_data['db']
    try:
        cuenta_resuelta, anterior = db.set_wallet_balance(
            pending['perfil'], pending['moneda'], pending['cuenta'], pending['monto'], fuente=pending['fuente']
        )
        mensaje = format_ajuste_message(pending['moneda'], cuenta_resuelta, anterior, pending['monto'], pending.get('respuesta'))
        await query.edit_message_text(mensaje)
    except StorageError as e:
        await query.edit_message_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")


async def _save_and_confirm(data: dict, user_id: int, perfil: str, db: DBClient, update: Update,
                             context: ContextTypes.DEFAULT_TYPE = None,
                             prefix: str = "", fuente: str = 'texto') -> None:
    """Guarda una transacción (gasto/ingreso) o una transferencia entre
    billeteras propias ya parseada y validada, en los datos DE ESTE PERFIL
    (aislados de cualquier otro), y responde con la confirmación
    correspondiente. Compartido por texto, voz y fotos de transferencia (a
    un tercero -- distinto de "transferencia" tipo, que es entre billeteras
    propias). Los ajustes de saldo ("ajuste_saldo") NO se guardan acá --
    pasan por _pedir_confirmacion_ajuste primero (ver ese docstring)."""
    moneda = data.get('moneda', 'Bs')

    if data['tipo'] == 'diezmo_pagado':
        # Diezmo: solo informativo, no toca ninguna billetera (ver
        # TithesMixin.mark_tithe_paid en src/storage/tithes.py). "moneda"
        # puede venir vacía/null -- en ese caso salda el pendiente de todas.
        pagados = db.mark_tithe_paid(perfil, data.get('moneda') or None)
        mensaje = format_diezmo_pagado_message(pagados, data.get('respuesta'))
        await _reply(update, f"{prefix}{mensaje}")
        return

    if data['tipo'] == 'ajuste_saldo':
        await _pedir_confirmacion_ajuste(
            context, update, db, perfil, moneda, data.get('cuenta'), data['monto'],
            fuente, data.get('respuesta'), prefix=prefix,
        )
        return

    if data['tipo'] == 'deuda_nueva':
        db.register_debt(
            perfil=perfil, persona=data['persona'], tipo=data['tipo_deuda'],
            moneda=moneda, monto=float(data['monto']),
            descripcion=data.get('descripcion', ''), fecha=data.get('fecha'),
        )
        mensaje = format_deuda_nueva_message(
            data['persona'], data['tipo_deuda'], moneda, float(data['monto']), data.get('respuesta')
        )
        await _reply(update, f"{prefix}{mensaje}")
        return

    if data['tipo'] == 'deuda_pago':
        resultado = db.register_payment(
            perfil=perfil, persona=data['persona'], tipo=data['tipo_deuda'],
            moneda=moneda, monto=float(data.get('monto') or 0),
        )
        mensaje = format_deuda_pago_message(resultado, data['persona'], moneda, data.get('respuesta'))
        await _reply(update, f"{prefix}{mensaje}")
        return

    if data['tipo'] == 'meta_nueva':
        db.create_goal(
            perfil=perfil, nombre=data['nombre_meta'], moneda=moneda,
            monto_objetivo=float(data['monto_objetivo']),
        )
        mensaje = format_meta_nueva_message(
            data['nombre_meta'], moneda, float(data['monto_objetivo']), data.get('respuesta')
        )
        await _reply(update, f"{prefix}{mensaje}")
        return

    if data['tipo'] == 'meta_aporte':
        resultado = db.contribute_goal(perfil, data['nombre_meta'], float(data['monto']))
        if not resultado.get('encontrada'):
            await _reply(
                update,
                f"{prefix}🐊 No encontré ninguna meta activa llamada \"{data['nombre_meta']}\". "
                f"¿Quiere que la cree con /meta_nueva o diciéndome el objetivo?"
            )
            return
        mensaje = format_meta_aporte_message(resultado, data.get('respuesta'))
        await _reply(update, f"{prefix}{mensaje}")
        return

    if data['tipo'] == 'transferencia':
        resultado = db.transfer(
            perfil=perfil,
            moneda_origen=data['moneda_origen'],
            cuenta_origen=data.get('cuenta_origen'),
            monto_origen=float(data['monto_origen']),
            moneda_destino=data['moneda_destino'],
            cuenta_destino=data.get('cuenta_destino'),
            monto_destino=float(data['monto_destino']),
            fuente=fuente,
        )
        mensaje = format_transferencia_message(resultado, data.get('respuesta'), data.get('tasa_cambio'))
        await _reply(update, f"{prefix}{mensaje}")
        return

    cuenta_resuelta = db.append_expense(
        perfil=perfil,
        tipo=data['tipo'],
        fecha=data['fecha'],
        descripcion=data['descripcion'],
        categoria=data['categoria'],
        monto=data['monto'],
        user_id=user_id,
        moneda=moneda,
        cuenta=data.get('cuenta'),
    )
    balance = db.get_wallet_balance(perfil, moneda, cuenta_resuelta)
    budget_status = None
    if data['tipo'] == 'gasto' and moneda == 'Bs':
        budget_status = db.get_budget_status(perfil, data['categoria'], data['fecha'])
    confirmation_message = format_confirmation_message(data, balance, cuenta_resuelta, budget_status)
    # Botón de deshacer solo para gasto/ingreso (mismo alcance que /deshacer,
    # que no cubre ajuste_saldo ni transferencia -- ver delete_last_transaction).
    await _reply(update, f"{prefix}{confirmation_message}", reply_markup=_deshacer_keyboard())


async def _offer_category_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, expense_data: dict) -> None:
    """Cuando el LLM entendió un gasto pero no supo en qué categoría ponerlo
    (validate_expense_data lo rechaza por categoría vacía), en vez de fallar
    le ofrecemos botones con las categorías fijas para que el usuario elija.
    Guarda el resto de los datos ya parseados (monto/fecha/descripción/etc.)
    en `context.user_data` hasta que llegue la elección (ver category_callback)."""
    context.user_data['pending_expense'] = expense_data
    categories = context.bot_data.get('categories', [])
    moneda = expense_data.get('moneda', 'Bs')
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    try:
        monto_str = f"{float(expense_data.get('monto', 0)):,.2f}"
    except (TypeError, ValueError):
        monto_str = str(expense_data.get('monto', ''))
    await _reply(
        update,
        f"🐊 Entendí un gasto de {simbolo} {monto_str} {moneda}, pero no supe en qué categoría "
        f"anotarlo. ¿Cuál de estas encaja?",
        reply_markup=_category_keyboard(categories),
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de categoría del selector de `_offer_category_picker`: completa
    el gasto pendiente con la categoría elegida y lo guarda igual que el
    flujo normal de texto/voz."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    categoria = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_expense', None)
    if categoria == "__cancel__" or not pending:
        await query.answer()
        try:
            await query.edit_message_text("🐊 Descartado. Cuénteme de nuevo cuando guste.")
        except Exception:
            pass
        return

    await query.answer(f"Categoría: {categoria}")
    pending['categoria'] = categoria
    db: DBClient = context.bot_data['db']
    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    try:
        await _save_and_confirm(pending, user_id, perfil, db, update, context, fuente='texto')
    except StorageError as e:
        await _reply(update, f"❌ Error al guardar en la base de datos.\n🔧 {e}")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


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
    if user_message.strip().lower() in ("menu", "menú", "m"):
        await menu_command(update, context)
        return

    if not _check_cooldown(update, context):
        return

    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    await _maybe_welcome_new_profile(update, context, perfil)
    llm_connector: LLMConnector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        dynamic_categories = db.get_dynamic_categories(perfil)
        prompt = build_prompt(user_message, categories, dynamic_categories)
        expense_data = llm_connector.generate(prompt)
        logger.debug(f"Datos extraídos: {expense_data}")

        is_valid, error_message = validate_expense_data(expense_data, categories)
        if not is_valid:
            if expense_data.get('tipo') == 'gasto' and error_message == "La categoría no puede estar vacía":
                await _offer_category_picker(update, context, expense_data)
                return
            await update.message.reply_text(
                f"❌ {error_message}\n\n💡 Intente reformular con monto y categoría claros."
            )
            return

        if expense_data.get('tipo') == 'charla':
            await update.message.reply_text(expense_data.get('respuesta') or "🐊 ¿En qué le ayudo?")
            return

        await _save_and_confirm(expense_data, user_id, perfil, db, update, context, fuente='texto')

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intente más tarde.")
        logger.error("Error de conexión con Gemini")

    except (GeminiInvalidJSONError,):
        await update.message.reply_text(
            "❌ No pude entender su mensaje.\n\n💡 Sea un poco más específico:\n"
            "• 'Compré [cosa] por $[monto]'\n• 'Gasté [monto] en [categoría]'\n• 'Tengo [monto] en efectivo'"
        )
        logger.error("JSON inválido desde Gemini")

    except StorageError as e:
        await update.message.reply_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")
        logger.error(f"Error de storage: {e}")

    except Exception as e:
        await update.message.reply_text("❌ Ocurrió un error inesperado. Intente nuevamente.")
        logger.exception(f"Error inesperado: {e}")


# ---------------------------------------------------------------------- #
# Fotos / capturas de pantalla
# ---------------------------------------------------------------------- #

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Descarga la foto de mayor resolución, la analiza con Gemini Vision y
    decide el flujo según 'captura_tipo':
      - "transferencia": registra un gasto/ingreso.
      - "saldo": pide confirmación (antes/nuevo) antes de sobrescribir la
        billetera correspondiente (BDV si es Bs, Binance/Efectivo si es USD
        según la app detectada) -- ver _pedir_confirmacion_ajuste. Un OCR que
        lee mal un dígito ya no corrompe el saldo en silencio.
    """
    if not _is_allowed(update, context):
        return
    if not _check_cooldown(update, context):
        return

    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    await _maybe_welcome_new_profile(update, context, perfil)
    llm_connector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        photo = update.message.photo[-1]  # mayor resolución
        photo_file = await photo.get_file()
        image_bytes = bytes(await photo_file.download_as_bytearray())

        dynamic_categories = db.get_dynamic_categories(perfil)
        cuentas_propias = context.bot_data.get('profile_to_account_ids', {}).get(perfil)
        data = llm_connector.analyze_image(image_bytes, categories, dynamic_categories, cuentas_propias=cuentas_propias)
        logger.debug(f"Datos extraídos de la imagen: {data}")

        captura_tipo = data.get('captura_tipo')
        moneda = data.get('moneda', 'Bs')

        if captura_tipo == 'diezmo_pagado':
            pagados = db.mark_tithe_paid(perfil, moneda if data.get('moneda') else None)
            mensaje = format_diezmo_pagado_message(pagados, data.get('respuesta'))
            await update.message.reply_text(f"📸 Detecté un pago de diezmo.\n\n{mensaje}")
            return

        if captura_tipo == 'saldo':
            monto_banco = float(data.get('monto', 0))
            await _pedir_confirmacion_ajuste(
                context, update, db, perfil, moneda, data.get('cuenta'), monto_banco,
                fuente='foto', respuesta=data.get('respuesta'), prefix="📸 Detecté una captura de saldo.\n\n",
            )
            return

        # captura_tipo == "transferencia" (o desconocido -> tratamos como transferencia)
        is_valid, error_message = validate_expense_data(data, categories)
        if not is_valid:
            await update.message.reply_text(
                f"❌ No pude interpretar la captura correctamente: {error_message}"
            )
            return

        await _save_and_confirm(data, user_id, perfil, db, update, context, prefix="📸 Captura de transferencia detectada.\n\n", fuente='foto')

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intente más tarde.")
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
    if not _check_cooldown(update, context):
        return

    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    await _maybe_welcome_new_profile(update, context, perfil)
    llm_connector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        await update.message.chat.send_action(action="typing")

        voice = update.message.voice
        voice_file = await voice.get_file()
        audio_bytes = bytes(await voice_file.download_as_bytearray())

        dynamic_categories = db.get_dynamic_categories(perfil)
        data = llm_connector.transcribe_and_parse_audio(audio_bytes, categories, dynamic_categories)
        logger.debug(f"Datos extraídos del audio: {data}")

        is_valid, error_message = validate_expense_data(data, categories)
        if not is_valid:
            if data.get('tipo') == 'gasto' and error_message == "La categoría no puede estar vacía":
                await _offer_category_picker(update, context, data)
                return
            await update.message.reply_text(
                f"❌ No pude entender la nota de voz: {error_message}"
            )
            return

        if data.get('tipo') == 'charla':
            await update.message.reply_text(data.get('respuesta') or "🐊 ¿En qué le ayudo?")
            return

        await _save_and_confirm(data, user_id, perfil, db, update, context, prefix="🎤 Nota de voz procesada.\n\n", fuente='audio')

    except (GeminiConnectionError,):
        await update.message.reply_text("❌ Error de conexión con Gemini. Intente más tarde.")
        logger.error("Error de conexión con Gemini (audio)")
    except (GeminiInvalidJSONError,):
        await update.message.reply_text(
            "❌ No pude entender su nota de voz. Intente hablar más claro o describir monto y categoría."
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
