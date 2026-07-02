"""Formateo de mensajes: tasas de cambio (con variación), confirmaciones de
gasto/ingreso, ajustes de saldo y transferencias entre billeteras propias.
Ninguna función de este módulo llama a Telegram ni a la base de datos --
solo arman texto a partir de datos ya calculados (fácil de testear aislado).
"""
from src.services import fx
from src.bot.constants import MONEDA_SIMBOLO


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
