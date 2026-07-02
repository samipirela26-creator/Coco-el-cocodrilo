"""Constantes de presentación compartidas por los formateadores y comandos
del bot (símbolos de moneda, emojis de cuenta). Separadas en su propio módulo
para que tanto `formatters.py` como `commands.py` las importen sin depender
una del otro.
"""

MONEDA_SIMBOLO = {"Bs": "Bs", "USD": "$", "COP": "$"}
CUENTA_EMOJI = {"BDV": "🏦", "Binance": "💻", "Efectivo": "💵"}
