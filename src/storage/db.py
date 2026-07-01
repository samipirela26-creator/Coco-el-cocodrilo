"""Cliente SQLite para gastos/ingresos. Reemplaza al SheetsClient de
telegram-bot-gastos-llm manteniendo una interfaz similar (append_expense),
y agrega get_balance / get_summary para saldo y porcentajes por categoría.

Soporta multi-moneda (Bs, USD, COP) y categorías dinámicas creadas por el LLM.
"""
import logging
import sqlite3
from datetime import datetime
from src.utils.exceptions import StorageError

logger = logging.getLogger('gastos-bot')

MONEDAS_VALIDAS = ('Bs', 'USD', 'COP')


class DBClient:
    """Cliente para leer/escribir transacciones en SQLite."""

    def __init__(self, db_path: str, initial_balance: float = 0.0,
                 fixed_categories: list = None):
        self.db_path = db_path
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._init_schema(initial_balance, fixed_categories or [])
            logger.info(f"Base de datos SQLite lista en: {db_path}")
        except sqlite3.Error as e:
            raise StorageError(f"No se pudo inicializar la base de datos: {e}")

    def _init_schema(self, initial_balance: float, fixed_categories: list) -> None:
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
                moneda TEXT NOT NULL DEFAULT 'Bs'
            )
        """)
        # Migración segura: si la tabla ya existía sin la columna 'moneda', agregarla.
        try:
            cur.execute("ALTER TABLE transactions ADD COLUMN moneda TEXT NOT NULL DEFAULT 'Bs'")
            self._conn.commit()
        except sqlite3.OperationalError:
            # La columna ya existe (o la tabla se acaba de crear con ella).
            pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('initial_balance', ?)",
            (str(initial_balance),)
        )
        # Saldos iniciales por moneda: initial_balance_Bs / initial_balance_USD / initial_balance_COP
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('initial_balance_Bs', ?)",
            (str(initial_balance),)
        )
        for moneda in ('USD', 'COP'):
            cur.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (f'initial_balance_{moneda}', '0')
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

        # Snapshot de saldo bancario reportado por captura de pantalla (punto 4b).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS balance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moneda TEXT NOT NULL,
                monto_banco REAL NOT NULL,
                monto_bot REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Transacciones
    # ------------------------------------------------------------------ #

    def append_expense(self, tipo: str, fecha: str, descripcion: str,
                        categoria: str, monto: float, user_id: int = None,
                        moneda: str = 'Bs') -> None:
        """Inserta una transacción (gasto o ingreso). Si la categoría no existe
        aún en la tabla `categories`, la crea como no-fija (dinámica)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        try:
            self._conn.execute(
                """INSERT INTO transactions
                   (tipo, monto, categoria, fecha, descripcion, user_id, created_at, moneda)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (tipo, monto, categoria, fecha, descripcion, user_id,
                 datetime.now().isoformat(), moneda)
            )
            self._ensure_category(categoria)
            self._conn.commit()
            logger.info(f"Transacción registrada: {tipo} {moneda} {monto} - {categoria} - {fecha}")
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
    # Saldo inicial / balances
    # ------------------------------------------------------------------ #

    def get_initial_balance(self, moneda: str = 'Bs') -> float:
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (f'initial_balance_{moneda}',)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def set_initial_balance(self, value: float, moneda: str = 'Bs') -> None:
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f'initial_balance_{moneda}', str(value))
        )
        # Mantiene compatibilidad con la clave legacy 'initial_balance' para Bs.
        if moneda == 'Bs':
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('initial_balance', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(value),)
            )
        self._conn.commit()

    def get_balance(self, moneda: str = 'Bs') -> float:
        """Saldo actual = saldo inicial + ingresos - gastos (todo el historial), por moneda."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        row = self._conn.execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN tipo = 'ingreso' THEN monto ELSE 0 END), 0) -
                   COALESCE(SUM(CASE WHEN tipo = 'gasto' THEN monto ELSE 0 END), 0)
               FROM transactions WHERE moneda = ?""",
            (moneda,)
        ).fetchone()
        movimientos = row[0] if row else 0.0
        return self.get_initial_balance(moneda) + movimientos

    def get_balances(self) -> dict:
        """Retorna el saldo actual de las 3 monedas: {'Bs': x, 'USD': y, 'COP': z}."""
        return {m: self.get_balance(m) for m in MONEDAS_VALIDAS}

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

    # ------------------------------------------------------------------ #
    # Snapshots de saldo bancario (captura de pantalla de saldo, punto 4b)
    # ------------------------------------------------------------------ #

    def save_balance_snapshot(self, moneda: str, monto_banco: float, monto_bot: float) -> None:
        self._conn.execute(
            """INSERT INTO balance_snapshots (moneda, monto_banco, monto_bot, created_at)
               VALUES (?, ?, ?, ?)""",
            (moneda, monto_banco, monto_bot, datetime.now().isoformat())
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
