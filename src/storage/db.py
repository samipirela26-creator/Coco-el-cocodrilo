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
"""
import logging
import sqlite3
from datetime import datetime
from src.utils.exceptions import StorageError

logger = logging.getLogger('gastos-bot')

MONEDAS_VALIDAS = ('Bs', 'USD', 'COP')

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


class DBClient:
    """Cliente para leer/escribir transacciones y billeteras en SQLite."""

    def __init__(self, db_path: str, initial_balance: float = 0.0,
                 fixed_categories: list = None):
        self.db_path = db_path
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._init_schema(fixed_categories or [], initial_balance)
            logger.info(f"Base de datos SQLite lista en: {db_path}")
        except sqlite3.Error as e:
            raise StorageError(f"No se pudo inicializar la base de datos: {e}")

    def _init_schema(self, fixed_categories: list, initial_balance: float = 0.0) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL CHECK(tipo IN ('gasto', 'ingreso')),
                monto REAL NOT NULL,
                categoria TEXT NOT NULL,
                fecha TEXT NOT NULL,
                descripcion TEXT,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                moneda TEXT NOT NULL DEFAULT 'Bs',
                cuenta TEXT NOT NULL DEFAULT 'BDV'
            )
        """)
        # Migraciones seguras para bases de datos creadas con un esquema previo.
        for statement in (
            "ALTER TABLE transactions ADD COLUMN moneda TEXT NOT NULL DEFAULT 'Bs'",
            "ALTER TABLE transactions ADD COLUMN cuenta TEXT NOT NULL DEFAULT 'BDV'",
        ):
            try:
                cur.execute(statement)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # la columna ya existe

        cur.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                moneda TEXT NOT NULL,
                cuenta TEXT NOT NULL,
                balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (moneda, cuenta)
            )
        """)
        for moneda, cuenta in WALLETS:
            # INITIAL_BALANCE (config) solo se aplica a Bs/BDV, la billetera
            # "principal"; las demás arrancan en 0 y se llenan con capturas,
            # texto/voz o transacciones.
            saldo_inicial = initial_balance if (moneda, cuenta) == ('Bs', 'BDV') else 0.0
            cur.execute(
                "INSERT OR IGNORE INTO wallets (moneda, cuenta, balance, updated_at) VALUES (?, ?, ?, ?)",
                (moneda, cuenta, saldo_inicial, datetime.now().isoformat())
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                is_fixed INTEGER NOT NULL DEFAULT 0
            )
        """)
        for cat in fixed_categories:
            cur.execute(
                "INSERT OR IGNORE INTO categories (name, is_fixed) VALUES (?, 1)",
                (cat,)
            )
        # Asegura que categorías fijas queden marcadas como fijas si ya existían.
        for cat in fixed_categories:
            cur.execute(
                "UPDATE categories SET is_fixed = 1 WHERE name = ?", (cat,)
            )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                page TEXT PRIMARY KEY,
                rate REAL NOT NULL,
                fetched_at TEXT NOT NULL
            )
        """)

        # Historial de cambios de saldo por billetera (fotos, comandos, texto/voz
        # tipo "tengo X en efectivo"), para poder auditar o corregir a mano si un
        # OCR/LLM lee mal un número.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moneda TEXT NOT NULL,
                cuenta TEXT NOT NULL,
                monto_anterior REAL NOT NULL,
                monto_nuevo REAL NOT NULL,
                fuente TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Transacciones
    # ------------------------------------------------------------------ #

    def append_expense(self, tipo: str, fecha: str, descripcion: str,
                        categoria: str, monto: float, user_id: int = None,
                        moneda: str = 'Bs', cuenta: str = None) -> str:
        """Inserta una transacción (gasto o ingreso) y ajusta la billetera
        correspondiente. Si la categoría no existe aún, la crea como no-fija
        (dinámica). Retorna la cuenta resuelta (útil para el mensaje de
        confirmación, ya que en USD puede no ser la que el usuario escribió)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        try:
            self._conn.execute(
                """INSERT INTO transactions
                   (tipo, monto, categoria, fecha, descripcion, user_id, created_at, moneda, cuenta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tipo, monto, categoria, fecha, descripcion, user_id,
                 datetime.now().isoformat(), moneda, cuenta)
            )
            self._ensure_category(categoria)
            delta = monto if tipo == 'ingreso' else -monto
            self._conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE moneda = ? AND cuenta = ?",
                (delta, datetime.now().isoformat(), moneda, cuenta)
            )
            self._conn.commit()
            logger.info(f"Transacción registrada: {tipo} {moneda}/{cuenta} {monto} - {categoria} - {fecha}")
            return cuenta
        except sqlite3.Error as e:
            raise StorageError(f"Error al guardar transacción: {e}")

    def _ensure_category(self, categoria: str) -> None:
        """Inserta la categoría en `categories` si no existe (como dinámica)."""
        if categoria == 'Ingreso':
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO categories (name, is_fixed) VALUES (?, 0)",
            (categoria,)
        )

    def get_all_categories(self) -> list:
        """Retorna todas las categorías (fijas + dinámicas) conocidas."""
        rows = self._conn.execute(
            "SELECT name FROM categories ORDER BY is_fixed DESC, name ASC"
        ).fetchall()
        return [r[0] for r in rows]

    def get_dynamic_categories(self) -> list:
        rows = self._conn.execute(
            "SELECT name FROM categories WHERE is_fixed = 0 ORDER BY name ASC"
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Billeteras (saldo por moneda + cuenta)
    # ------------------------------------------------------------------ #

    def get_wallet_balance(self, moneda: str, cuenta: str = None) -> float:
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        row = self._conn.execute(
            "SELECT balance FROM wallets WHERE moneda = ? AND cuenta = ?", (moneda, cuenta)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_all_wallets(self) -> list:
        """Retorna las 4 billeteras: [{"moneda": str, "cuenta": str, "balance": float}, ...]."""
        rows = self._conn.execute(
            "SELECT moneda, cuenta, balance FROM wallets ORDER BY moneda, cuenta"
        ).fetchall()
        return [{"moneda": m, "cuenta": c, "balance": b} for m, c, b in rows]

    def set_wallet_balance(self, moneda: str, cuenta: str, monto: float,
                            fuente: str = 'comando') -> tuple:
        """Sobrescribe directo el saldo de una billetera (ej: al leer una
        captura de saldo, o cuando el usuario dice "tengo X en efectivo").
        No pide confirmación -- por diseño, para que registrar sea sin
        fricción -- pero deja registro en `balance_snapshots` (antes/después)
        para poder auditar o corregir a mano si algo se leyó mal.

        Retorna (cuenta_resuelta, saldo_anterior)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        try:
            anterior = self.get_wallet_balance(moneda, cuenta)
            self._conn.execute(
                "UPDATE wallets SET balance = ?, updated_at = ? WHERE moneda = ? AND cuenta = ?",
                (monto, datetime.now().isoformat(), moneda, cuenta)
            )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (moneda, cuenta, anterior, monto, fuente, datetime.now().isoformat())
            )
            self._conn.commit()
            logger.info(f"Saldo actualizado: {moneda}/{cuenta} {anterior} -> {monto} (fuente={fuente})")
            return cuenta, anterior
        except sqlite3.Error as e:
            raise StorageError(f"Error al actualizar saldo: {e}")

    # ------------------------------------------------------------------ #
    # Resumen
    # ------------------------------------------------------------------ #

    def get_summary(self, fecha_desde: str = None, fecha_hasta: str = None,
                     moneda: str = None) -> dict:
        """
        Calcula totales y porcentaje de gasto por categoría en un rango de fechas
        (formato YYYY-MM-DD, inclusive). Si no se pasan fechas, usa todo el historial.
        Si se pasa `moneda`, filtra por esa moneda; si no, retorna un dict por moneda.

        Returns (si se pasa moneda):
            {
                "total_gastos": float,
                "total_ingresos": float,
                "categorias": [{"categoria": str, "total": float, "porcentaje": float}, ...]
            }
        Returns (si NO se pasa moneda):
            { "Bs": {...}, "USD": {...}, "COP": {...} }
        """
        if moneda is None:
            return {m: self.get_summary(fecha_desde, fecha_hasta, m) for m in MONEDAS_VALIDAS}

        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'

        where = ["moneda = ?"]
        params = [moneda]
        if fecha_desde:
            where.append("fecha >= ?")
            params.append(fecha_desde)
        if fecha_hasta:
            where.append("fecha <= ?")
            params.append(fecha_hasta)
        where_clause = " AND ".join(where)

        total_gastos_row = self._conn.execute(
            f"SELECT COALESCE(SUM(monto), 0) FROM transactions WHERE tipo = 'gasto' AND {where_clause}",
            params
        ).fetchone()
        total_gastos = total_gastos_row[0] if total_gastos_row else 0.0

        total_ingresos_row = self._conn.execute(
            f"SELECT COALESCE(SUM(monto), 0) FROM transactions WHERE tipo = 'ingreso' AND {where_clause}",
            params
        ).fetchone()
        total_ingresos = total_ingresos_row[0] if total_ingresos_row else 0.0

        cat_rows = self._conn.execute(
            f"""SELECT categoria, SUM(monto) as total
                FROM transactions
                WHERE tipo = 'gasto' AND {where_clause}
                GROUP BY categoria
                ORDER BY total DESC""",
            params
        ).fetchall()

        categorias = []
        for categoria, total in cat_rows:
            porcentaje = (total / total_gastos * 100) if total_gastos > 0 else 0.0
            categorias.append({
                "categoria": categoria,
                "total": total,
                "porcentaje": round(porcentaje, 1)
            })

        return {
            "total_gastos": total_gastos,
            "total_ingresos": total_ingresos,
            "categorias": categorias
        }

    # ------------------------------------------------------------------ #
    # Tasas de cambio (cache)
    # ------------------------------------------------------------------ #

    def get_cached_fx_rate(self, page: str) -> tuple:
        """Retorna (rate, fetched_at) o (None, None) si no hay cache."""
        row = self._conn.execute(
            "SELECT rate, fetched_at FROM fx_rates WHERE page = ?", (page,)
        ).fetchone()
        if row:
            return float(row[0]), row[1]
        return None, None

    def set_cached_fx_rate(self, page: str, rate: float) -> None:
        self._conn.execute(
            "INSERT INTO fx_rates (page, rate, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(page) DO UPDATE SET rate = excluded.rate, fetched_at = excluded.fetched_at",
            (page, rate, datetime.now().isoformat())
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
