"""Cliente SQLite para gastos/ingresos. Reemplaza al SheetsClient de
telegram-bot-gastos-llm manteniendo una interfaz similar (append_expense),
y agrega get_summary para porcentajes por categoría.

Soporta multi-moneda (Bs, USD, COP) y categorías dinámicas creadas por el LLM.

El saldo se trackea por "billetera" (moneda + cuenta), no solo por moneda,
porque el usuario maneja el dinero en bolsillos distintos que no se mezclan:
- Bs   -> BDV        (cuenta bancaria en bolívares)
- USD  -> Binance    (USDT / dólares digitales)
- USD  -> Efectivo   (dólares en cash)
- COP  -> Efectivo   (pesos colombianos en cash)
Cada billetera es un contador independiente (no se deriva de la suma de
transacciones): las transacciones lo incrementan/decrementan, y una foto de
saldo o una frase tipo "tengo 50 dólares en efectivo" lo puede sobrescribir
directo (ver `set_wallet_balance`).

MULTI-PERFIL: el bot puede tener varias personas usándolo (ej. Samuel con dos
cuentas de Telegram, y un amigo/pareja con la suya), cada una con sus propios
saldos/gastos/racha, totalmente aislados entre sí -- nadie ve los datos de
otro "perfil" aunque compartan el mismo bot/proceso/IA. Todas las tablas de
datos financieros (wallets, transactions, balance_snapshots, categorías
dinámicas) están particionadas por `perfil` (ver src/config.py: USER_PROFILES
agrupa varias cuentas de Telegram bajo un mismo perfil). Las categorías FIJAS
(EXPENSE_CATEGORIES) siguen siendo compartidas/globales -- son solo etiquetas
de configuración, no datos personales.

Organización interna: `DBClient` es la única clase pública y su API/firma de
métodos no cambia nunca por refactors internos. Por dentro, sus métodos están
repartidos por responsabilidad en varios mixins (patrón común para clases
DAO/Repository grandes), cada uno en su propio archivo:
- schema.py       -> SchemaMixin: creación de tablas y migraciones
- wallets.py      -> WalletsMixin: saldo por billetera, transferencias
- transactions.py -> TransactionsMixin: gasto/ingreso, categorías, resumen, racha
- budgets.py      -> BudgetsMixin: presupuestos por categoría
- tithes.py       -> TithesMixin: diezmo (10% de ingresos), solo informativo
- fx_cache.py     -> FxCacheMixin: cache de tasas de cambio
Todos comparten `self._conn` (la conexión sqlite3 abierta en `__init__`).
"""
import logging
import sqlite3
from src.utils.exceptions import StorageError
from src.storage.schema import SchemaMixin
from src.storage.wallets import WalletsMixin
from src.storage.transactions import TransactionsMixin
from src.storage.budgets import BudgetsMixin
from src.storage.tithes import TithesMixin
from src.storage.fx_cache import FxCacheMixin
# Re-exportados por compatibilidad: código previo podía importar estos
# nombres directamente desde `src.storage.db`.
from src.storage.constants import (  # noqa: F401
    MONEDAS_VALIDAS, LEGACY_PROFILE, WALLETS, CUENTAS_POR_MONEDA,
    DEFAULT_CUENTA, resolve_cuenta,
)

logger = logging.getLogger('gastos-bot')


class DBClient(SchemaMixin, WalletsMixin, TransactionsMixin, BudgetsMixin, TithesMixin, FxCacheMixin):
    """Cliente para leer/escribir transacciones y billeteras en SQLite.

    Todos los métodos que tocan datos financieros reciben un `perfil` (str)
    como primer argumento y solo leen/escriben datos de ese perfil."""

    def __init__(self, db_path: str, initial_balance: float = 0.0,
                 fixed_categories: list = None):
        self.db_path = db_path
        self.initial_balance = initial_balance
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._init_schema(fixed_categories or [])
            logger.info(f"Base de datos SQLite lista en: {db_path}")
        except sqlite3.Error as e:
            raise StorageError(f"No se pudo inicializar la base de datos: {e}")

    def close(self):
        self._conn.close()
