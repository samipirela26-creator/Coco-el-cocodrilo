"""Validadores para datos de gastos/ingresos/ajustes de saldo. Adaptado de
telegram-bot-gastos-llm."""
from datetime import datetime
from typing import Tuple

MONEDAS_VALIDAS = ('Bs', 'USD', 'COP')
CUENTAS_VALIDAS = ('BDV', 'Binance', 'Efectivo')


def validate_expense_data(data: dict, valid_categories: list = None) -> Tuple[bool, str]:
    """
    Valida los datos de una transacción (gasto, ingreso o ajuste_saldo).

    Las categorías ahora pueden ser dinámicas (propuestas por el LLM), así que
    ya no se rechaza una categoría solo por no estar en `valid_categories`:
    solo se valida que sea un string no vacío (y solo para gasto/ingreso;
    ajuste_saldo no requiere categoría). `valid_categories` se deja como
    parámetro por compatibilidad, pero no se usa para rechazar.

    Args:
        data: Diccionario con tipo, monto, fecha, descripcion, categoria
              (gasto/ingreso), moneda y cuenta (opcionales)
        valid_categories: (sin efecto restrictivo, se mantiene por compatibilidad)

    Returns:
        Tupla (es_valido, mensaje_error)
    """
    tipo = data.get('tipo')
    if tipo not in ('gasto', 'ingreso', 'ajuste_saldo', 'charla', 'transferencia'):
        return False, "El campo 'tipo' debe ser 'gasto', 'ingreso', 'ajuste_saldo', 'transferencia' o 'charla'"

    if tipo == 'charla':
        return True, ""

    if tipo == 'transferencia':
        return _validate_transferencia(data)

    required_fields = ['monto', 'fecha', 'descripcion']
    if tipo != 'ajuste_saldo':
        required_fields.append('categoria')
    for field in required_fields:
        if field not in data:
            return False, f"Campo requerido '{field}' no encontrado"

    try:
        monto = float(data['monto'])
        if monto <= 0:
            return False, "El monto debe ser mayor que 0"
    except (ValueError, TypeError):
        return False, "El monto debe ser un número válido"

    categoria = data.get('categoria', '')
    if tipo == 'gasto' and (not categoria or not isinstance(categoria, str)):
        return False, "La categoría no puede estar vacía"

    try:
        datetime.strptime(data['fecha'], '%Y-%m-%d')
    except ValueError:
        return False, "La fecha debe tener formato YYYY-MM-DD"

    moneda = data.get('moneda', 'Bs')
    if moneda not in MONEDAS_VALIDAS:
        return False, f"La moneda '{moneda}' no es válida. Use una de: {', '.join(MONEDAS_VALIDAS)}"

    cuenta = data.get('cuenta')
    if cuenta and cuenta not in CUENTAS_VALIDAS:
        return False, f"La cuenta '{cuenta}' no es válida. Use una de: {', '.join(CUENTAS_VALIDAS)}"

    return True, ""


def _validate_transferencia(data: dict) -> Tuple[bool, str]:
    """Valida una transferencia entre billeteras propias (misma persona, dos
    bolsillos distintos, ej. Binance -> Efectivo, o un cambio de divisa
    dólares -> pesos). No usa 'monto'/'moneda'/'cuenta' genéricos, sino los
    campos _origen/_destino."""
    required_fields = [
        'moneda_origen', 'cuenta_origen', 'monto_origen',
        'moneda_destino', 'cuenta_destino', 'monto_destino',
        'fecha', 'descripcion',
    ]
    for field in required_fields:
        if field not in data:
            return False, f"Campo requerido '{field}' no encontrado"

    try:
        monto_origen = float(data['monto_origen'])
        monto_destino = float(data['monto_destino'])
        if monto_origen <= 0 or monto_destino <= 0:
            return False, "Los montos de la transferencia deben ser mayores que 0"
    except (ValueError, TypeError):
        return False, "Los montos de la transferencia deben ser números válidos"

    for campo_moneda in ('moneda_origen', 'moneda_destino'):
        if data.get(campo_moneda) not in MONEDAS_VALIDAS:
            return False, f"La moneda '{data.get(campo_moneda)}' no es válida. Use una de: {', '.join(MONEDAS_VALIDAS)}"

    for campo_cuenta in ('cuenta_origen', 'cuenta_destino'):
        cuenta = data.get(campo_cuenta)
        if cuenta and cuenta not in CUENTAS_VALIDAS:
            return False, f"La cuenta '{cuenta}' no es válida. Use una de: {', '.join(CUENTAS_VALIDAS)}"

    try:
        datetime.strptime(data['fecha'], '%Y-%m-%d')
    except ValueError:
        return False, "La fecha debe tener formato YYYY-MM-DD"

    tasa_cambio = data.get('tasa_cambio')
    if tasa_cambio not in (None, '', 0):
        try:
            if float(tasa_cambio) <= 0:
                return False, "La tasa de cambio debe ser mayor que 0"
        except (ValueError, TypeError):
            return False, "La tasa de cambio debe ser un número válido"

    return True, ""
