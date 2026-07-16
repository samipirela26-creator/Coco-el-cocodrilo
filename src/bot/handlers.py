"""Manejadores de entrada libre del bot: mensajes de texto, fotos (capturas
de saldo/transferencia) y notas de voz. Comparten el guardado/confirmación
vía `_save_and_confirm`.
"""
import asyncio
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
    format_deshacer_transferencia_preview_message, format_transferencia_deshecha_message,
)
from src.bot.replies import (
    _reply, _deshacer_keyboard, _category_keyboard, _saldo_confirm_keyboard,
    _cuenta_nueva_confirm_keyboard, _tipo_transferencia_keyboard, _efectivo_keyboard,
    _deshacer_transferencia_keyboard, _deshacer_transferencia_confirm_keyboard,
)
from src.bot.constants import MONEDA_SIMBOLO, CUENTA_EMOJI, INCOME_SOURCES
from src.storage.constants import resolve_cuenta, CUENTAS_POR_MONEDA
from src.bot.commands import menu_command, cambio_command, _maybe_welcome_new_profile

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
        # Botón para revertir el cambio (ver deshacer_transferencia_callback) --
        # a diferencia de _deshacer_keyboard (gasto/ingreso), este pide
        # confirmación explicando el impacto en AMBAS billeteras antes de
        # tocar nada, por pedido explícito del usuario (2026-07-09).
        await _reply(update, f"{prefix}{mensaje}", reply_markup=_deshacer_transferencia_keyboard())
        return

    # Efectivo (USD/COP, cuenta "Efectivo") es plata física que se pierde de
    # vista fácil si Coco solo adivina la categoría/origen -- a diferencia de
    # Bs/Binance, que casi siempre vienen de una captura o de un texto ya
    # detallado. Por pedido explícito del usuario (2026-07-09), TODO gasto o
    # ingreso en efectivo se confirma con botones (categoría/origen) ANTES
    # de tocar la billetera, mostrando de una vez el "antes -> después" --
    # ver _pedir_categoria_efectivo. `_efectivo_confirmado` evita volver a
    # entrar acá cuando esta misma función se reinvoca después de elegir el
    # botón (ver efectivo_categoria_callback).
    if not data.get('_efectivo_confirmado'):
        cuenta_preview = db.resolve_cuenta_perfil(perfil, moneda, data.get('cuenta'))
        if cuenta_preview == 'Efectivo':
            await _pedir_categoria_efectivo(context, update, db, perfil, moneda, cuenta_preview, data, prefix)
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


async def _pedir_confirmacion_tipo_transferencia(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                                  data: dict, user_id: int, perfil: str, prefix: str) -> None:
    """Para TODA captura de Pago Móvil/transferencia se le pregunta al
    usuario con botones si el movimiento fue una salida o una entrada --
    Coco ya no intenta adivinarlo comparando cédulas (ese enfoque resultó
    poco confiable, ver prompt_builder.build_image_prompt), así que siempre
    se confirma con el usuario antes de guardar nada. Guarda los datos ya
    parseados en `context.user_data` hasta que llegue la respuesta (ver
    tipo_transferencia_callback).

    La primera vez que un perfil pasa por este flujo (después de esta
    actualización) se le explica brevemente cómo funciona, ver
    db.has_seen_pago_movil_intro/mark_pago_movil_intro_seen."""
    context.user_data['pending_tipo_transferencia'] = {
        'data': data, 'user_id': user_id, 'perfil': perfil, 'prefix': prefix,
    }
    db: DBClient = context.bot_data['db']
    if not db.has_seen_pago_movil_intro(perfil):
        db.mark_pago_movil_intro_seen(perfil)
        await update.message.reply_text(
            "🐊 Antes de seguir: cada vez que me mandes una captura de Pago Móvil o transferencia "
            "te voy a preguntar con botones si fue una salida (gasto) o una entrada (ingreso) -- "
            "así no me arriesgo a adivinar mal. Si te equivocas de botón, usa \"↩️ Deshacer\" y no "
            "guardo nada."
        )
    simbolo = MONEDA_SIMBOLO.get(data.get('moneda', 'Bs'), '')
    monto = data.get('monto', 0)
    await update.message.reply_text(
        f"{prefix}🐊 Detecté un movimiento de {simbolo} {monto:,.2f}. ¿Fue una salida o una entrada?",
        reply_markup=_tipo_transferencia_keyboard(),
    )


async def tipo_transferencia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de _pedir_confirmacion_tipo_transferencia: recién ahora se fija
    'tipo' con la respuesta del usuario y se guarda de verdad."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    decision = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_tipo_transferencia', None)
    await query.answer()
    if decision == 'cancelar' or decision not in ('gasto', 'ingreso') or not pending:
        try:
            await query.edit_message_text("🐊 Deshecho, no registré nada de esa captura.")
        except Exception:
            pass
        return

    db: DBClient = context.bot_data['db']
    data = pending['data']
    data['tipo'] = decision
    try:
        await _save_and_confirm(
            data, pending['user_id'], pending['perfil'], db, update, context,
            prefix=pending['prefix'], fuente='foto',
        )
    except StorageError as e:
        await query.edit_message_text(f"❌ Error al guardar en la base de datos.\n🔧 {e}")


async def deshacer_transferencia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón "↩️ Deshacer cambio" bajo cada confirmación de transferencia
    (ver _save_and_confirm, rama 'transferencia'): NO revierte nada todavía
    -- primero busca la última transferencia DE ESTE PERFIL (ver
    db.peek_last_transfer) y explica exactamente qué se va a deshacer en
    ambas billeteras, pidiendo confirmación (ver
    deshacer_transferencia_confirmacion_callback). Por pedido explícito del
    usuario (2026-07-09): "pide deshacer y explica lo que deshará"."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    await query.answer()
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    pendiente = db.peek_last_transfer(perfil)

    if pendiente is None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _reply(update, "🐊 No tengo ningún cambio de divisa reciente suyo que deshacer.")
        return

    context.user_data['pending_deshacer_transferencia'] = {
        'perfil': perfil,
        'snapshot_id_origen': pendiente['snapshot_id_origen'],
        'snapshot_id_destino': pendiente['snapshot_id_destino'],
    }
    await _reply(
        update, format_deshacer_transferencia_preview_message(pendiente),
        reply_markup=_deshacer_transferencia_confirm_keyboard(),
    )


async def deshacer_transferencia_confirmacion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de deshacer_transferencia_callback: solo AHORA se revierte de
    verdad (por los ids puntuales de los dos snapshots que se mostraron, no
    "la última en este momento" a ciegas -- ver db.undo_transfer_by_ids), para
    no arriesgarse a revertir otra distinta si algo cambió entre el aviso y
    la confirmación."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    decision = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_deshacer_transferencia', None)
    await query.answer()
    if decision != "si" or not pending:
        try:
            await query.edit_message_text("🐊 Entendido, no deshice nada.")
        except Exception:
            pass
        return

    db: DBClient = context.bot_data['db']
    try:
        resultado = db.undo_transfer_by_ids(
            pending['perfil'], pending['snapshot_id_origen'], pending['snapshot_id_destino']
        )
    except StorageError as e:
        await query.edit_message_text(f"❌ Error al deshacer: {e}")
        return

    if resultado is None:
        await query.edit_message_text(
            "🐊 Ese cambio ya no está disponible (puede que ya lo hubiera deshecho antes)."
        )
        return

    await query.edit_message_text(format_transferencia_deshecha_message(resultado))


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


async def _pedir_categoria_efectivo(context: ContextTypes.DEFAULT_TYPE, update: Update, db: DBClient,
                                     perfil: str, moneda: str, cuenta: str, data: dict,
                                     prefix: str = "") -> None:
    """Para TODO gasto/ingreso en efectivo (USD o COP, cuenta "Efectivo") se
    pide SIEMPRE con botones en qué se gastó (categoría) o de dónde vino
    (origen), antes de tocar la billetera -- ver el `if` en _save_and_confirm
    que llama a esta función. Muestra de una vez el "antes -> después" de esa
    billetera (la "calculadora" que pidió el usuario) para que confirme
    visualmente el impacto antes de elegir. Guarda los datos ya parseados en
    `context.user_data['pending_efectivo']` hasta que llegue la respuesta --
    ver efectivo_categoria_callback."""
    tipo = data['tipo']
    monto = float(data['monto'])
    anterior = db.get_wallet_balance(perfil, moneda, cuenta)
    proyectado = anterior + monto if tipo == 'ingreso' else anterior - monto
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    cuenta_emoji = CUENTA_EMOJI.get(cuenta, '👛')

    pending = dict(data)
    pending['moneda'] = moneda
    pending['cuenta'] = cuenta
    context.user_data['pending_efectivo'] = pending

    if tipo == 'gasto':
        opciones = context.bot_data.get('categories', [])
        pregunta = "¿En qué lo gastó?"
    else:
        opciones = INCOME_SOURCES
        pregunta = "¿De dónde vino ese ingreso?"

    mensaje = (
        f"{prefix}🐊 {'Gasto' if tipo == 'gasto' else 'Ingreso'} de {simbolo} {monto:,.2f} {moneda} en efectivo.\n\n"
        f"{cuenta_emoji} {cuenta} ({moneda}): {simbolo} {anterior:,.2f} → {simbolo} {proyectado:,.2f}\n\n"
        f"{pregunta}"
    )
    await _reply(update, mensaje, reply_markup=_efectivo_keyboard(opciones))


async def efectivo_categoria_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de categoría/origen del selector de efectivo (ver
    _pedir_categoria_efectivo): completa el movimiento pendiente y recién
    ahora lo guarda de verdad (toca la billetera)."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    valor = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.pop('pending_efectivo', None)
    if valor == "__cancel__" or not pending:
        await query.answer()
        try:
            await query.edit_message_text("🐊 Descartado. Cuénteme de nuevo cuando guste.")
        except Exception:
            pass
        return

    await query.answer(valor)
    pending['categoria'] = valor
    if not pending.get('descripcion'):
        # Para ingreso, "categoria" guarda el origen elegido -- si el LLM no
        # había puesto una descripción propia, se usa el mismo origen (se ve
        # reflejado igual en la confirmación final, ver format_confirmation_message).
        pending['descripcion'] = valor
    pending['_efectivo_confirmado'] = True

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
    from src.bot.calculator import calculadora_command, _registrar_pending

    # Si veníamos esperando que el usuario escriba una categoría propia desde
    # la calculadora (botón "✏️ Otra…"), este texto ES esa categoría -- no un
    # gasto nuevo que mandar al LLM. Ver calculator.calc_categoria_callback.
    pending_cat = context.user_data.pop('calc_await_categoria', None)
    if pending_cat is not None:
        categoria = user_message.strip()
        if not categoria:
            context.user_data['calc_await_categoria'] = pending_cat
            await update.message.reply_text("✏️ Escríbame el nombre de la categoría (no puede ir vacío):")
            return
        pending_cat['categoria'] = categoria
        pending_cat['descripcion'] = categoria
        await _registrar_pending(update, context, pending_cat)
        return

    # Atajo del botón fijo "🧮 Calculadora" (teclado de respuesta persistente).
    if user_message.strip().lower() in ("calculadora", "🧮 calculadora"):
        await calculadora_command(update, context)
        return

    if user_message.strip().lower() in ("menu", "menú", "m"):
        await menu_command(update, context)
        return

    # Atajo del botón fijo "💱 Tasas" (teclado de respuesta persistente, ver
    # _tasas_reply_keyboard) -- sin esto, el texto del botón caería en el LLM
    # como si fuera un gasto/ingreso cualquiera.
    if user_message.strip().lower() in ("tasas", "💱 tasas"):
        await cambio_command(update, context)
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
        # Gemini se llama con urllib bloqueante; se corre en un hilo aparte para
        # NO congelar el event loop del bot mientras Gemini responde (o se satura
        # con reintentos). Sin esto, una llamada lenta/429 bloquea a todos los
        # usuarios y los jobs de salud (ver incidente 2026-07-16).
        expense_data = await asyncio.to_thread(llm_connector.generate, prompt)
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

    perfil = _perfil_de(update.effective_user.id, context)
    await _maybe_welcome_new_profile(update, context, perfil)

    await update.message.chat.send_action(action="typing")
    photo = update.message.photo[-1]  # mayor resolución
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    # En Telegram se puede escribir un comentario debajo de la imagen (caption).
    # Lo pasamos como contexto para que Gemini aclare la captura (ver
    # build_image_prompt): ej. "esto fue comida", "pagué el diezmo".
    await _procesar_foto(update, context, image_bytes, "image/jpeg",
                         caption=update.message.caption)


async def _procesar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          image_bytes: bytes, mime_type: str, caption: str = None) -> None:
    """Analiza la captura ya descargada con Gemini Vision y decide el flujo
    según 'captura_tipo' -- separado de handle_photo para poder reusarlo
    cuando la captura quedó pendiente esperando la cédula del usuario (ver
    _pedir_cedula)."""
    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    llm_connector = context.bot_data['llm_connector']
    db: DBClient = context.bot_data['db']
    categories = context.bot_data['categories']

    try:
        dynamic_categories = db.get_dynamic_categories(perfil)
        # Bloqueante (urllib) -> en hilo aparte para no congelar el event loop.
        data = await asyncio.to_thread(
            llm_connector.analyze_image, image_bytes, categories, dynamic_categories,
            mime_type, caption)
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

        prefix = "📸 Captura de transferencia detectada.\n\n"
        await _pedir_confirmacion_tipo_transferencia(update, context, data, user_id, perfil, prefix)

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
        # Bloqueante (urllib) -> en hilo aparte para no congelar el event loop.
        data = await asyncio.to_thread(
            llm_connector.transcribe_and_parse_audio, audio_bytes, categories, dynamic_categories)
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
