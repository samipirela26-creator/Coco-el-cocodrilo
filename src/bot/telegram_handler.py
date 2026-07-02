"""Handlers del bot de Telegram.
Adaptado de telegram-bot-gastos-llm: agrega /saldo, /resumen y /saldo_inicial;
además soporta multi-moneda (Bs/USD/COP), billeteras por cuenta
(BDV/Binance/Efectivo), tasas BCV/Binance, categorías dinámicas, capturas de
pantalla (transferencia/saldo, con adopción directa del saldo leído) y notas
de voz.
"""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    user_id = update.effective_user.id
    if user_id in allowed:
        return True
    logger.warning(f"Acceso bloqueado: user_id {user_id} no está en ALLOWED_USER_IDS")
    return False


def _perfil_de(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Resuelve a qué 'perfil' (persona) pertenece este user_id de Telegram --
    cada perfil tiene sus propios saldos/gastos/racha, totalmente aislados de
    los demás (ver src/config.py: USER_PROFILES). Si el user_id no está
    agrupado explícitamente, es su propio perfil aislado por defecto."""
    mapping = context.bot_data.get('user_id_to_profile') or {}
    return mapping.get(user_id, str(user_id))


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


def _welcome_text(nuevo_registro: bool = False) -> str:
    intro = (
        "🐊 Un gusto. Acabo de abrirle su propio libro de cuentas, separado del "
        "de cualquier otra persona que me escriba -- lo que usted me confíe "
        "aquí es solo suyo.\n\n"
        if nuevo_registro else "🐊 Buenas. Aquí su banquero de confianza, a la orden.\n\n"
    )
    return intro + """Cuénteme sus gastos, ingresos o saldos como se los diría a un amigo --
texto, foto de una captura, o nota de voz -- y yo se los anoto.

📝 Ejemplos de gasto/ingreso:
• "Compré pan por 500" (Bs por defecto)
• "Gasté 20 dólares en el super"
• "Pagué 3000 pesos por un almuerzo"
• "Me pagaron el sueldo, 50000"

💰 Ejemplos para declararme cuánto tiene (sin que sea un gasto/ingreso nuevo):
• "Tengo 50 dólares en efectivo"
• "En Binance tengo 200"
• "Me quedan 300 mil bolívares en el BDV"

🔄 Ejemplos para mover dinero entre sus propias billeteras (no es gasto ni ingreso):
• "Moví 50 dólares de Binance a efectivo"
• "Cambié 20 dólares por 3600 pesos y los metí en efectivo"
• "Cambié 100 mil bolívares a dólares en Binance, a 190 el cambio" (me dice la tasa
  y yo le calculo cuánto entra)

También puede enviarme:
📸 Una captura de una transferencia -> la registro como gasto/ingreso
📸 Una captura de su saldo (BDV o Binance) -> actualizo esa billetera directo
🎤 Una nota de voz describiendo el gasto o el saldo

Comandos:
/menu - botones rápidos (saldo, resumen, cambio, ayuda)
/saldo - ver su saldo (BDV, Binance, y Efectivo con USD + COP juntos)
/resumen - ver gastos del mes por categoría (con porcentajes)
/saldo_inicial <monto> [moneda] [cuenta] - configurar el saldo de una billetera
/cambio - ver tasas BCV y Binance
/exportar - descargar un respaldo CSV de todos sus movimientos
/deshacer - revertir el último gasto o ingreso registrado
/presupuesto [categoría] [monto] - fijar o ver topes mensuales por categoría
/help - ver categorías y ayuda"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    await _reply(update, _welcome_text())


async def _maybe_welcome_new_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, perfil: str) -> None:
    """Registro abierto: si es la primera vez que se ve este perfil (nadie lo
    agregó a mano en USER_PROFILES), le manda el saludo de bienvenida UNA
    sola vez, antes de procesar su mensaje/foto/nota de voz con normalidad.
    Cada perfil nuevo queda aislado (sus propios saldos/gastos), no ve nada
    de los demás."""
    db: DBClient = context.bot_data['db']
    if db.is_new_profile(perfil):
        await _reply(update, _welcome_text(nuevo_registro=True))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    categories = context.bot_data.get('categories', [])
    categories_str = '\n• '.join(categories)
    help_message = f"""🐊 Ayuda -- lo que este banquero sabe hacer

📂 Categorías fijas de gasto:
• {categories_str}

(También puedo abrir categorías nuevas solo si el gasto no encaja en ninguna,
ej: "Gasto de Gio", "Comida en la calle".)

👛 Billeteras que administro por usted:
• BDV (Bs) — su cuenta bancaria en bolívares
• Binance (USD) — sus dólares digitales/USDT
• Efectivo — dólares y pesos (COP) en cash, juntos en /saldo con el
  equivalente combinado en cada moneda (tasa fija: {fx.COP_PER_USD:,.0f} COP = 1 USD)

📝 Cómo usarme:
Envíeme un mensaje, foto o nota de voz describiendo el gasto, ingreso o saldo, por ejemplo:
• "Compré X por Y" / "Gasté Z en [categoría]" / "Cobré Z de [fuente]"
• "Tengo Z dólares en efectivo" / "En Binance tengo Z" -> actualizo esa billetera directo
• "Moví Z de Binance a efectivo" / "Cambié Z dólares por W pesos" -> transferencia entre
  sus propias billeteras (no cuenta como gasto ni ingreso en /resumen)
Si no menciona moneda, asumo Bs. Puede decir "20 dólares" o "3000 pesos" para USD/COP.
En USD, si no menciona Binance/USDT/cripto, asumo que es Efectivo.

Comandos:
/menu - botones rápidos (saldo, resumen, cambio, ayuda), también escribiendo "menu"
/saldo - su saldo (BDV, Binance, Efectivo), con conversión BCV/Binance de sus Bs
/resumen [mes] - resumen y % de gasto por categoría del mes actual (o YYYY-MM)
/saldo_inicial <monto> [moneda] [cuenta] - fija el saldo de una billetera
  (moneda: Bs/USD/COP; cuenta obligatoria si moneda es USD: Binance o Efectivo)
/cambio - tasas BCV, Binance y USD->COP
/exportar - descargar un CSV con todo su historial (respaldo manual)
/deshacer - revierte el último gasto/ingreso, por si algo se registró mal
/presupuesto <categoría> <monto en Bs> - fija un tope mensual; sin argumentos, lo lista
  (le aviso en la confirmación del gasto si va llegando al 80% o ya lo superó)"""
    await _reply(update, help_message)


def _rate_variation_pct(rate: float, previous: float) -> float:
    """None si no hay valor previo para comparar (cache vigente, o primera
    vez) o si el previo es 0 (evita división entre cero)."""
    if previous is None or previous == 0:
        return None
    return (rate - previous) / previous * 100


def _variation_note(pct: float) -> str:
    """Solo avisa si el movimiento es relevante (>=3%) -- para no hacer ruido
    con variaciones normales del día a día."""
    if pct is None or abs(pct) < 3:
        return ""
    flecha = "🔺" if pct > 0 else "🔻"
    return f" ({flecha} {abs(pct):.1f}% desde la última consulta)"


def _format_rates_block(bcv: float, binance: float, bcv_var: float = None, binance_var: float = None) -> str:
    return (
        f"🏦 BCV: {bcv:,.2f} Bs/USD{_variation_note(bcv_var)}\n"
        f"💵 Binance: {binance:,.2f} Bs/USD{_variation_note(binance_var)}\n"
        f"🌎 USD -> COP: {fx.COP_PER_USD:,.0f} (tasa fija)"
    )


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

        racha = db.get_current_streak(perfil)
        if racha > 0:
            lines.append("")
            lines.append(f"🔥 Racha: {racha} día{'s' if racha != 1 else ''} seguido{'s' if racha != 1 else ''} registrando")

        lines.append("")
        lines.append(_format_rates_block(bcv, binance, bcv_var, binance_var))

        await _reply(update, '\n'.join(lines))
    except StorageError as e:
        await _reply(update, f"❌ Error al consultar el saldo: {e}")


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


async def resumen_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    db: DBClient = context.bot_data['db']
    perfil = _perfil_de(update.effective_user.id, context)
    from datetime import datetime
    import calendar

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

    tiene_movimientos = any(
        s['total_gastos'] > 0 or s['total_ingresos'] > 0 for s in summaries.values()
    )
    if not tiene_movimientos:
        await _reply(update, f"No hay movimientos registrados en {year:04d}-{month:02d}.")
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

    await _reply(update, '\n'.join(lines))


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

    import csv
    import io as _io
    buffer = _io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["fecha", "tipo", "monto", "moneda", "cuenta", "categoria", "descripcion", "registrado_el"])
    for t in transacciones:
        writer.writerow([
            t["fecha"], t["tipo"], f"{t['monto']:.2f}", t["moneda"],
            t["cuenta"], t["categoria"], t["descripcion"] or "", t["created_at"],
        ])

    data_bytes = buffer.getvalue().encode("utf-8-sig")  # BOM para que Excel lea bien los acentos
    from datetime import datetime as _datetime
    hoy = _datetime.now().strftime("%Y-%m-%d")
    nombre_archivo = f"coco_respaldo_{hoy}.csv"

    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_document(
            document=_io.BytesIO(data_bytes),
            filename=nombre_archivo,
            caption=f"🐊 Aquí tiene su respaldo: {len(transacciones)} movimiento{'s' if len(transacciones) != 1 else ''}.",
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

    emoji = "💸" if deshecho['tipo'] == 'gasto' else "💵"
    tipo_str = "gasto" if deshecho['tipo'] == 'gasto' else "ingreso"
    simbolo = MONEDA_SIMBOLO.get(deshecho['moneda'], '')
    await _reply(
        update,
        f"🐊 Listo, deshecho.\n\n"
        f"{emoji} Ese {tipo_str} de {simbolo} {deshecho['monto']:.2f} {deshecho['moneda']} "
        f"({deshecho['categoria']}, {deshecho['fecha']}) ya no cuenta.\n\n"
        f"💰 Saldo en {deshecho['cuenta']} ({deshecho['moneda']}): {deshecho['nuevo_balance']:,.2f}"
    )


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
        from datetime import datetime as _datetime
        hoy_str = _datetime.now().strftime("%Y-%m-%d")
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


# ---------------------------------------------------------------------- #
# Guardado compartido (texto, voz, y fotos de transferencia)
# ---------------------------------------------------------------------- #

def _coco_line(respuesta: str) -> str:
    """Linea de cierre con la voz de Coco, si el modelo mando una."""
    respuesta = (respuesta or "").strip()
    return f"\n\n🐊 {respuesta}" if respuesta else ""


def _budget_alert_line(estado: dict, categoria: str) -> str:
    """Linea corta (tono de banquero, sin alarmismo) cuando un gasto acerca o
    supera el presupuesto mensual de su categoria. None/"" si no aplica."""
    if not estado or estado['limite_mensual'] <= 0:
        return ""
    porcentaje = estado['porcentaje']
    if porcentaje >= 100:
        return (
            f"\n\n📊 Este mes ya superó su presupuesto de {categoria} "
            f"({estado['gastado']:,.2f} de {estado['limite_mensual']:,.2f} Bs)."
        )
    if porcentaje >= 80:
        return (
            f"\n\n📊 Va en {porcentaje:.0f}% de su presupuesto de {categoria} este mes "
            f"({estado['gastado']:,.2f} de {estado['limite_mensual']:,.2f} Bs)."
        )
    return ""


def format_confirmation_message(expense_data: dict, balance: float, cuenta: str, budget_status: dict = None) -> str:
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

💰 Saldo en {cuenta} ({moneda}): {balance:,.2f}""" \
        + _budget_alert_line(budget_status, expense_data['categoria']) \
        + _coco_line(expense_data.get('respuesta'))


def format_ajuste_message(moneda: str, cuenta: str, anterior: float, nuevo: float, respuesta: str = "") -> str:
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    return f"""✅ Saldo actualizado

👛 {cuenta} ({moneda})
Antes: {simbolo} {anterior:,.2f}
Ahora: {simbolo} {nuevo:,.2f}""" + _coco_line(respuesta)


def _tasa_label(moneda_origen: str, moneda_destino: str) -> str:
    """Ej: 'Bs/USD' si el par es Bs<->USD, para que la tasa mostrada en la
    confirmación tenga unidades claras (misma convención que /cambio: cuántos
    Bs o COP equivalen a 1 USD)."""
    monedas = {moneda_origen, moneda_destino}
    if 'USD' in monedas:
        otra = monedas - {'USD'}
        if otra:
            return f"{next(iter(otra))}/USD"
    return ""


def format_transferencia_message(resultado: dict, respuesta: str = "", tasa_cambio: float = None) -> str:
    simbolo_o = MONEDA_SIMBOLO.get(resultado['moneda_origen'], '')
    simbolo_d = MONEDA_SIMBOLO.get(resultado['moneda_destino'], '')
    tasa_line = ""
    if tasa_cambio:
        label = _tasa_label(resultado['moneda_origen'], resultado['moneda_destino'])
        tasa_line = f"\n💱 Tasa usada: {float(tasa_cambio):,.2f}" + (f" {label}" if label else "")
    return f"""✅ Transferencia registrada

👛 {resultado['cuenta_origen']} ({resultado['moneda_origen']})
Antes: {simbolo_o} {resultado['anterior_origen']:,.2f}
Ahora: {simbolo_o} {resultado['nuevo_origen']:,.2f}

👛 {resultado['cuenta_destino']} ({resultado['moneda_destino']})
Antes: {simbolo_d} {resultado['anterior_destino']:,.2f}
Ahora: {simbolo_d} {resultado['nuevo_destino']:,.2f}{tasa_line}""" + _coco_line(respuesta)


async def _save_and_confirm(data: dict, user_id: int, perfil: str, db: DBClient, update: Update,
                             prefix: str = "", fuente: str = 'texto') -> None:
    """Guarda una transacción (gasto/ingreso), un ajuste de saldo, o una
    transferencia entre billeteras propias ya parseada y validada, en los
    datos DE ESTE PERFIL (aislados de cualquier otro), y responde con la
    confirmación correspondiente. Compartido por texto, voz y fotos de
    transferencia (a un tercero -- distinto de "transferencia" tipo, que es
    entre billeteras propias)."""
    moneda = data.get('moneda', 'Bs')

    if data['tipo'] == 'ajuste_saldo':
        cuenta_resuelta, anterior = db.set_wallet_balance(perfil, moneda, data.get('cuenta'), data['monto'], fuente=fuente)
        mensaje = format_ajuste_message(moneda, cuenta_resuelta, anterior, data['monto'], data.get('respuesta'))
        await update.message.reply_text(f"{prefix}{mensaje}")
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
        await update.message.reply_text(f"{prefix}{mensaje}")
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
    if user_message.strip().lower() in ("menu", "menú", "m"):
        await menu_command(update, context)
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
            await update.message.reply_text(
                f"❌ {error_message}\n\n💡 Intente reformular con monto y categoría claros."
            )
            return

        if expense_data.get('tipo') == 'charla':
            await update.message.reply_text(expense_data.get('respuesta') or "🐊 ¿En qué le ayudo?")
            return

        await _save_and_confirm(expense_data, user_id, perfil, db, update, fuente='texto')

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
      - "saldo": ADOPTA directo el saldo leído como la billetera correspondiente
        (BDV si es Bs, Binance/Efectivo si es USD según la app detectada) --
        sin pedir confirmación, pero mostrando antes/después para que el
        usuario note al toque si el OCR leyó mal algo (ver db.set_wallet_balance).
    """
    if not _is_allowed(update, context):
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
        data = llm_connector.analyze_image(image_bytes, categories, dynamic_categories)
        logger.debug(f"Datos extraídos de la imagen: {data}")

        captura_tipo = data.get('captura_tipo')
        moneda = data.get('moneda', 'Bs')

        if captura_tipo == 'saldo':
            monto_banco = float(data.get('monto', 0))
            cuenta_resuelta, anterior = db.set_wallet_balance(perfil, moneda, data.get('cuenta'), monto_banco, fuente='foto')
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

        await _save_and_confirm(data, user_id, perfil, db, update, prefix="📸 Captura de transferencia detectada.\n\n", fuente='foto')

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
            await update.message.reply_text(
                f"❌ No pude entender la nota de voz: {error_message}"
            )
            return

        if data.get('tipo') == 'charla':
            await update.message.reply_text(data.get('respuesta') or "🐊 ¿En qué le ayudo?")
            return

        await _save_and_confirm(data, user_id, perfil, db, update, prefix="🎤 Nota de voz procesada.\n\n", fuente='audio')

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
