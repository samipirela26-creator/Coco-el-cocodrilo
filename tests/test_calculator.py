"""Tests del motor aritmético puro de la calculadora (src/bot/calculator.py).
Cubre formato venezolano, tecleo, operaciones encadenadas y división por cero.
No toca Telegram -- solo la lógica de estado."""
from src.bot.calculator import (
    nuevo_estado, aplicar_tecla, valor_actual,
    formatear_entry, formatear_monto,
)


def _teclear(estado, secuencia):
    """Helper: aplica una secuencia de teclas tipo '12+3=' sobre el estado."""
    for ch in secuencia:
        if ch.isdigit():
            aplicar_tecla(estado, 'digito', ch)
        elif ch == ',':
            aplicar_tecla(estado, 'decimal')
        elif ch == '+':
            aplicar_tecla(estado, 'op', 'add')
        elif ch == '-':
            aplicar_tecla(estado, 'op', 'sub')
        elif ch == '*':
            aplicar_tecla(estado, 'op', 'mul')
        elif ch == '/':
            aplicar_tecla(estado, 'op', 'div')
        elif ch == '=':
            aplicar_tecla(estado, 'eq')
    return estado


# ------------------------- formato venezolano ------------------------- #

def test_formatear_entry_miles():
    assert formatear_entry('1234567') == '1.234.567'


def test_formatear_entry_decimales_en_tecleo():
    assert formatear_entry('12.') == '12,'
    assert formatear_entry('12.5') == '12,5'


def test_formatear_monto_dos_decimales():
    assert formatear_monto(1234.5) == '1.234,50'
    assert formatear_monto(1000000) == '1.000.000,00'
    assert formatear_monto(0) == '0,00'


# ----------------------------- tecleo -------------------------------- #

def test_teclear_reemplaza_cero_inicial():
    e = nuevo_estado()
    _teclear(e, '7')
    assert e['entry'] == '7'
    assert valor_actual(e) == 7.0


def test_decimal_desde_cero():
    e = nuevo_estado()
    aplicar_tecla(e, 'decimal')
    aplicar_tecla(e, 'digito', '5')
    assert e['entry'] == '0.5'
    assert valor_actual(e) == 0.5


def test_backspace():
    e = nuevo_estado()
    _teclear(e, '123')
    aplicar_tecla(e, 'back')
    assert e['entry'] == '12'
    aplicar_tecla(e, 'back')
    aplicar_tecla(e, 'back')
    assert e['entry'] == '0'


def test_un_solo_punto_decimal():
    e = nuevo_estado()
    _teclear(e, '1,2')
    aplicar_tecla(e, 'decimal')  # segundo punto: se ignora
    aplicar_tecla(e, 'digito', '3')
    assert e['entry'] == '1.23'


# --------------------------- operaciones ----------------------------- #

def test_suma():
    e = nuevo_estado()
    _teclear(e, '12+3=')
    assert valor_actual(e) == 15.0


def test_resta():
    e = nuevo_estado()
    _teclear(e, '20-8=')
    assert valor_actual(e) == 12.0


def test_multiplicacion():
    e = nuevo_estado()
    _teclear(e, '6*7=')
    assert valor_actual(e) == 42.0


def test_division():
    e = nuevo_estado()
    _teclear(e, '9/2=')
    assert valor_actual(e) == 4.5


def test_operaciones_encadenadas():
    e = nuevo_estado()
    _teclear(e, '10+5+3=')  # 10+5=15, +3 = 18
    assert valor_actual(e) == 18.0


def test_division_por_cero_marca_error():
    e = nuevo_estado()
    _teclear(e, '5/0=')
    assert e['error'] == "No se puede dividir entre cero"
    assert valor_actual(e) == 0.0


def test_clear_reinicia_pero_conserva_moneda_tipo():
    e = nuevo_estado(moneda='USD', tipo='ingreso')
    _teclear(e, '123+4')
    aplicar_tecla(e, 'clear')
    assert e['entry'] == '0'
    assert e['op'] is None
    assert e['moneda'] == 'USD'
    assert e['tipo'] == 'ingreso'


def test_redondeo_a_dos_decimales():
    e = nuevo_estado()
    _teclear(e, '10/3=')  # 3.333... -> 3.33
    assert valor_actual(e) == 3.33
