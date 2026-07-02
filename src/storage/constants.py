"""Constantes y helpers de dominio compartidos por todos los mixins de
`DBClient` (billeteras válidas, moneda por defecto, resolución de cuenta).
Viven en su propio módulo para que `wallets.py`, `transactions.py`, etc. las
importen sin depender de `db.py` (que a su vez las compone a todas)."""

MONEDAS_VALIDAS = ('Bs', 'USD', 'COP')

# Perfil al que se le asigna cualquier dato ya existente en una base de datos
# creada antes de que existiera el aislamiento multi-perfil (migración segura
# de instalaciones previas de este bot, donde solo Samuel lo usaba).
LEGACY_PROFILE = 'samuel'

# Billeteras conocidas: (moneda, cuenta). Bs y COP solo tienen una cuenta
# posible; USD tiene dos (Binance y Efectivo) porque son bolsillos distintos.
WALLETS = (
    ('Bs', 'BDV'),
    ('USD', 'Binance'),
    ('USD', 'Efectivo'),
    ('COP', 'Efectivo'),
)
CUENTAS_POR_MONEDA = {'Bs': ('BDV',), 'USD': ('Binance', 'Efectivo'), 'COP': ('Efectivo',)}
DEFAULT_CUENTA = {'Bs': 'BDV', 'USD': 'Efectivo', 'COP': 'Efectivo'}


def resolve_cuenta(moneda: str, cuenta: str = None) -> str:
    """Devuelve una cuenta válida para la moneda dada. Bs y COP tienen una
    sola cuenta posible, así que se ignora lo que venga. USD tiene dos
    (Binance/Efectivo); si no viene o no es válida, asume Efectivo (el caso
    más común en el día a día: solo se asume Binance si el mensaje lo
    menciona explícitamente, ver prompt_builder.py)."""
    validas = CUENTAS_POR_MONEDA.get(moneda, ('Efectivo',))
    if cuenta in validas:
        return cuenta
    return DEFAULT_CUENTA.get(moneda, 'Efectivo')
