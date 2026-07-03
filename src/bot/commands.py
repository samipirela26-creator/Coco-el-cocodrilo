"""Comandos de Telegram del bot (/menu, /start, /help, /saldo, /cambio,
/resumen, /exportar, /deshacer, /presupuesto, /racha, /saldo_inicial).

Cada comando sigue el mismo patrón: `_is_allowed` -> obtener `db`/`perfil` ->
lógica -> `_reply` (para que también funcione como botón del /menu).
"""
import csv
import io
import logging
from datetime import datetime
import calendar
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from src.storage.db import DBClient
from src.services import fx
from src.utils.exceptions import StorageError
from src.bot.access import _is_allowed, _perfil_de
from src.bot.replies import _reply, _reply_photo, _menu_keyboard, _resumen_nav_keyboard
from src.bot.formatters import _format_rates_block, _rate_variation_pct, format_diezmo_pagado_message
from src.bot.texts import _welcome_text, _help_text
from src.bot.constants import MONEDA_SIMBOLO, CUENTA_EMOJI
from src.reports.weekly_image import render_monthly_report, MESES_ES
from src.reports.balance_image import render_balance_report

logger = logging.getLogger('gastos-bot')


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    await _reply(update, "🐊 ¿Qué necesita?", reply_markup=_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja los botones inline del /menu (mismo patrón que el menú de Larry:
    cada botón reusa el comando equivalente, sin duplicar lógica)."""
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update, context):
        return
    accion = (query.data or "").split(":", 1)[-1]
    if accion == "saldo":
        await saldo_command(update, context)
    elif accion == "resumen":
        await resumen_command(update, context)
    elif accion == "cambio":
        await cambio_command(update, context)
    elif accion == "ayuda":
        await help_command(update, context)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    await _reply(update, _welcome_text())


async def _maybe_welcome_new_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, perfil: str) -> None:
    """Registro abierto: si es la primera vez que se ve este perfil (nadie lo
    agregó a mano en USER_PROFILES), le manda el saludo de bienvenida UNA
    sola vez, antes de procesar su mensaje/foto/nota de voz con normalidad.
    Cada perfil nuevo queda aislado (sus propios saldos/gastos), no ve nada
    de los demás. Además, si hay OWNER_USER_ID configurado, le avisa al
    dueño del bot (para que se entere de quién se está registrando solo)."""
    db: DBClient = context.bot_data['db']
    if db.is_new_profile(perfil):
        await _reply(update, _welcome_text(nuevo_registro=True))
        await _notify_owner_new_profile(update, context, perfil)


async def _notify_owner_new_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, perfil: str) -> None:
    owner_id = context.bot_data.get('owner_user_id')
    if not owner_id:
        return
    user = update.effective_user
    if user.id == owner_id:
        return  # el dueño no se auto-notifica si el perfil nuevo es el suyo
    nombre = user.full_name or user.username or str(user.id)
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                f"🐊 Nuevo registro en Coco: {nombre} (user_id {user.id}, "
                f"perfil '{perfil}') acaba de escribirle por primera vez."
            ),
        )
    except Exception as e:
        logger.warning(f"No se pudo avisar al dueño del nuevo perfil '{perfil}': {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    categories = context.bot_data.get('categories', [])
    await _reply(update, _help_text(categories))


async def cambio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    bcv, bcv_prev = fx.get_bcv_rate_with_variation(db)
    binance, binance_prev = fx.get_binance_rate_with_variation(db)
    bcv_var = _rate_variation_pct(bcv, bcv_prev)
    binance_var = _rate_variation_pct(binance, binance_prev)
    await _reply(update, f"💱 Tasas actuales\n\n{_format_rates_block(bcv, binance, bcv_var, binance_var)}")


async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    try:
        wallets = {(w['moneda'], w['cuenta']): w['balance'] for w in db.get_all_wallets(perfil)}
        bcv, bcv_prev = fx.get_bcv_rate_with_variation(db)
        binance, binance_prev = fx.get_binance_rate_with_variation(db)
        bcv_var = _rate_variation_pct(bcv, bcv_prev)
        binance_var = _rate_variation_pct(binance, binance_prev)

        bs_bdv = wallets.get(('Bs', 'BDV'), 0.0)
        usd_binance = wallets.get(('USD', 'Binance'), 0.0)
        usd_efectivo = wallets.get(('USD', 'Efectivo'), 0.0)
        cop_efectivo = wallets.get(('COP', 'Efectivo'), 0.0)

        lines = ["💰 Su saldo actual:\n"]
        lines.append(f"{CUENTA_EMOJI['BDV']} BDV (Bs): {bs_bdv:,.2f}")
        if bcv:
            lines.append(f"   ↳ ≈ $ {bs_bdv / bcv:,.2f} a tasa BCV")
        if binance:
            lines.append(f"   ↳ ≈ $ {bs_bdv / binance:,.2f} a tasa Binance")
        lines.append(f"{CUENTA_EMOJI['Binance']} Binance (USD): $ {usd_binance:,.2f}")

        efectivo_cop_en_usd = cop_efectivo / fx.COP_PER_USD if fx.COP_PER_USD else 0.0
        efectivo_total_usd = usd_efectivo + efectivo_cop_en_usd
        efectivo_total_cop = efectivo_total_usd * fx.COP_PER_USD
        lines.append(f"{CUENTA_EMOJI['Efectivo']} Efectivo:")
        lines.append(f"   ↳ $ {usd_efectivo:,.2f} dólares")
        lines.append(f"   ↳ $ {cop_efectivo:,.2f} pesos (COP)")
        lines.append(f"   ↳ Junto: $ {efectivo_total_usd:,.2f} si lo pasa todo a dólares")
        lines.append(f"   ↳ Junto: $ {efectivo_total_cop:,.2f} si lo pasa todo a pesos")

        if binance:
            total_usd = (bs_bdv / binance) + usd_binance + efectivo_total_usd
            lines.append("")
            lines.append(f"🌎 Total aprox. en USD (BDV a tasa Binance + Binance + Efectivo): $ {total_usd:,.2f}")

        fijas = {('Bs', 'BDV'), ('USD', 'Binance'), ('USD', 'Efectivo'), ('COP', 'Efectivo')}
        personalizadas = [w for w in db.get_all_wallets(perfil) if (w['moneda'], w['cuenta']) not in fijas]
        if personalizadas:
            lines.append("")
            lines.append("🏦 Otras cuentas:")
            for w in personalizadas:
                simbolo = MONEDA_SIMBOLO.get(w['moneda'], '')
                lines.append(f"   ↳ {w['cuenta']} ({w['moneda']}): {simbolo} {w['balance']:,.2f}")

        racha = db.get_current_streak(perfil)
        if racha > 0:
            lines.append("")
            lines.append(f"🔥 Racha: {racha} día{'s' if racha != 1 else ''} seguido{'s' if racha != 1 else ''} registrando")

        lines.append("")
        lines.append(_format_rates_block(bcv, binance, bcv_var, binance_var))

        await _reply(update, '\n'.join(lines))

        items = [{"label": f"{CUENTA_EMOJI['BDV']} BDV (Bs)", "value_str": f"{bs_bdv:,.2f} Bs",
                  "usd_equiv": (bs_bdv / binance) if binance else 0.0}]
        if usd_binance:
            items.append({"label": f"{CUENTA_EMOJI['Binance']} Binance (USD)",
                           "value_str": f"$ {usd_binance:,.2f}", "usd_equiv": usd_binance})
        if usd_efectivo:
            items.append({"label": f"{CUENTA_EMOJI['Efectivo']} Efectivo (USD)",
                           "value_str": f"$ {usd_efectivo:,.2f}", "usd_equiv": usd_efectivo})
        if cop_efectivo:
            items.append({"label": f"{CUENTA_EMOJI['Efectivo']} Efectivo (COP)",
                           "value_str": f"$ {cop_efectivo:,.2f}", "usd_equiv": efectivo_cop_en_usd})
        total_usd_img = ((bs_bdv / binance) + usd_binance + efectivo_total_usd) if binance else None
        image_bytes = render_balance_report(items, total_usd=total_usd_img)
        await _reply_photo(update, image_bytes, caption="💰 Su saldo actual")
    except StorageError as e:
        await _reply(update, f"❌ Error al consultar el saldo: {e}")


async def cuenta_nueva_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abre una cuenta nueva DE FORMA DELIBERADA (a diferencia de la
    creación automática con confirmación cuando se menciona una cuenta
    desconocida en un ajuste de saldo -- ver _pedir_confirmacion_ajuste en
    handlers.py). Útil para quien tiene varias cuentas del mismo banco (ej.
    Mercantil además de BDV) y quiere abrirla sin tener que "reportar" un
    saldo primero."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    args = context.args
    if len(args) < 2:
        await _reply(
            update,
            "🐊 Uso: /cuenta_nueva <moneda: Bs/USD/COP> <nombre de la cuenta>\n"
            "Ej: /cuenta_nueva Bs Mercantil"
        )
        return
    moneda_raw = args[0].strip().lower()
    mapa_moneda = {'bs': 'Bs', 'usd': 'USD', 'cop': 'COP'}
    moneda = mapa_moneda.get(moneda_raw)
    if not moneda:
        await _reply(update, "🐊 Moneda no reconocida. Use Bs, USD o COP.")
        return
    nombre = " ".join(args[1:]).strip()
    if not nombre:
        await _reply(update, "🐊 Necesito un nombre para la cuenta.")
        return
    try:
        creada = db.create_account(perfil, moneda, nombre, 0.0)
        if creada:
            await _reply(update, f"✨ Cuenta abierta: {nombre} ({moneda}), saldo inicial 0.00.")
        else:
            await _reply(update, f"🐊 Ya existe una cuenta \"{nombre}\" en {moneda}.")
    except StorageError as e:
        await _reply(update, f"❌ Error al abrir la cuenta: {e}")


async def saldo_inicial_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
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
            await update.message.reply_text("❌ Moneda inválida. Use Bs, USD o COP.")
            return

    cuenta = None
    if len(args) > 2:
        cuenta_arg = args[2].strip()
        cuenta_map = {"bdv": "BDV", "binance": "Binance", "efectivo": "Efectivo"}
        cuenta = cuenta_map.get(cuenta_arg.lower())
        if not cuenta:
            await update.message.reply_text("❌ Cuenta inválida. Use BDV, Binance o Efectivo.")
            return
    elif moneda == 'USD':
        await update.message.reply_text(
            "❌ Para USD necesito que indique la cuenta:\n"
            "/saldo_inicial <monto> USD Binance\n"
            "/saldo_inicial <monto> USD Efectivo"
        )
        return

    try:
        cuenta_resuelta, anterior = db.set_wallet_balance(perfil, moneda, cuenta, monto, fuente='comando')
    except StorageError as e:
        await update.message.reply_text(f"❌ Error al actualizar el saldo: {e}")
        return

    await update.message.reply_text(
        f"✅ Saldo de {cuenta_resuelta} ({moneda}) actualizado\n"
        f"Antes: {anterior:,.2f}\n"
        f"Ahora: {monto:,.2f}"
    )


def _mes_adyacente(year: int, month: int, delta: int) -> tuple:
    """delta=-1 -> mes anterior, delta=+1 -> mes siguiente."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


async def resumen_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return

    args = context.args
    if args:
        try:
            year, month = map(int, args[0].split('-'))
        except ValueError:
            await _reply(update, "Formato inválido. Use /resumen YYYY-MM, ej: /resumen 2026-06")
            return
    else:
        now = datetime.now()
        year, month = now.year, now.month

    await _send_resumen(update, context, year, month)


async def resumen_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones "◀ Mes anterior" / "Mes siguiente ▶" de /resumen."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    await query.answer()
    try:
        year, month = map(int, (query.data or "").split(":", 1)[-1].split('-'))
    except ValueError:
        return
    await _send_resumen(update, context, year, month)


async def _send_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE, year: int, month: int) -> None:
    """Lógica compartida por /resumen y los botones de navegación de mes:
    arma el texto (con botones ◀/▶) y manda una imagen por cada moneda con
    gastos ese mes."""
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)

    last_day = calendar.monthrange(year, month)[1]
    fecha_desde = f"{year:04d}-{month:02d}-01"
    fecha_hasta = f"{year:04d}-{month:02d}-{last_day:02d}"

    try:
        summaries = db.get_summary(perfil, fecha_desde, fecha_hasta)  # dict por moneda
        bcv = fx.get_bcv_rate(db)
        binance = fx.get_binance_rate(db)
    except StorageError as e:
        await _reply(update, f"❌ Error al calcular el resumen: {e}")
        return

    prev_year, prev_month = _mes_adyacente(year, month, -1)
    next_year, next_month = _mes_adyacente(year, month, 1)
    nav_keyboard = _resumen_nav_keyboard(prev_year, prev_month, next_year, next_month)

    tiene_movimientos = any(
        s['total_gastos'] > 0 or s['total_ingresos'] > 0 for s in summaries.values()
    )
    if not tiene_movimientos:
        await _reply(
            update,
            f"No hay movimientos registrados en {MESES_ES[month].capitalize()} {year}.",
            reply_markup=nav_keyboard,
        )
        return

    mes_label = f"{MESES_ES[month].capitalize()} {year}"
    lines = [f"📊 Resumen {mes_label}\n"]
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

    await _reply(update, '\n'.join(lines), reply_markup=nav_keyboard)

    for moneda, summary in summaries.items():
        if summary['total_gastos'] <= 0:
            continue
        try:
            image_bytes = render_monthly_report(summary, moneda=moneda, year=year, month=month)
            await _reply_photo(update, image_bytes, caption=f"📊 {moneda} — {mes_label}")
        except Exception as e:
            logger.warning(f"No se pudo generar la imagen de resumen ({moneda}) [{perfil}]: {e}")


async def exportar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respaldo manual: genera un CSV con todo el historial de gastos/ingresos
    de este perfil y lo envía como documento de Telegram. Útil como copia de
    seguridad ante cualquier problema con la base de datos del servidor."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)

    try:
        transacciones = db.get_all_transactions(perfil)
    except StorageError as e:
        await _reply(update, f"❌ Error al generar su respaldo: {e}")
        return

    if not transacciones:
        await _reply(update, "Todavía no tiene movimientos registrados, así que no hay nada que exportar.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["fecha", "tipo", "monto", "moneda", "cuenta", "categoria", "descripcion", "registrado_el"])
    for t in transacciones:
        writer.writerow([
            t["fecha"], t["tipo"], f"{t['monto']:.2f}", t["moneda"],
            t["cuenta"], t["categoria"], t["descripcion"] or "", t["created_at"],
        ])

    data_bytes = buffer.getvalue().encode("utf-8-sig")  # BOM para que Excel lea bien los acentos
    hoy = datetime.now().strftime("%Y-%m-%d")
    nombre_archivo = f"coco_respaldo_{hoy}.csv"

    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_document(
            document=io.BytesIO(data_bytes),
            filename=nombre_archivo,
            caption=f"🐊 Aquí tiene su respaldo: {len(transacciones)} movimiento{'s' if len(transacciones) != 1 else ''}.",
        )


def _formatear_deshecho(deshecho: dict) -> str:
    emoji = "💸" if deshecho['tipo'] == 'gasto' else "💵"
    tipo_str = "gasto" if deshecho['tipo'] == 'gasto' else "ingreso"
    simbolo = MONEDA_SIMBOLO.get(deshecho['moneda'], '')
    return (
        f"🐊 Listo, deshecho.\n\n"
        f"{emoji} Ese {tipo_str} de {simbolo} {deshecho['monto']:.2f} {deshecho['moneda']} "
        f"({deshecho['categoria']}, {deshecho['fecha']}) ya no cuenta.\n\n"
        f"💰 Saldo en {deshecho['cuenta']} ({deshecho['moneda']}): {deshecho['nuevo_balance']:,.2f}"
    )


async def deshacer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revierte el último gasto/ingreso registrado por este perfil (por si el
    LLM entendió mal un monto o categoría). No deshace ajustes de saldo
    directos ni transferencias entre billeteras propias -- solo gasto/ingreso."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)

    try:
        deshecho = db.delete_last_transaction(perfil)
    except StorageError as e:
        await _reply(update, f"❌ Error al deshacer: {e}")
        return

    if deshecho is None:
        await _reply(update, "No tengo ningún gasto o ingreso reciente suyo que deshacer.")
        return

    await _reply(update, _formatear_deshecho(deshecho))


async def deshacer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón "↩️ Deshacer" bajo cada confirmación de gasto/ingreso -- misma
    lógica que /deshacer, pero disparada desde el botón. Siempre deshace el
    ÚLTIMO movimiento del perfil (igual que el comando), no necesariamente
    el que muestra el mensaje donde se apretó el botón, si ya se registró
    algo más nuevo después."""
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)

    try:
        deshecho = db.delete_last_transaction(perfil)
    except StorageError as e:
        await query.answer(f"Error al deshacer: {e}", show_alert=True)
        return

    if deshecho is None:
        await query.answer("No hay nada reciente que deshacer.", show_alert=True)
        return

    await query.answer("Deshecho.")
    try:
        texto_original = query.message.text or ""
        await query.edit_message_text(f"{texto_original}\n\n↩️ Deshecho.")
    except Exception:
        # Si no se pudo editar (mensaje muy viejo, etc.), igual mandamos la confirmación aparte.
        await _reply(update, _formatear_deshecho(deshecho))


async def presupuesto_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/presupuesto sin argumentos -> lista los presupuestos fijados.
    /presupuesto <categoria> <monto> -> fija (o reemplaza) el límite mensual
    en Bs de esa categoría. /presupuesto <categoria> 0 -> lo elimina.
    Los límites son en Bs porque es la moneda del gasto del día a día."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    categories = context.bot_data.get('categories', [])
    args = context.args

    if not args:
        presupuestos = db.get_all_budgets(perfil)
        if not presupuestos:
            await _reply(
                update,
                "No tiene presupuestos fijados todavía.\n\n"
                "Uso: /presupuesto <categoría> <monto en Bs>\n"
                "Ej: /presupuesto Delivery 500\n"
                "Para quitarlo: /presupuesto Delivery 0"
            )
            return
        hoy_str = datetime.now().strftime("%Y-%m-%d")
        lines = ["📊 Sus presupuestos mensuales (Bs):\n"]
        for p in presupuestos:
            estado = db.get_budget_status(perfil, p['categoria'], hoy_str)
            gastado = estado['gastado'] if estado else 0.0
            lines.append(f"• {p['categoria']}: {gastado:,.2f} / {p['limite_mensual']:,.2f}")
        await _reply(update, '\n'.join(lines))
        return

    if len(args) < 2:
        await _reply(update, "Uso: /presupuesto <categoría> <monto en Bs>\nEj: /presupuesto Delivery 500")
        return

    *categoria_parts, monto_str = args
    categoria_arg = ' '.join(categoria_parts)
    try:
        monto = float(monto_str.replace(',', '.'))
    except ValueError:
        await _reply(update, "❌ El monto debe ser un número, ej: /presupuesto Delivery 500")
        return

    categoria = next((c for c in categories if c.lower() == categoria_arg.lower()), categoria_arg)

    try:
        db.set_budget(perfil, categoria, monto)
    except StorageError as e:
        await _reply(update, f"❌ Error al fijar el presupuesto: {e}")
        return

    if monto <= 0:
        await _reply(update, f"🐊 Listo, ya no tiene un límite fijado para {categoria}.")
    else:
        await _reply(update, f"🐊 Anotado: {categoria} tiene un tope de {monto:,.2f} Bs al mes. Le avisaré si se acerca.")


async def diezmo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el diezmo pendiente (10% acumulado automáticamente sobre cada
    ingreso registrado), por moneda. Solo informativo -- no toca ninguna
    billetera ni el saldo mostrado en /saldo (ver TithesMixin)."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    pendientes = db.get_tithe_status(perfil)
    if not pendientes:
        await _reply(update, "🐊 No tiene diezmo pendiente por ahora.")
        return
    lines = ["🙏 Diezmo pendiente (10% de sus ingresos):\n"]
    for p in pendientes:
        simbolo = MONEDA_SIMBOLO.get(p['moneda'], '')
        lines.append(f"• {p['moneda']}: {simbolo} {p['monto_pendiente']:,.2f}")
    lines.append("\nCuando lo pague, dígame \"ya pagué el diezmo\" (o use /diezmo_pagado) y lo dejo en cero.")
    await _reply(update, '\n'.join(lines))


async def diezmo_pagado_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/diezmo_pagado [moneda] -- marca como pagado el diezmo pendiente: de
    UNA moneda si se indica, o de todas las que tengan pendiente si no.
    Equivalente por comando a decirle "ya pagué el diezmo" por texto/voz/foto."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    args = context.args
    moneda = None
    if args:
        moneda_arg = args[0].strip().upper()
        moneda_map = {"BS": "Bs", "USD": "USD", "COP": "COP"}
        moneda = moneda_map.get(moneda_arg)
        if not moneda:
            await _reply(update, "❌ Moneda inválida. Use Bs, USD o COP (o déjelo vacío para pagar todo).")
            return
    pagados = db.mark_tithe_paid(perfil, moneda)
    await _reply(update, format_diezmo_pagado_message(pagados))


async def deudas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el resumen neto de deudas/préstamos informales (quién le debe
    a usted y a quién le debe usted), netedado por persona y moneda -- ver
    DebtsMixin.resumen_deudas. No toca ninguna billetera (es solo un
    registro informativo de "quién le debe a quién")."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    resumen = db.resumen_deudas(perfil)
    if not resumen:
        await _reply(update, "🐊 No tiene deudas ni préstamos pendientes registrados.")
        return
    lines = ["🤝 Deudas y préstamos pendientes:\n"]
    for persona, monedas in resumen.items():
        lines.append(f"👤 {persona}")
        for moneda, neto in monedas.items():
            simbolo = MONEDA_SIMBOLO.get(moneda, '')
            if neto > 0:
                lines.append(f"   ↳ le debe a usted: {simbolo} {neto:,.2f} {moneda}")
            else:
                lines.append(f"   ↳ usted le debe: {simbolo} {abs(neto):,.2f} {moneda}")
        lines.append("")
    await _reply(update, '\n'.join(lines).rstrip())


async def racha_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra la racha de días consecutivos registrando movimientos (hasta
    ahora solo visible dentro de /saldo)."""
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    racha = db.get_current_streak(perfil)
    if racha <= 0:
        await _reply(update, "🐊 Todavía no tiene una racha activa -- hoy es un buen día para empezar una.")
        return
    dia_str = "día" if racha == 1 else "días"
    await _reply(update, f"🔥 Lleva {racha} {dia_str} seguido{'s' if racha != 1 else ''} registrando con Coco. Así se hace.")


def _es_dueno(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True solo si quien escribe es el dueño del bot (OWNER_USER_ID en
    .env). /bloquear, /desbloquear y /bloqueados son SOLO para el dueño --
    con el bot en acceso abierto, nadie más debe poder bloquear a otros."""
    owner_id = context.bot_data.get('owner_user_id')
    return owner_id is not None and update.effective_user.id == owner_id


async def bloquear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bloquear <user_id> -- antes de bloquear, muestra el nombre que Coco
    tiene registrado para ese user_id (ver known_users/track_user) y pide
    confirmación con botones, para no bloquear a la persona equivocada por
    escribir mal un número."""
    if not _es_dueno(update, context):
        return
    db: DBClient = context.bot_data['db']
    args = context.args if hasattr(context, 'args') else []
    if not args or not args[0].isdigit():
        await _reply(update, "Uso: /bloquear <user_id>\nEj: /bloquear 123456789\n"
                              "Si no sabe el ID, revise /bloqueados o el aviso que le llegó cuando esa persona escribió por primera vez.")
        return
    user_id = int(args[0])
    if user_id == update.effective_user.id:
        await _reply(update, "🐊 Ese es su propio user_id -- no puede bloquearse a sí mismo.")
        return
    conocido = db.get_known_user(user_id)
    nombre = conocido["nombre"] if conocido else "(nunca le ha escrito a Coco)"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí, bloquear", callback_data=f"coco_bloquear:si:{user_id}"),
        InlineKeyboardButton("❌ Cancelar", callback_data="coco_bloquear:no"),
    ]])
    await _reply(
        update,
        f"¿Bloquear a *{nombre}* (user_id {user_id})?\nNo podrá volver a usar el bot hasta que lo desbloquee.",
        reply_markup=keyboard, parse_mode="Markdown",
    )


async def bloquear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _es_dueno(update, context):
        return
    query = update.callback_query
    await query.answer()
    partes = query.data.split(":")
    if partes[1] == "no":
        await query.edit_message_text("🐊 Cancelado, no bloqueé a nadie.")
        return
    user_id = int(partes[2])
    db: DBClient = context.bot_data['db']
    conocido = db.get_known_user(user_id)
    nombre = conocido["nombre"] if conocido else str(user_id)
    db.block_user(user_id, nombre, blocked_by=update.effective_user.id)
    await query.edit_message_text(f"🚫 Bloqueado: {nombre} (user_id {user_id}) ya no puede usar a Coco.")


async def desbloquear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _es_dueno(update, context):
        return
    db: DBClient = context.bot_data['db']
    args = context.args if hasattr(context, 'args') else []
    if not args or not args[0].isdigit():
        await _reply(update, "Uso: /desbloquear <user_id>")
        return
    user_id = int(args[0])
    if db.unblock_user(user_id):
        await _reply(update, f"🐊 Listo, {user_id} ya puede volver a usar el bot.")
    else:
        await _reply(update, f"🐊 {user_id} no estaba bloqueado.")


async def bloqueados_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _es_dueno(update, context):
        return
    db: DBClient = context.bot_data['db']
    bloqueados = db.list_blocked()
    if not bloqueados:
        await _reply(update, "🐊 No tiene a nadie bloqueado.")
        return
    lines = ["🚫 Usuarios bloqueados:"]
    for b in bloqueados:
        lines.append(f"- {b['nombre']} (user_id {b['user_id']})")
    await _reply(update, '\n'.join(lines))
