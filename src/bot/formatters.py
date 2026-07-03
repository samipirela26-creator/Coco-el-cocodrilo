"""Formateo de mensajes: tasas de cambio (con variación), confirmaciones de
gasto/ingreso, ajustes de saldo y transferencias entre billeteras propias.
Ninguna función de este módulo llama a Telegram ni a la base de datos --
solo arman texto a partir de datos ya calculados (fácil de testear aislado).
"""
from src.services import fx
from src.bot.constants import MONEDA_SIMBOLO, CUENTA_EMOJI


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
    cuenta_emoji = CUENTA_EMOJI.get(cuenta, '👛')
    return f"""✅ {tipo_str} registrado

{emoji} Monto: {simbolo} {expense_data['monto']:.2f} {moneda}
📂 Categoría: {expense_data['categoria']}
{cuenta_emoji} Cuenta: {cuenta}
📅 Fecha: {expense_data['fecha']}
📝 {expense_data['descripcion']}

💰 Saldo en {cuenta} ({moneda}): {balance:,.2f}""" \
        + _budget_alert_line(budget_status, expense_data['categoria']) \
        + _coco_line(expense_data.get('respuesta'))


def format_ajuste_message(moneda: str, cuenta: str, anterior: float, nuevo: float, respuesta: str = "") -> str:
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    cuenta_emoji = CUENTA_EMOJI.get(cuenta, '👛')
    return f"""✅ Saldo actualizado

{cuenta_emoji} {cuenta} ({moneda})
Antes: {simbolo} {anterior:,.2f}
Ahora: {simbolo} {nuevo:,.2f}""" + _coco_line(respuesta)


def format_ajuste_preview_message(moneda: str, cuenta: str, anterior: float, nuevo: float, respuesta: str = "") -> str:
    """Vista previa ANTES de guardar un ajuste de saldo (foto o texto/voz tipo
    "tengo X en efectivo") -- pide confirmación en vez de sobrescribir directo,
    porque un OCR/LLM que lee mal un dígito puede corromper el saldo de
    referencia sin que el usuario lo note a tiempo (ver botones en
    _saldo_confirm_keyboard, src/bot/replies.py)."""
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    cuenta_emoji = CUENTA_EMOJI.get(cuenta, '👛')
    return f"""🐊 ¿Confirma actualizar su saldo?

{cuenta_emoji} {cuenta} ({moneda})
Antes: {simbolo} {anterior:,.2f}
Nuevo: {simbolo} {nuevo:,.2f}""" + _coco_line(respuesta)


def format_diezmo_pagado_message(pagados: list, respuesta: str = "") -> str:
    """Confirmación al marcar el diezmo como pagado (ver TithesMixin.mark_tithe_paid).
    `pagados` es una lista de {"moneda", "monto_pagado"} -- vacía si no había nada
    pendiente en ninguna moneda."""
    if not pagados:
        return "🐊 No tenía diezmo pendiente que marcar como pagado." + _coco_line(respuesta)
    lines = ["✅ Diezmo marcado como pagado\n"]
    for p in pagados:
        simbolo = MONEDA_SIMBOLO.get(p['moneda'], '')
        lines.append(f"🙏 {p['moneda']}: {simbolo} {p['monto_pagado']:,.2f}")
    return '\n'.join(lines) + _coco_line(respuesta)


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


def format_deuda_nueva_message(persona: str, tipo_deuda: str, moneda: str, monto: float, respuesta: str = "") -> str:
    """Confirmación al registrar una deuda/préstamo informal nuevo (ver
    DebtsMixin.register_debt). tipo_deuda='prestado' -> la persona le queda
    debiendo a usted; 'pedido' -> usted le queda debiendo a la persona."""
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    if tipo_deuda == 'prestado':
        linea = f"👤 {persona} le debe a usted: {simbolo} {monto:,.2f} {moneda}"
    else:
        linea = f"👤 Usted le debe a {persona}: {simbolo} {monto:,.2f} {moneda}"
    return f"""✅ Deuda registrada

{linea}""" + _coco_line(respuesta)


def format_deuda_pago_message(resultado: dict, persona: str, moneda: str, respuesta: str = "") -> str:
    """Confirmación al aplicar un pago/abono a deudas pendientes (ver
    DebtsMixin.register_payment). `resultado` trae aplicado/sobra/saldadas."""
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    lines = [
        "✅ Pago de deuda registrado\n",
        f"👤 {persona}",
        f"💰 Aplicado: {simbolo} {resultado['aplicado']:,.2f} {moneda}",
    ]
    if resultado['saldadas'] > 0:
        lines.append(f"🎉 Deuda(s) saldada(s): {resultado['saldadas']}")
    if resultado['sobra'] > 0.005:
        lines.append(f"↩️ Sobrante (no había pendiente por esa cantidad): {simbolo} {resultado['sobra']:,.2f}")
    return '\n'.join(lines) + _coco_line(respuesta)


def _barra_progreso(porcentaje: float, ancho: int = 10) -> str:
    llenos = min(int(round(porcentaje / 100 * ancho)), ancho)
    return "🟩" * llenos + "⬜" * (ancho - llenos)


def format_meta_nueva_message(nombre: str, moneda: str, monto_objetivo: float, respuesta: str = "") -> str:
    """Confirmación al crear una meta de ahorro nueva (ver SavingsMixin.create_goal)."""
    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    return f"""✅ Meta de ahorro creada

🎯 {nombre}
Objetivo: {simbolo} {monto_objetivo:,.2f} {moneda}
{_barra_progreso(0)} 0%""" + _coco_line(respuesta)


def format_meta_aporte_message(resultado: dict, respuesta: str = "") -> str:
    """Confirmación al aportar a una meta de ahorro existente (ver
    SavingsMixin.contribute_goal). Si `resultado["encontrada"]` es False, la
    meta no existe -- llamador debe mostrar un mensaje aparte en ese caso."""
    simbolo = MONEDA_SIMBOLO.get(resultado['moneda'], '')
    porcentaje = round(min(resultado['monto_actual'] / resultado['monto_objetivo'], 1.0) * 100, 1) \
        if resultado['monto_objetivo'] > 0 else 0.0
    linea_cumplida = "\n\n🎉 ¡Meta cumplida!" if resultado['cumplida'] else ""
    return f"""✅ Aporte registrado

🎯 {resultado['nombre']}
{simbolo} {resultado['monto_actual']:,.2f} / {resultado['monto_objetivo']:,.2f} {resultado['moneda']}
{_barra_progreso(porcentaje)} {porcentaje}%{linea_cumplida}""" + _coco_line(respuesta)


def format_transferencia_message(resultado: dict, respuesta: str = "", tasa_cambio: float = None) -> str:
    simbolo_o = MONEDA_SIMBOLO.get(resultado['moneda_origen'], '')
    simbolo_d = MONEDA_SIMBOLO.get(resultado['moneda_destino'], '')
    emoji_o = CUENTA_EMOJI.get(resultado['cuenta_origen'], '👛')
    emoji_d = CUENTA_EMOJI.get(resultado['cuenta_destino'], '👛')
    tasa_line = ""
    if tasa_cambio:
        label = _tasa_label(resultado['moneda_origen'], resultado['moneda_destino'])
        tasa_line = f"\n💱 Tasa usada: {float(tasa_cambio):,.2f}" + (f" {label}" if label else "")
    return f"""✅ Transferencia registrada

{emoji_o} {resultado['cuenta_origen']} ({resultado['moneda_origen']})
Antes: {simbolo_o} {resultado['anterior_origen']:,.2f}
Ahora: {simbolo_o} {resultado['nuevo_origen']:,.2f}

{emoji_d} {resultado['cuenta_destino']} ({resultado['moneda_destino']})
Antes: {simbolo_d} {resultado['anterior_destino']:,.2f}
Ahora: {simbolo_d} {resultado['nuevo_destino']:,.2f}{tasa_line}""" + _coco_line(respuesta)
