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
"""
import logging
import sqlite3
from datetime import datetime
from src.utils.exceptions import StorageError

logger = logging.getLogger('gastos-bot')

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


class DBClient:
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

    # ------------------------------------------------------------------ #
    # Esquema y migraciones
    # ------------------------------------------------------------------ #

    def _column_exists(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    def _init_schema(self, fixed_categories: list) -> None:
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
                cuenta TEXT NOT NULL DEFAULT 'BDV',
                perfil TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migraciones seguras para bases de datos creadas con un esquema previo.
        for statement in (
            "ALTER TABLE transactions ADD COLUMN moneda TEXT NOT NULL DEFAULT 'Bs'",
            "ALTER TABLE transactions ADD COLUMN cuenta TEXT NOT NULL DEFAULT 'BDV'",
            "ALTER TABLE transactions ADD COLUMN perfil TEXT NOT NULL DEFAULT ''",
        ):
            try:
                cur.execute(statement)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # la columna ya existe
        # Filas viejas (de antes del aislamiento por perfil) quedan asignadas
        # al perfil legacy, en vez de con perfil vacío (que no pertenecería a nadie).
        cur.execute("UPDATE transactions SET perfil = ? WHERE perfil = ''", (LEGACY_PROFILE,))

        self._migrate_categories_table(cur)
        for cat in fixed_categories:
            cur.execute(
                "INSERT OR IGNORE INTO categories (name, is_fixed, perfil) VALUES (?, 1, '')",
                (cat,)
            )
            cur.execute(
                "UPDATE categories SET is_fixed = 1 WHERE name = ? AND perfil = ''", (cat,)
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
                created_at TEXT NOT NULL,
                perfil TEXT NOT NULL DEFAULT ''
            )
        """)
        try:
            cur.execute("ALTER TABLE balance_snapshots ADD COLUMN perfil TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        cur.execute("UPDATE balance_snapshots SET perfil = ? WHERE perfil = ''", (LEGACY_PROFILE,))

        # wallets: la clave primaria cambia (ahora incluye perfil), así que si
        # la tabla existe con el esquema viejo (sin perfil) hay que migrarla
        # copiando los datos al perfil legacy en vez de solo agregar la columna.
        self._migrate_wallets_table(cur)

        self._conn.commit()

    def _migrate_wallets_table(self, cur) -> None:
        existing = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wallets'"
        ).fetchone()
        needs_migration = existing is not None and not self._column_exists('wallets', 'perfil')

        if needs_migration:
            # Esquema viejo: PRIMARY KEY (moneda, cuenta), sin perfil. SQLite no
            # permite alterar la PK con ALTER TABLE, así que hay que renombrar,
            # crear la tabla nueva, copiar los datos (asignados al perfil
            # legacy) y borrar la vieja.
            cur.execute("ALTER TABLE wallets RENAME TO wallets_old")
            cur.execute("""
                CREATE TABLE wallets (
                    perfil TEXT NOT NULL,
                    moneda TEXT NOT NULL,
                    cuenta TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (perfil, moneda, cuenta)
                )
            """)
            cur.execute(
                "INSERT INTO wallets (perfil, moneda, cuenta, balance, updated_at) "
                "SELECT ?, moneda, cuenta, balance, updated_at FROM wallets_old",
                (LEGACY_PROFILE,)
            )
            cur.execute("DROP TABLE wallets_old")
            self._conn.commit()
            logger.info(f"Migración: tabla 'wallets' migrada al perfil legacy '{LEGACY_PROFILE}'")
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    perfil TEXT NOT NULL,
                    moneda TEXT NOT NULL,
                    cuenta TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (perfil, moneda, cuenta)
                )
            """)

    def _migrate_categories_table(self, cur) -> None:
        existing = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='categories'"
        ).fetchone()
        needs_migration = existing is not None and not self._column_exists('categories', 'perfil')

        if needs_migration:
            # Esquema viejo: PRIMARY KEY (name), sin perfil, con is_fixed
            # distinguiendo fijas de dinámicas. SQLite no permite alterar la
            # PK con ALTER TABLE, así que hay que renombrar, crear la tabla
            # nueva y copiar los datos: las fijas (is_fixed=1) se quedan
            # compartidas (perfil=''), las dinámicas (is_fixed=0) se asignan
            # al perfil legacy (eran de Samuel, único usuario antes de esto).
            cur.execute("ALTER TABLE categories RENAME TO categories_old")
            cur.execute("""
                CREATE TABLE categories (
                    name TEXT NOT NULL,
                    is_fixed INTEGER NOT NULL DEFAULT 0,
                    perfil TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (name, perfil)
                )
            """)
            cur.execute(
                "INSERT INTO categories (name, is_fixed, perfil) "
                "SELECT name, is_fixed, CASE WHEN is_fixed = 1 THEN '' ELSE ? END FROM categories_old",
                (LEGACY_PROFILE,)
            )
            cur.execute("DROP TABLE categories_old")
            self._conn.commit()
            logger.info(f"Migración: tabla 'categories' migrada (dinámicas -> perfil legacy '{LEGACY_PROFILE}')")
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    name TEXT NOT NULL,
                    is_fixed INTEGER NOT NULL DEFAULT 0,
                    perfil TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (name, perfil)
                )
            """)

    def _ensure_wallets_for_profile(self, perfil: str) -> None:
        """Crea las 4 billeteras del perfil si aún no existen (saldo 0, salvo
        Bs/BDV que arranca con INITIAL_BALANCE la primera vez que se ve ese
        perfil)."""
        for moneda, cuenta in WALLETS:
            saldo_inicial = self.initial_balance if (moneda, cuenta) == ('Bs', 'BDV') else 0.0
            self._conn.execute(
                "INSERT OR IGNORE INTO wallets (perfil, moneda, cuenta, balance, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (perfil, moneda, cuenta, saldo_inicial, datetime.now().isoformat())
            )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Transacciones
    # ------------------------------------------------------------------ #

    def append_expense(self, perfil: str, tipo: str, fecha: str, descripcion: str,
                        categoria: str, monto: float, user_id: int = None,
                        moneda: str = 'Bs', cuenta: str = None) -> str:
        """Inserta una transacción (gasto o ingreso) y ajusta la billetera
        correspondiente DEL PERFIL dado. Si la categoría no existe aún para
        ese perfil, la crea como dinámica. Retorna la cuenta resuelta (útil
        para el mensaje de confirmación, ya que en USD puede no ser la que
        el usuario escribió)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        self._ensure_wallets_for_profile(perfil)
        try:
            self._conn.execute(
                """INSERT INTO transactions
                   (tipo, monto, categoria, fecha, descripcion, user_id, created_at, moneda, cuenta, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tipo, monto, categoria, fecha, descripcion, user_id,
                 datetime.now().isoformat(), moneda, cuenta, perfil)
            )
            self._ensure_category(categoria, perfil)
            delta = monto if tipo == 'ingreso' else -monto
            self._conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? "
                "WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (delta, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            self._conn.commit()
            logger.info(f"Transacción registrada [{perfil}]: {tipo} {moneda}/{cuenta} {monto} - {categoria} - {fecha}")
            return cuenta
        except sqlite3.Error as e:
            raise StorageError(f"Error al guardar transacción: {e}")

    def _ensure_category(self, categoria: str, perfil: str) -> None:
        """Inserta la categoría dinámica en `categories` (scopeada al perfil)
        si no existe."""
        if categoria == 'Ingreso':
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO categories (name, is_fixed, perfil) VALUES (?, 0, ?)",
            (categoria, perfil)
        )

    def get_all_categories(self, perfil: str) -> list:
        """Retorna las categorías fijas (compartidas) + las dinámicas de este perfil."""
        rows = self._conn.execute(
            "SELECT name FROM categories WHERE perfil = '' OR perfil = ? "
            "ORDER BY is_fixed DESC, name ASC",
            (perfil,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_dynamic_categories(self, perfil: str) -> list:
        rows = self._conn.execute(
            "SELECT name FROM categories WHERE is_fixed = 0 AND perfil = ? ORDER BY name ASC",
            (perfil,)
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ #
    # Billeteras (saldo por moneda + cuenta), aisladas por perfil
    # ------------------------------------------------------------------ #

    def get_wallet_balance(self, perfil: str, moneda: str, cuenta: str = None) -> float:
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        self._ensure_wallets_for_profile(perfil)
        row = self._conn.execute(
            "SELECT balance FROM wallets WHERE perfil = ? AND moneda = ? AND cuenta = ?",
            (perfil, moneda, cuenta)
        ).fetchone()
        return float(row[0]) if row else 0.0

    def get_all_wallets(self, perfil: str) -> list:
        """Retorna las 4 billeteras del perfil: [{"moneda": str, "cuenta": str, "balance": float}, ...]."""
        self._ensure_wallets_for_profile(perfil)
        rows = self._conn.execute(
            "SELECT moneda, cuenta, balance FROM wallets WHERE perfil = ? ORDER BY moneda, cuenta",
            (perfil,)
        ).fetchall()
        return [{"moneda": m, "cuenta": c, "balance": b} for m, c, b in rows]

    def set_wallet_balance(self, perfil: str, moneda: str, cuenta: str, monto: float,
                            fuente: str = 'comando') -> tuple:
        """Sobrescribe directo el saldo de una billetera DEL PERFIL dado (ej:
        al leer una captura de saldo, o cuando el usuario dice "tengo X en
        efectivo"). No pide confirmación -- por diseño, para que registrar
        sea sin fricción -- pero deja registro en `balance_snapshots`
        (antes/después) para poder auditar o corregir a mano si algo se leyó
        mal.

        Retorna (cuenta_resuelta, saldo_anterior)."""
        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'
        cuenta = resolve_cuenta(moneda, cuenta)
        self._ensure_wallets_for_profile(perfil)
        try:
            anterior = self.get_wallet_balance(perfil, moneda, cuenta)
            self._conn.execute(
                "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (monto, datetime.now().isoformat(), perfil, moneda, cuenta)
            )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda, cuenta, anterior, monto, fuente, datetime.now().isoformat(), perfil)
            )
            self._conn.commit()
            logger.info(f"Saldo actualizado [{perfil}]: {moneda}/{cuenta} {anterior} -> {monto} (fuente={fuente})")
            return cuenta, anterior
        except sqlite3.Error as e:
            raise StorageError(f"Error al actualizar saldo: {e}")

    def transfer(self, perfil: str, moneda_origen: str, cuenta_origen: str, monto_origen: float,
                 moneda_destino: str, cuenta_destino: str, monto_destino: float,
                 fuente: str = 'texto') -> dict:
        """Mueve dinero entre dos billeteras DEL MISMO PERFIL (ej. Binance ->
        Efectivo, o un cambio de divisa dólares -> pesos). Resta de la
        billetera origen y suma a la de destino -- NO pasa por `transactions`
        (no es un gasto ni un ingreso nuevo, es el mismo dinero cambiando de
        bolsillo), pero deja registro en `balance_snapshots` de ambos lados
        (fuente='transferencia') para poder auditar.

        Retorna un dict con las cuentas resueltas y saldos antes/después de
        ambas billeteras, para armar el mensaje de confirmación."""
        if moneda_origen not in MONEDAS_VALIDAS:
            moneda_origen = 'Bs'
        if moneda_destino not in MONEDAS_VALIDAS:
            moneda_destino = 'Bs'
        cuenta_origen = resolve_cuenta(moneda_origen, cuenta_origen)
        cuenta_destino = resolve_cuenta(moneda_destino, cuenta_destino)
        self._ensure_wallets_for_profile(perfil)
        try:
            anterior_origen = self.get_wallet_balance(perfil, moneda_origen, cuenta_origen)
            nuevo_origen = anterior_origen - monto_origen

            if moneda_origen == moneda_destino and cuenta_origen == cuenta_destino:
                # Transferencia a la misma billetera (raro, pero por si acaso):
                # no tiene sentido restar y sumar lo mismo -- no hace nada.
                anterior_destino = anterior_origen
                nuevo_destino = anterior_destino
            else:
                anterior_destino = self.get_wallet_balance(perfil, moneda_destino, cuenta_destino)
                nuevo_destino = anterior_destino + monto_destino

            ahora = datetime.now().isoformat()
            self._conn.execute(
                "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                (nuevo_origen, ahora, perfil, moneda_origen, cuenta_origen)
            )
            if not (moneda_origen == moneda_destino and cuenta_origen == cuenta_destino):
                self._conn.execute(
                    "UPDATE wallets SET balance = ?, updated_at = ? WHERE perfil = ? AND moneda = ? AND cuenta = ?",
                    (nuevo_destino, ahora, perfil, moneda_destino, cuenta_destino)
                )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda_origen, cuenta_origen, anterior_origen, nuevo_origen, fuente, ahora, perfil)
            )
            self._conn.execute(
                """INSERT INTO balance_snapshots
                   (moneda, cuenta, monto_anterior, monto_nuevo, fuente, created_at, perfil)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (moneda_destino, cuenta_destino, anterior_destino, nuevo_destino, fuente, ahora, perfil)
            )
            self._conn.commit()
            logger.info(
                f"Transferencia [{perfil}]: {moneda_origen}/{cuenta_origen} {anterior_origen} -> {nuevo_origen} "
                f"| {moneda_destino}/{cuenta_destino} {anterior_destino} -> {nuevo_destino}"
            )
            return {
                "moneda_origen": moneda_origen, "cuenta_origen": cuenta_origen,
                "anterior_origen": anterior_origen, "nuevo_origen": nuevo_origen,
                "moneda_destino": moneda_destino, "cuenta_destino": cuenta_destino,
                "anterior_destino": anterior_destino, "nuevo_destino": nuevo_destino,
            }
        except sqlite3.Error as e:
            raise StorageError(f"Error al transferir saldo: {e}")

    # ------------------------------------------------------------------ #
    # Resumen
    # ------------------------------------------------------------------ #

    def get_summary(self, perfil: str, fecha_desde: str = None, fecha_hasta: str = None,
                     moneda: str = None) -> dict:
        """
        Calcula totales y porcentaje de gasto por categoría en un rango de fechas
        (formato YYYY-MM-DD, inclusive), solo para este perfil. Si no se pasan
        fechas, usa todo el historial. Si se pasa `moneda`, filtra por esa
        moneda; si no, retorna un dict por moneda.

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
            return {m: self.get_summary(perfil, fecha_desde, fecha_hasta, m) for m in MONEDAS_VALIDAS}

        if moneda not in MONEDAS_VALIDAS:
            moneda = 'Bs'

        where = ["moneda = ?", "perfil = ?"]
        params = [moneda, perfil]
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
    # Racha de días registrando (para el recordatorio nocturno de Coco)
    # ------------------------------------------------------------------ #

    def has_transactions_on(self, perfil: str, fecha: str) -> bool:
        """True si este perfil tiene al menos una transacción con esa fecha (YYYY-MM-DD)."""
        row = self._conn.execute(
            "SELECT 1 FROM transactions WHERE perfil = ? AND fecha = ? LIMIT 1", (perfil, fecha)
        ).fetchone()
        return row is not None

    def get_current_streak(self, perfil: str, reference_date=None) -> int:
        """Cuenta días consecutivos (terminando en reference_date, hoy por
        defecto) con al menos una transacción registrada, solo para este perfil."""
        from datetime import timedelta
        day = reference_date or datetime.now().date()
        if isinstance(day, str):
            day = datetime.strptime(day, "%Y-%m-%d").date()
        rows = self._conn.execute(
            "SELECT DISTINCT fecha FROM transactions WHERE perfil = ? AND fecha <= ?",
            (perfil, day.strftime("%Y-%m-%d"))
        ).fetchall()
        dias_con_registro = {r[0] for r in rows}
        racha = 0
        cursor = day
        while cursor.strftime("%Y-%m-%d") in dias_con_registro:
            racha += 1
            cursor -= timedelta(days=1)
        return racha

    # ------------------------------------------------------------------ #
    # Tasas de cambio (cache) -- global, no depende del perfil (son
    # precios de mercado, no datos personales)
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
