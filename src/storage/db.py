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
import os
import sqlite3
import datetime
from src.utils.exceptions import StorageError
from src.storage.schema import SchemaMixin
from src.storage.wallets import WalletsMixin
from src.storage.transactions import TransactionsMixin
from src.storage.budgets import BudgetsMixin
from src.storage.tithes import TithesMixin
from src.storage.fx_cache import FxCacheMixin
from src.storage.access_control import AccessControlMixin
from src.storage.debts import DebtsMixin
from src.storage.savings import SavingsMixin
from src.storage.reset import ResetMixin
# Re-exportados por compatibilidad: código previo podía importar estos
# nombres directamente desde `src.storage.db`.
from src.storage.constants import (  # noqa: F401
    MONEDAS_VALIDAS, LEGACY_PROFILE, WALLETS, CUENTAS_POR_MONEDA,
    DEFAULT_CUENTA, resolve_cuenta,
)

logger = logging.getLogger('gastos-bot')


class DBClient(SchemaMixin, WalletsMixin, TransactionsMixin, BudgetsMixin, TithesMixin, FxCacheMixin,
                AccessControlMixin, DebtsMixin, SavingsMixin, ResetMixin):
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
            if db_path != ':memory:' and os.path.exists(db_path):
                # Datos financieros: nunca deben quedar legibles por otras
                # cuentas del sistema (defensa en profundidad además de los
                # permisos del home -- ver auditoría de seguridad).
                try:
                    os.chmod(db_path, 0o600)
                except OSError:
                    pass
            logger.info(f"Base de datos SQLite lista en: {db_path}")
        except sqlite3.Error as e:
            raise StorageError(f"No se pudo inicializar la base de datos: {e}")

    def close(self):
        self._conn.close()

    def respaldo_diario(self, carpeta: str = "backups", conservar: int = 14) -> bool:
        """Copia gastos.db a `carpeta/` una vez al día (mismo patrón que
        asistente-bot: API de backup online de SQLite, no una copia cruda del
        archivo vivo -- así el snapshot siempre queda consistente aunque haya
        escrituras concurrentes). Conserva los últimos `conservar` días y
        borra el resto. Retorna True si hizo un backup nuevo, False si ya
        había uno de hoy (para que un job diario sea idempotente si se
        reintenta el mismo día)."""
        hoy = datetime.date.today().isoformat()
        os.makedirs(carpeta, exist_ok=True)
        os.chmod(carpeta, 0o700)  # datos financieros: solo el dueño del proceso puede leer la carpeta
        destino = os.path.join(carpeta, f"gastos-{hoy}.db")
        if os.path.exists(destino):
            return False
        respaldo = sqlite3.connect(destino)
        try:
            self._conn.backup(respaldo)
        finally:
            respaldo.close()
        os.chmod(destino, 0o600)  # el respaldo no debe quedar legible por otras cuentas del sistema
        viejos = sorted(
            f for f in os.listdir(carpeta)
            if f.startswith("gastos-") and f.endswith(".db")
        )
        for f in viejos[:-conservar]:
            try:
                os.remove(os.path.join(carpeta, f))
            except OSError:
                pass
        logger.info(f"Backup diario de la base de datos creado: {destino}")
        return True
