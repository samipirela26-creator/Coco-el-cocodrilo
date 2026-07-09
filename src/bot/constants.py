"""Constantes de presentación compartidas por los formateadores y comandos
del bot (símbolos de moneda, emojis de cuenta). Separadas en su propio módulo
para que tanto `formatters.py` como `commands.py` las importen sin depender
una del otro.
"""

MONEDA_SIMBOLO = {"Bs": "Bs", "USD": "$", "COP": "$"}
CUENTA_EMOJI = {"BDV": "🏦", "Binance": "💻", "Efectivo": "💵"}

# Orígenes fijos para ingresos en efectivo (ver handlers._pedir_categoria_efectivo):
# a diferencia de EXPENSE_CATEGORIES (configurable en .env), esta lista es fija
# porque el patrón de ingresos en cash de Samuel es simple y no varía por perfil.
INCOME_SOURCES = ["Sueldo/Pago", "Venta", "Regalo", "Reembolso", "Préstamo recibido", "Otro"]
