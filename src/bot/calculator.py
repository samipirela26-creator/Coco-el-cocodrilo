"""Calculadora inline en el chat para meter dinero a mano (pedido explícito
del usuario, 2026-07-14): divisas arriba (Bs/USD/COP), teclado tipo
calculadora abajo (dígitos, coma decimal, + − × ÷, C, ⌫, =) y un toggle
Gasto/Ingreso. Todo el formato numérico usa el sistema venezolano
(miles con punto, decimales con coma: 1.234,56).

El motor aritmético (arriba) es puro y sin dependencias de Telegram, para
poder testearlo aislado (ver tests/test_calculator.py). Los handlers (abajo)
solo traducen pulsaciones de botones a operaciones sobre el estado, que vive
en context.user_data['calc'].

Flujo: /calculadora (o botón 🧮) -> teclear monto + elegir divisa/tipo ->
✅ Registrar -> elegir categoría (gasto) u origen (ingreso), con un botón
"✏️ Otra…" para escribir una categoría propia -> se guarda como cualquier
gasto/ingreso (ver handlers._save_and_confirm). Las divisas de la calculadora
son Bs/USD/COP en efectivo (USD/COP -> Efectivo, Bs -> BDV); Binance sigue por
texto/foto porque es la excepción, no el caso común.
"""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger('gastos-bot')

MAX_DIGITS = 15  # tope de dígitos de la parte entera, evita overflow visual

# Operadores: token ASCII en el callback_data (× ÷ no viajan bien) -> símbolo
# mostrado.
OPERADORES = {'add': '+', 'sub': '−', 'mul': '×', 'div': '÷'}


# ---------------------------------------------------------------------- #
# Motor aritmético puro (sin Telegram) -- testeable aislado
# ---------------------------------------------------------------------- #

def nuevo_estado(moneda: str = 'Bs', tipo: str = 'gasto') -> dict:
    """Estado inicial de una sesión de calculadora."""
    return {
        'entry': '0',       # número que se está tecleando ('.' como decimal)
        'stored': None,     # operando izquierdo acumulado (float) o None
        'op': None,         # operador pendiente ('add'/'sub'/'mul'/'div')
        'overwrite': True,  # el próximo dígito reemplaza 'entry' en vez de anexar
        'moneda': moneda,
        'tipo': tipo,
        'error': None,      # mensaje de error transitorio (ej. división por cero)
    }


def _agrupar_miles(digitos: str) -> str:
    """'1234567' -> '1.234.567' (separador de miles venezolano)."""
    digitos = digitos.lstrip('0') or '0'
    partes = []
    while len(digitos) > 3:
        partes.insert(0, digitos[-3:])
        digitos = digitos[:-3]
    partes.insert(0, digitos)
    return '.'.join(partes)


def formatear_entry(entry: str) -> str:
    """Convierte el 'entry' interno (con '.' decimal) a display venezolano.
    Preserva el estado de tecleo: '12.' -> '12,' (coma esperando decimales)."""
    negativo = entry.startswith('-')
    e = entry[1:] if negativo else entry
    if '.' in e:
        entero, decimal = e.split('.', 1)
        salida = _agrupar_miles(entero) + ',' + decimal
    else:
        salida = _agrupar_miles(e)
    return ('-' if negativo else '') + salida


def formatear_monto(valor: float, decimales: int = 2) -> str:
    """Formatea un número ya calculado en sistema venezolano (1.234,56).
    Recorta a `decimales` posiciones (contexto de dinero)."""
    negativo = valor < 0
    entero = int(abs(valor))
    frac = round(abs(valor) - entero, decimales)
    dec_str = f"{frac:.{decimales}f}".split('.')[1] if decimales else ''
    salida = _agrupar_miles(str(entero))
    if decimales:
        salida += ',' + dec_str
    return ('-' if negativo else '') + salida


def _evaluar(estado: dict) -> None:
    """Aplica el operador pendiente sobre stored y entry, dejando el resultado
    (redondeado a 2 decimales, contexto de dinero) en entry+stored."""
    a = estado['stored'] if estado['stored'] is not None else 0.0
    b = float(estado['entry'])
    op = estado['op']
    if op == 'add':
        r = a + b
    elif op == 'sub':
        r = a - b
    elif op == 'mul':
        r = a * b
    elif op == 'div':
        if b == 0:
            estado['error'] = "No se puede dividir entre cero"
            estado['entry'] = '0'
            estado['stored'] = None
            estado['op'] = None
            estado['overwrite'] = True
            return
        r = a / b
    else:
        return
    r = round(r, 2)
    estado['stored'] = r
    estado['entry'] = _float_a_entry(r)
    estado['overwrite'] = True


def _float_a_entry(valor: float) -> str:
    """float -> string 'entry' interno, sin '.0' sobrante ('12.0' -> '12')."""
    if valor == int(valor):
        return str(int(valor))
    return f"{valor:.2f}".rstrip('0').rstrip('.')


def aplicar_tecla(estado: dict, tecla: str, valor: str = None) -> dict:
    """Aplica una pulsación al estado (lo muta y lo devuelve). `tecla` es uno
    de: 'digito', 'decimal', 'op', 'back', 'clear', 'eq'. Para 'digito' y 'op'
    el argumento `valor` trae el dígito ('0'..'9') o el token de operador."""
    estado['error'] = None

    if tecla == 'clear':
        estado.update(nuevo_estado(estado['moneda'], estado['tipo']))
        return estado

    if tecla == 'digito':
        if estado['overwrite'] or estado['entry'] == '0':
            estado['entry'] = valor
            estado['overwrite'] = False
        else:
            entero = estado['entry'].split('.')[0].lstrip('-')
            if len(entero) >= MAX_DIGITS and '.' not in estado['entry']:
                return estado  # tope alcanzado, ignora
            estado['entry'] += valor
        return estado

    if tecla == 'decimal':
        if estado['overwrite']:
            estado['entry'] = '0.'
            estado['overwrite'] = False
        elif '.' not in estado['entry']:
            estado['entry'] += '.'
        return estado

    if tecla == 'back':
        if estado['overwrite']:
            estado['entry'] = '0'
        else:
            e = estado['entry'][:-1]
            if e in ('', '-'):
                e = '0'
                estado['overwrite'] = True
            estado['entry'] = e
        return estado

    if tecla == 'op':
        if estado['op'] is not None and not estado['overwrite']:
            _evaluar(estado)
            if estado['error']:
                return estado
        else:
            estado['stored'] = float(estado['entry'])
        estado['op'] = valor
        estado['overwrite'] = True
        return estado

    if tecla == 'eq':
        if estado['op'] is not None:
            _evaluar(estado)
            estado['op'] = None
        return estado

    return estado


def valor_actual(estado: dict) -> float:
    """Monto que se registraría si el usuario confirma ahora (redondeado a 2)."""
    return round(float(estado['entry']), 2)


# ---------------------------------------------------------------------- #
# UI de Telegram
# ---------------------------------------------------------------------- #

def _calc_keyboard(estado: dict) -> InlineKeyboardMarkup:
    moneda = estado['moneda']
    tipo = estado['tipo']

    def cur(m):
        etiqueta = f"• {m} •" if moneda == m else m
        return InlineKeyboardButton(etiqueta, callback_data=f"coco_calc:cur:{m}")

    def dig(d):
        return InlineKeyboardButton(d, callback_data=f"coco_calc:d:{d}")

    def op(token):
        return InlineKeyboardButton(OPERADORES[token], callback_data=f"coco_calc:op:{token}")

    tipo_lbl = "💸 Gasto ⇄ Ingreso" if tipo == 'gasto' else "💰 Ingreso ⇄ Gasto"

    return InlineKeyboardMarkup([
        [cur('Bs'), cur('USD'), cur('COP')],
        [dig('7'), dig('8'), dig('9'), op('div')],
        [dig('4'), dig('5'), dig('6'), op('mul')],
        [dig('1'), dig('2'), dig('3'), op('sub')],
        [dig('0'), InlineKeyboardButton(',', callback_data="coco_calc:dec"),
         InlineKeyboardButton('⌫', callback_data="coco_calc:back"), op('add')],
        [InlineKeyboardButton('C', callback_data="coco_calc:clear"),
         InlineKeyboardButton('=', callback_data="coco_calc:eq")],
        [InlineKeyboardButton(tipo_lbl, callback_data="coco_calc:tipo")],
        [InlineKeyboardButton('✅ Registrar', callback_data="coco_calc:ok"),
         InlineKeyboardButton('✖️ Cancelar', callback_data="coco_calc:cancel")],
    ])


def _render_text(estado: dict) -> str:
    tipo_txt = "Gasto" if estado['tipo'] == 'gasto' else "Ingreso"
    display = formatear_entry(estado['entry'])
    op_txt = ""
    if estado['op'] and estado['stored'] is not None:
        op_txt = f"\n_{formatear_monto(estado['stored'])} {OPERADORES[estado['op']]} …_"
    cuerpo = (
        f"🧮 *Calculadora de Coco*\n"
        f"{tipo_txt} · {estado['moneda']}\n\n"
        f"`{display}`{op_txt}"
    )
    if estado['error']:
        cuerpo += f"\n\n⚠️ {estado['error']}"
    return cuerpo


async def _mostrar_calculadora(update: Update, context: ContextTypes.DEFAULT_TYPE, editar: bool = False) -> None:
    estado = context.user_data.setdefault('calc', nuevo_estado())
    texto = _render_text(estado)
    teclado = _calc_keyboard(estado)
    if editar and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                texto, reply_markup=teclado, parse_mode='Markdown'
            )
        except Exception:
            pass  # "message is not modified" al pulsar una tecla sin efecto
        return
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text(texto, reply_markup=teclado, parse_mode='Markdown')


async def calculadora_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/calculadora (y botón 🧮 / botón del menú): abre una calculadora nueva."""
    from src.bot.access import _is_allowed
    if not _is_allowed(update, context):
        return
    context.user_data['calc'] = nuevo_estado()
    await _mostrar_calculadora(update, context, editar=False)


async def calc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Todas las teclas de la calculadora (prefijo coco_calc:)."""
    from src.bot.access import _is_allowed
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    partes = (query.data or "").split(":")
    accion = partes[1] if len(partes) > 1 else ""

    if accion == "cancel":
        context.user_data.pop('calc', None)
        await query.answer()
        try:
            await query.edit_message_text("🐊 Calculadora cerrada.")
        except Exception:
            pass
        return

    if accion == "ok":
        await query.answer()
        await _confirmar_monto(update, context)
        return

    estado = context.user_data.get('calc')
    if estado is None:
        estado = context.user_data['calc'] = nuevo_estado()

    if accion == "cur":
        estado['moneda'] = partes[2]
    elif accion == "tipo":
        estado['tipo'] = 'ingreso' if estado['tipo'] == 'gasto' else 'gasto'
    elif accion == "d":
        aplicar_tecla(estado, 'digito', partes[2])
    elif accion == "dec":
        aplicar_tecla(estado, 'decimal')
    elif accion == "op":
        aplicar_tecla(estado, 'op', partes[2])
    elif accion == "back":
        aplicar_tecla(estado, 'back')
    elif accion == "clear":
        aplicar_tecla(estado, 'clear')
    elif accion == "eq":
        aplicar_tecla(estado, 'eq')

    await query.answer()
    await _mostrar_calculadora(update, context, editar=True)


# ---------------------------------------------------------------------- #
# Confirmación de monto -> selección de categoría/origen
# ---------------------------------------------------------------------- #

def _categoria_keyboard(opciones: list) -> InlineKeyboardMarkup:
    """Botones de categoría (gasto) u origen (ingreso) + un botón "✏️ Otra…"
    para escribir una categoría propia (la "que podamos modificar" que pidió
    el usuario) + cancelar. Prefijo coco_calccat: propio."""
    filas, fila = [], []
    for op in opciones:
        fila.append(InlineKeyboardButton(op, callback_data=f"coco_calccat:{op}"))
        if len(fila) == 2:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    filas.append([InlineKeyboardButton("✏️ Otra…", callback_data="coco_calccat:__otra__")])
    filas.append([InlineKeyboardButton("✖️ Cancelar", callback_data="coco_calccat:__cancel__")])
    return InlineKeyboardMarkup(filas)


async def _confirmar_monto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Al pulsar ✅ Registrar: valida el monto, arma el gasto/ingreso pendiente
    y pide la categoría (gasto) u origen (ingreso)."""
    from src.bot.constants import MONEDA_SIMBOLO, INCOME_SOURCES
    from src.storage.constants import resolve_cuenta
    from datetime import datetime

    estado = context.user_data.get('calc')
    query = update.callback_query
    if estado is None:
        try:
            await query.edit_message_text("🐊 Esa calculadora ya se cerró. Abra otra con /calculadora.")
        except Exception:
            pass
        return

    monto = valor_actual(estado)
    if monto <= 0:
        try:
            await query.edit_message_text(
                "🐊 El monto debe ser mayor que cero. Abra otra con /calculadora.",
            )
        except Exception:
            pass
        context.user_data.pop('calc', None)
        return

    moneda = estado['moneda']
    tipo = estado['tipo']
    cuenta = resolve_cuenta(moneda, None)
    pending = {
        'tipo': tipo,
        'monto': monto,
        'moneda': moneda,
        'cuenta': cuenta,
        'fecha': datetime.now().strftime("%Y-%m-%d"),
        'descripcion': '',
        'categoria': '',
    }
    context.user_data['calc_pending'] = pending
    context.user_data.pop('calc', None)

    simbolo = MONEDA_SIMBOLO.get(moneda, '')
    if tipo == 'gasto':
        opciones = context.bot_data.get('categories', [])
        pregunta = "¿En qué lo gastó?"
    else:
        opciones = INCOME_SOURCES
        pregunta = "¿De dónde vino ese ingreso?"

    texto = (
        f"🧮 {'Gasto' if tipo == 'gasto' else 'Ingreso'} de {simbolo} {formatear_monto(monto)} {moneda}.\n\n"
        f"{pregunta}"
    )
    try:
        await query.edit_message_text(texto, reply_markup=_categoria_keyboard(opciones))
    except Exception:
        pass


async def _registrar_pending(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    """Guarda el gasto/ingreso pendiente reusando el flujo normal. Marca
    `_efectivo_confirmado` para que _save_and_confirm no vuelva a pedir
    categoría (ya la elegimos acá)."""
    from src.bot.access import _perfil_de
    from src.bot.handlers import _save_and_confirm
    from src.utils.exceptions import StorageError
    from src.bot.replies import _reply

    pending['_efectivo_confirmado'] = True
    if not pending.get('descripcion'):
        pending['descripcion'] = pending.get('categoria', '')
    db = context.bot_data['db']
    user_id = update.effective_user.id
    perfil = _perfil_de(user_id, context)
    try:
        await _save_and_confirm(pending, user_id, perfil, db, update, context, fuente='calculadora')
    except StorageError as e:
        await _reply(update, f"❌ Error al guardar en la base de datos.\n🔧 {e}")


async def calc_categoria_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de categoría/origen tras la calculadora (prefijo coco_calccat:)."""
    from src.bot.access import _is_allowed
    query = update.callback_query
    if not _is_allowed(update, context):
        await query.answer()
        return
    valor = (query.data or "").split(":", 1)[-1]
    pending = context.user_data.get('calc_pending')

    if valor == "__cancel__" or not pending:
        context.user_data.pop('calc_pending', None)
        await query.answer()
        try:
            await query.edit_message_text("🐊 Descartado. Abra otra con /calculadora cuando guste.")
        except Exception:
            pass
        return

    if valor == "__otra__":
        await query.answer()
        # Espera el próximo mensaje de texto como nombre de la categoría
        # (ver intercept en handlers.handle_text_message).
        context.user_data['calc_await_categoria'] = context.user_data.pop('calc_pending')
        try:
            await query.edit_message_text("✏️ Escríbame el nombre de la categoría (una palabra o dos):")
        except Exception:
            pass
        return

    await query.answer(valor)
    context.user_data.pop('calc_pending', None)
    pending['categoria'] = valor
    pending['descripcion'] = valor
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _registrar_pending(update, context, pending)
